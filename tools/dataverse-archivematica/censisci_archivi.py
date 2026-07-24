#!/usr/bin/env python3
# =============================================================================
# censisci_archivi.py  —  strumento di sola lettura (pianificazione lotti)
#
# Progetto Arkive — Università degli Studi di Milano, Direzione ICT
#
# SCOPO
#   Individuare, nello scope della ri-acquisizione, i dataset che contengono
#   ARCHIVI COMPRESSI (zip, 7z, tar, gzip, rar...).
#
#   Serve perche' con "Extract packages = Yes" Archivematica estrae gli archivi
#   e tratta ogni file interno come un oggetto autonomo: un dataset che l'API
#   dichiara come "1 file" puo' produrre migliaia di oggetti nell'AIP. Caso
#   reale: doi:10.13130/RD_UNIMI/WPOXS5, dichiarato 1 file / 0,58 GB, ha
#   generato 2238 elementi (1859 eventi di unpacking).
#
#   Senza questo censimento il piano dei lotti e' cieco proprio sui dataset che
#   pesano di piu': lotti apparentemente leggeri possono nascondere decine di
#   migliaia di file da processare.
#
#   Sola lettura: esegue solo GET verso Dataverse, non scarica i file (usa i
#   metadati dell'API) e non tocca Archivematica.
#
# USO
#   cd tools/dataverse-archivematica
#   # i parametri vengono letti dal .env (DATAVERSE_URL, DATAVERSE_API_KEY,
#   # SSL_VERIFY); le variabili di shell hanno la precedenza
#   python3 censisci_archivi.py [lista_doi.txt] [--sleep 0.3]
#                               [--piano lotti]
#
#   Default lista: dois_scope.txt
#   Report:        archivi_report.json      (resumibile)
#   Con --piano <dir>: incrocia il risultato con le liste dei lotti generate da
#   prepara_lotti.py e segnala i lotti sottostimati.
# =============================================================================

import argparse
import json
import os
import sys
import time

import requests
import urllib3


def _load_dotenv(env_file: str = ".env") -> None:
    """Carica il .env senza dipendenze esterne (come la pipeline).
    Le variabili gia' presenti nell'ambiente NON vengono sovrascritte."""
    if not os.path.isfile(env_file):
        return
    with open(env_file, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            key = key.strip()
            value = value.strip().strip('"').strip("'")
            if key and key not in os.environ:
                os.environ[key] = value


# IMPORTANTE: caricare il .env PRIMA di leggere le costanti che ne dipendono.
_load_dotenv()

BASE_URL   = os.environ.get("DATAVERSE_URL", "https://dataverse.unimi.it").rstrip("/")
API_KEY    = os.environ.get("DATAVERSE_API_KEY", "")
SSL_VERIFY = os.environ.get("SSL_VERIFY", "true").lower() not in ("false", "0", "no")

REPORT_PATH = "archivi_report.json"
RETRIES      = 4
BACKOFF_BASE = 3
GB = 1024 ** 3

# Content-type e estensioni che indicano un archivio estraibile da Archivematica
CT_ARCHIVIO = {
    "application/zip", "application/x-zip-compressed",
    "application/x-7z-compressed",
    "application/x-tar", "application/x-gtar",
    "application/gzip", "application/x-gzip",
    "application/x-bzip2", "application/x-xz",
    "application/x-rar-compressed", "application/vnd.rar",
    "application/java-archive",
}
EXT_ARCHIVIO = (".zip", ".7z", ".tar", ".tar.gz", ".tgz", ".gz", ".bz2",
                ".tbz2", ".xz", ".rar", ".jar")


def _headers() -> dict:
    return {"X-Dataverse-key": API_KEY} if API_KEY else {}


def _e_archivio(df: dict, label: str) -> bool:
    ct = (df.get("contentType") or "").lower()
    if ct in CT_ARCHIVIO:
        return True
    nome = (df.get("originalFileName") or df.get("filename") or label or "").lower()
    return nome.endswith(EXT_ARCHIVIO)


def _get_versioni(doi: str):
    """GET /versions con retry sui transitori. Ritorna (esito, payload)."""
    if not SSL_VERIFY:
        urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
    url = f"{BASE_URL}/api/datasets/:persistentId/versions"
    dettaglio = None
    for tentativo in range(1, RETRIES + 1):
        try:
            r = requests.get(url, params={"persistentId": doi}, headers=_headers(),
                             timeout=120, verify=SSL_VERIFY)
            if r.status_code == 200:
                try:
                    return 200, r.json()
                except ValueError:
                    dettaglio = "200 con corpo non-JSON"
            elif r.status_code == 404:
                ctype = (r.headers.get("Content-Type") or "").lower()
                if "json" in ctype:
                    return 404, None      # dataset inesistente: definitivo
                dettaglio = "404 senza corpo JSON (proxy/rate-limit?)"
            elif r.status_code in (401, 403):
                return r.status_code, None
            else:
                dettaglio = f"HTTP {r.status_code}"
        except requests.exceptions.RequestException as e:
            dettaglio = f"errore di rete: {e}"
        if tentativo < RETRIES:
            time.sleep(BACKOFF_BASE * tentativo)
    return "TRANSITORIO", dettaglio


def analizza(doi: str) -> dict:
    esito, payload = _get_versioni(doi)
    if esito != 200:
        return {"doi": doi, "stato": "NON_ANALIZZATO", "dettaglio": str(esito)}

    versioni = payload.get("data", [])
    if not isinstance(versioni, list):
        versioni = []

    n_arch = n_file = 0
    size_arch = 0
    nomi = []
    for ver in versioni:
        for f in ver.get("files", []):
            df = f.get("dataFile", {})
            n_file += 1
            if _e_archivio(df, f.get("label", "")):
                n_arch += 1
                size_arch += int(df.get("originalFileSize") or df.get("fileSize")
                                 or df.get("filesize") or 0)
                nome = df.get("originalFileName") or df.get("filename") or "?"
                if nome not in nomi:
                    nomi.append(nome)

    return {"doi": doi, "stato": "OK", "n_versioni": len(versioni),
            "n_file": n_file, "n_archivi": n_arch,
            "size_archivi": size_arch, "nomi_archivi": nomi[:10]}


def _salva(ris: dict) -> None:
    with open(REPORT_PATH, "w", encoding="utf-8") as fh:
        json.dump({"dettaglio": list(ris.values())}, fh, indent=2, ensure_ascii=False)


def incrocia_piano(ris: dict, dir_piano: str) -> None:
    """Segnala quali lotti contengono dataset con archivi (carico sottostimato)."""
    import glob
    files = sorted(glob.glob(os.path.join(dir_piano, "dois_lotto_*.txt")))
    if not files:
        print(f"\n(nessuna lista di lotti trovata in {dir_piano}/)")
        return
    print("\n" + "=" * 66)
    print("LOTTI CON DATASET CONTENENTI ARCHIVI (carico sottostimato)")
    print("=" * 66)
    for p in files:
        dois = [l.strip() for l in open(p, encoding="utf-8") if l.strip()]
        att = [ris[d] for d in dois
               if d in ris and ris[d].get("n_archivi", 0) > 0]
        if not att:
            continue
        tot_a = sum(r["n_archivi"] for r in att)
        tot_s = sum(r["size_archivi"] for r in att)
        print(f"  {os.path.basename(p):<22} {len(att):>3} DOI con archivi, "
              f"{tot_a:>4} archivi, {tot_s/GB:>6.2f} GB")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("lista", nargs="?", default="dois_scope.txt")
    ap.add_argument("--sleep", type=float, default=0.3)
    ap.add_argument("--piano", default=None,
                    help="directory con le liste dei lotti (es. lotti)")
    ap.add_argument("--force", action="store_true")
    args = ap.parse_args()

    if not os.path.isfile(args.lista):
        print(f"ERRORE: lista non trovata: {args.lista}", file=sys.stderr)
        return 1

    dois, visti = [], set()
    for riga in open(args.lista, encoding="utf-8"):
        riga = riga.strip()
        if not riga or riga.startswith("#"):
            continue
        d = riga if riga.startswith("doi:") else f"doi:{riga}"
        if d not in visti:
            visti.add(d); dois.append(d)

    ris = {}
    if os.path.isfile(REPORT_PATH) and not args.force:
        try:
            for r in json.load(open(REPORT_PATH, encoding="utf-8"))["dettaglio"]:
                ris[r["doi"]] = r
        except Exception:
            ris = {}

    print(f"Istanza    : {BASE_URL}")
    print(f"API key    : {'presente' if API_KEY else 'assente'}")
    print(f"DOI da analizzare: {len(dois)}  (gia' fatti: {len(ris)})")
    print("-" * 66)

    for i, doi in enumerate(dois, 1):
        pre = ris.get(doi)
        if pre and not args.force and pre.get("stato") == "OK":
            continue
        r = analizza(doi)
        ris[doi] = r
        if r.get("n_archivi"):
            print(f"[{i}/{len(dois)}] {doi.split('/')[-1]:10s} -> "
                  f"{r['n_archivi']} archivio/i, "
                  f"{r['size_archivi']/GB:.2f} GB  {', '.join(r['nomi_archivi'][:2])}")
        if i % 20 == 0:
            _salva(ris)
        if args.sleep:
            time.sleep(args.sleep)

    _salva(ris)

    vals = list(ris.values())
    con = [r for r in vals if r.get("n_archivi", 0) > 0]
    ko  = [r for r in vals if r.get("stato") != "OK"]
    tot_arch = sum(r["n_archivi"] for r in con)
    tot_size = sum(r["size_archivi"] for r in con)

    print("\n" + "=" * 66)
    print("RIEPILOGO")
    print(f"  DOI analizzati              : {len(vals)}")
    print(f"  DOI CON archivi compressi   : {len(con)}")
    print(f"  Archivi totali              : {tot_arch}")
    print(f"  Volume degli archivi        : {tot_size/GB:.2f} GB")
    if ko:
        print(f"  DOI non analizzati          : {len(ko)} (rilanciare per ritentare)")
    if con:
        print("\n  I dataset con archivi producono, dopo l'estrazione, un numero di")
        print("  oggetti molto superiore a quello dichiarato dall'API: il piano dei")
        print("  lotti li sottostima. Valutare lotti dedicati per i piu' grandi.")
        print("\n  Primi 15 per volume:")
        for r in sorted(con, key=lambda x: -x["size_archivi"])[:15]:
            print(f"   {r['doi']:<42} {r['n_archivi']:>3} arch  "
                  f"{r['size_archivi']/GB:>6.2f} GB")

    if args.piano:
        incrocia_piano(ris, args.piano)
    return 0


if __name__ == "__main__":
    sys.exit(main())
