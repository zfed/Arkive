#!/usr/bin/env python3
# =============================================================================
# analizza_versioni.py  —  strumento di sola lettura
#
# Progetto Arkive — Università degli Studi di Milano, Direzione ICT
#
# SCOPO
#   La pipeline attuale scarica i file di OGNI versione di un dataset in una
#   cartella separata (objects/<versione>/objects/), quindi un file invariato
#   tra versioni viene scaricato e conservato piu' volte (runbook Fase 1,
#   sez. 14.3: LOTTO_14, 4 versioni x 259 file identici).
#
#   Questo strumento misura il fenomeno PRIMA di cambiare la pipeline, e
#   verifica empiricamente la regola indicata dai colleghi Dataverse:
#
#     "nelle minor release (x.1, x.2...) cambiano solo i metadati,
#      i file cambiano solo nelle major release (x.0)"
#
#   Per ogni DOI confronta, versione dopo versione, l'insieme dei file
#   (identificati da dataFile.id e checksum) e classifica ogni passaggio:
#     - file aggiunti, rimossi, con contenuto cambiato (stesso id, checksum
#       diverso), rinominati o spostati (stesso id, nome/directoryLabel
#       diversi);
#     - VIOLAZIONE della regola: una minor release con file aggiunti,
#       rimossi o con contenuto cambiato.
#
#   Calcola inoltre, per file e per volume, tre scenari:
#     ATTUALE   : tutti i file di tutte le versioni (comportamento odierno)
#     REGOLA    : file scaricati solo nelle major release (prima versione
#                 di ogni major), come suggerito dai colleghi
#     DEDUP     : ogni contenuto distinto (checksum) scaricato una sola volta
#
#   Non scrive nulla su Dataverse: esegue solo GET. Scrive in locale un report
#   JSON (resumibile) e un CSV per DOI.
#
# USO
#   cd tools/dataverse-archivematica
#   # stesso .env della pipeline: DATAVERSE_URL, DATAVERSE_API_KEY, SSL_VERIFY
#   python3 analizza_versioni.py lista_doi.txt [--sleep 0.3] [--force]
#
#   La lista accetta righe "doi:10.13130/...", "10.13130/..." oppure
#   "https://doi.org/10.13130/...". Righe vuote e commenti (#) ignorati.
#
#   Report: analisi_versioni_report.json   (resumibile: skip dei DOI gia' fatti)
#   CSV:    analisi_versioni.csv           (una riga per DOI)
#
# Dipendenza: requests (gia' usata dalla pipeline).
# =============================================================================

import argparse
import csv
import json
import os
import sys
import time

import requests
import urllib3


def _load_dotenv(env_file: str = ".env") -> None:
    """
    Carica variabili da un file .env senza dipendenze esterne (stesso
    comportamento degli altri script). Le variabili gia' presenti
    nell'ambiente NON vengono sovrascritte (la shell ha la precedenza).
    """
    if not os.path.isfile(env_file):
        return
    with open(env_file, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            key   = key.strip()
            value = value.strip().strip('"').strip("'")
            if key and key not in os.environ:
                os.environ[key] = value


# IMPORTANTE: caricare il .env PRIMA di leggere le costanti che ne dipendono.
_load_dotenv()

BASE_URL   = os.environ.get("DATAVERSE_URL", "https://dataverse.unimi.it").rstrip("/")
API_KEY    = os.environ.get("DATAVERSE_API_KEY", "")
SSL_VERIFY = os.environ.get("SSL_VERIFY", "true").lower() not in ("false", "0", "no")

REPORT_PATH  = "analisi_versioni_report.json"
CSV_PATH     = "analisi_versioni.csv"
RETRIES      = 4
BACKOFF_BASE = 3      # secondi: attesa = BACKOFF_BASE * tentativo

STATI_DEFINITIVI = ("OK", "VUOTO", "NON_RAGGIUNGIBILE", "ACCESSO_NEGATO")


# ── Accesso a Dataverse ───────────────────────────────────────────────────────

def _headers() -> dict:
    return {"X-Dataverse-key": API_KEY} if API_KEY else {}


def _get_versions(doi: str):
    """
    GET /api/datasets/:persistentId/versions (tutte le versioni).

    Restituisce (esito, payload):
      200           -> payload = lista delle versioni
      404           -> 404 confermato su TUTTI i tentativi
      401 / 403     -> accesso negato (definitivo)
      "TRANSITORIO" -> throttling, 5xx, rete, 404 non confermato

    Stessa logica di verifica_raggiungibilita.py: su dataverse.unimi.it un 404
    da rate limiting e' indistinguibile da un 404 reale, quindi il 404 e'
    definitivo solo se si ripete a ogni tentativo.
    """
    if not SSL_VERIFY:
        urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
    url = f"{BASE_URL}/api/datasets/:persistentId/versions"
    dettaglio = None
    n_404 = 0
    for tentativo in range(1, RETRIES + 1):
        try:
            r = requests.get(url, params={"persistentId": doi}, headers=_headers(),
                             timeout=60, verify=SSL_VERIFY)
            if r.status_code == 200:
                try:
                    dati = r.json().get("data", [])
                    return 200, dati if isinstance(dati, list) else []
                except ValueError:
                    dettaglio = "200 con corpo non-JSON (probabile pagina proxy)"
            elif r.status_code == 404:
                n_404 += 1
                dettaglio = "404 (da confermare con i tentativi successivi)"
            elif r.status_code in (401, 403):
                return r.status_code, None
            else:
                dettaglio = f"HTTP {r.status_code}"
        except requests.exceptions.RequestException as e:
            dettaglio = f"errore di rete: {e}"
        if tentativo < RETRIES:
            time.sleep(BACKOFF_BASE * tentativo)
    if n_404 == RETRIES:
        return 404, None
    return "TRANSITORIO", dettaglio


# ── Modello dei file di una versione ──────────────────────────────────────────

def _checksum(df: dict) -> str | None:
    """Checksum dichiarato da Dataverse (oggetto 'checksum' o campo 'md5')."""
    ck = df.get("checksum")
    if isinstance(ck, dict) and ck.get("value"):
        return f"{ck.get('type', '?')}:{ck['value']}"
    if df.get("md5"):
        return f"MD5:{df['md5']}"
    return None


def _size(df: dict) -> int:
    """Dimensione del file come lo scarica la pipeline (originale se ingerito)."""
    if df.get("originalFileName") or df.get("originalFileFormat"):
        return int(df.get("originalFileSize") or df.get("filesize")
                   or df.get("fileSize") or 0)
    return int(df.get("filesize") or df.get("fileSize") or 0)


def _percorso(f: dict) -> str:
    """Percorso logico del file nella versione: directoryLabel/label."""
    label = f.get("label") or f.get("dataFile", {}).get("filename") or ""
    cartella = (f.get("directoryLabel") or "").strip("/")
    return f"{cartella}/{label}" if cartella else label


def _file_della_versione(ver: dict) -> dict:
    """
    Mappa id -> dati del file per una versione.
    Se un file non ha id (non dovrebbe accadere) si usa il percorso come chiave.
    """
    out = {}
    for f in ver.get("files", []):
        df = f.get("dataFile", {})
        chiave = df.get("id") or f"path:{_percorso(f)}"
        ck = _checksum(df)
        out[chiave] = {
            "percorso":  _percorso(f),
            "checksum":  ck,
            # contenuto: checksum se disponibile, altrimenti id (prudenziale:
            # senza checksum non si puo' dire che due id abbiano lo stesso
            # contenuto, quindi non vengono deduplicati fra loro)
            "contenuto": ck or f"id:{chiave}",
            "size":      _size(df),
        }
    return out


def _tag(ver: dict) -> str:
    if ver.get("versionState") == "DRAFT":
        return "DRAFT"
    return f"v{ver.get('versionNumber', '?')}.{ver.get('versionMinorNumber', '?')}"


# ── Analisi di un DOI ─────────────────────────────────────────────────────────

def analizza_versioni(doi: str, versioni: list[dict]) -> dict:
    """Analisi pura (nessuna chiamata di rete): testabile su payload salvati."""
    # Le bozze non sono versioni pubblicate: si contano ma restano fuori
    # dall'analisi della regola (non hanno numero di versione).
    bozze = [v for v in versioni if v.get("versionState") == "DRAFT"]
    numerate = [v for v in versioni if v.get("versionState") != "DRAFT"]
    numerate.sort(key=lambda v: (v.get("versionNumber") or 0,
                                 v.get("versionMinorNumber") or 0))

    stati = sorted({v.get("versionState") for v in numerate if v.get("versionState")})

    passaggi = []
    violazioni = []
    rinomine_in_minor = 0

    # Scenario ATTUALE: tutti i file di tutte le versioni (bozze escluse:
    # scarica_dataverse.py lavora sulle versioni pubblicate)
    file_attuale = vol_attuale = 0
    # Scenario REGOLA: file solo alla prima versione di ogni major
    file_regola = vol_regola = 0
    # Scenario DEDUP: contenuti distinti
    contenuti: dict[str, int] = {}

    prec = None
    prec_files: dict = {}
    major_scaricate = set()
    for ver in numerate:
        files = _file_della_versione(ver)
        for info in files.values():
            file_attuale += 1
            vol_attuale  += info["size"]
            contenuti.setdefault(info["contenuto"], info["size"])

        major = ver.get("versionNumber") or 0
        if major not in major_scaricate:
            major_scaricate.add(major)
            file_regola += len(files)
            vol_regola  += sum(i["size"] for i in files.values())

        if prec is not None:
            tipo = "major" if (ver.get("versionNumber") or 0) != (prec.get("versionNumber") or 0) else "minor"
            ids_prec, ids_cur = set(prec_files), set(files)
            aggiunti  = ids_cur - ids_prec
            rimossi   = ids_prec - ids_cur
            comuni    = ids_cur & ids_prec
            cambiati  = {i for i in comuni
                         if files[i]["checksum"] and prec_files[i]["checksum"]
                         and files[i]["checksum"] != prec_files[i]["checksum"]}
            rinominati = {i for i in comuni - cambiati
                          if files[i]["percorso"] != prec_files[i]["percorso"]}
            p = {
                "da": _tag(prec), "a": _tag(ver), "tipo": tipo,
                "aggiunti": len(aggiunti), "rimossi": len(rimossi),
                "contenuto_cambiato": len(cambiati), "rinominati": len(rinominati),
            }
            passaggi.append(p)
            if tipo == "minor":
                rinomine_in_minor += len(rinominati)
                if aggiunti or rimossi or cambiati:
                    violazioni.append(p)

        prec, prec_files = ver, files

    file_dedup = len(contenuti)
    vol_dedup  = sum(contenuti.values())

    return {
        "doi": doi,
        "stato": "OK" if file_attuale else "VUOTO",
        "n_versioni": len(numerate),
        "n_bozze": len(bozze),
        "stati_versioni": stati,
        "versioni": [_tag(v) for v in numerate],
        "n_major": len(major_scaricate),
        "passaggi": passaggi,
        "violazioni_regola": violazioni,
        "rinomine_in_minor": rinomine_in_minor,
        "file_attuale": file_attuale, "vol_attuale": vol_attuale,
        "file_regola":  file_regola,  "vol_regola":  vol_regola,
        "file_dedup":   file_dedup,   "vol_dedup":   vol_dedup,
    }


def analizza(doi: str) -> dict:
    esito, payload = _get_versions(doi)
    if esito == 200:
        return analizza_versioni(doi, payload)
    if esito == 404:
        return {"doi": doi, "stato": "NON_RAGGIUNGIBILE", "http": 404}
    if esito in (401, 403):
        return {"doi": doi, "stato": "ACCESSO_NEGATO", "http": esito}
    return {"doi": doi, "stato": "DA_RIVERIFICARE", "dettaglio": str(payload)}


# ── Lista, report, riepilogo ──────────────────────────────────────────────────

def _normalizza_doi(riga: str) -> str:
    riga = riga.strip()
    for prefisso in ("https://doi.org/", "http://doi.org/",
                     "https://dx.doi.org/", "doi:"):
        if riga.lower().startswith(prefisso):
            riga = riga[len(prefisso):]
            break
    return f"doi:{riga}"


def _leggi_lista(path: str) -> list[str]:
    dois, visti = [], set()
    with open(path, encoding="utf-8") as fh:
        for riga in fh:
            riga = riga.strip()
            if not riga or riga.startswith("#"):
                continue
            d = _normalizza_doi(riga)
            if d not in visti:
                visti.add(d)
                dois.append(d)
    return dois


def _salva(risultati: dict) -> None:
    with open(REPORT_PATH, "w", encoding="utf-8") as fh:
        json.dump({"dettaglio": list(risultati.values())}, fh,
                  indent=2, ensure_ascii=False)
    campi = ["doi", "stato", "n_versioni", "n_major", "n_bozze",
             "n_violazioni", "rinomine_in_minor",
             "file_attuale", "file_regola", "file_dedup",
             "vol_attuale", "vol_regola", "vol_dedup"]
    with open(CSV_PATH, "w", encoding="utf-8", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=campi, extrasaction="ignore")
        w.writeheader()
        for r in risultati.values():
            riga = dict(r)
            riga["n_violazioni"] = len(r.get("violazioni_regola", []))
            w.writerow(riga)


def _gb(n: int) -> str:
    return f"{n / (1024**3):.2f} GB"


def _riepilogo(risultati: dict) -> None:
    vals = list(risultati.values())
    ok = [r for r in vals if r["stato"] in ("OK", "VUOTO")]
    def conta(s): return sum(1 for r in vals if r["stato"] == s)
    def somma(k): return sum(r.get(k, 0) for r in ok)

    multi    = [r for r in ok if r["n_versioni"] > 1]
    violanti = [r for r in ok if r.get("violazioni_regola")]
    n_viol   = sum(len(r["violazioni_regola"]) for r in violanti)
    n_minor  = sum(1 for r in ok for p in r["passaggi"] if p["tipo"] == "minor")
    con_rin  = [r for r in ok if r.get("rinomine_in_minor")]
    con_bozze = [r for r in ok if r.get("n_bozze")]

    print("\n" + "=" * 64)
    print("RIEPILOGO")
    print(f"  DOI analizzati               : {len(vals)}")
    print(f"  OK / VUOTI                   : {conta('OK')} / {conta('VUOTO')}")
    print(f"  NON RAGGIUNGIBILI (404 reale): {conta('NON_RAGGIUNGIBILE')}")
    print(f"  ACCESSO NEGATO               : {conta('ACCESSO_NEGATO')}")
    print(f"  DA RIVERIFICARE (transitori) : {conta('DA_RIVERIFICARE')}")
    print(f"  DOI con piu' versioni        : {len(multi)}")
    print(f"  DOI con bozza aperta         : {len(con_bozze)}")
    print()
    print("  VERIFICA DELLA REGOLA \"minor = solo metadati\"")
    print(f"    passaggi minor esaminati   : {n_minor}")
    print(f"    passaggi minor con file    : {n_viol}  (su {len(violanti)} DOI)")
    print(f"    file rinominati/spostati   : "
          f"{sum(r['rinomine_in_minor'] for r in con_rin)} in minor release "
          f"(su {len(con_rin)} DOI)")
    print()
    print("  SCENARI                        file        volume")
    print(f"    ATTUALE (ogni versione)    {somma('file_attuale'):>8}   {_gb(somma('vol_attuale')):>12}")
    print(f"    REGOLA  (solo major)       {somma('file_regola'):>8}   {_gb(somma('vol_regola')):>12}")
    print(f"    DEDUP   (contenuti unici)  {somma('file_dedup'):>8}   {_gb(somma('vol_dedup')):>12}")

    if violanti:
        print("\n  DOI con violazioni della regola (dettaglio nel report JSON):")
        for r in violanti[:30]:
            for p in r["violazioni_regola"]:
                print(f"    {r['doi'].split('/')[-1]:8s} {p['da']} -> {p['a']}: "
                      f"+{p['aggiunti']} -{p['rimossi']} ~{p['contenuto_cambiato']}")
        if len(violanti) > 30:
            print(f"    ... e altri {len(violanti) - 30} DOI")
    if conta("DA_RIVERIFICARE"):
        print("\n  Ci sono esiti TRANSITORI: rilanciare (il resume li riprende);")
        print("  se restano molti, aumentare la pausa: --sleep 1")


def main() -> int:
    ap = argparse.ArgumentParser(
        description="Analisi di sola lettura delle versioni Dataverse.")
    ap.add_argument("lista", help="file con un DOI per riga")
    ap.add_argument("--sleep", type=float, default=0.3,
                    help="pausa tra un DOI e l'altro (default 0.3)")
    ap.add_argument("--force", action="store_true",
                    help="rianalizza anche i DOI gia' conclusi nel report")
    args = ap.parse_args()

    if not os.path.isfile(args.lista):
        print(f"ERRORE: lista non trovata: {args.lista}", file=sys.stderr)
        return 1
    dois = _leggi_lista(args.lista)

    risultati: dict = {}
    if os.path.isfile(REPORT_PATH) and not args.force:
        try:
            for r in json.load(open(REPORT_PATH, encoding="utf-8")).get("dettaglio", []):
                risultati[r["doi"]] = r
        except Exception:
            risultati = {}
    # Il report puo' contenere DOI di run precedenti su altre liste:
    # il riepilogo considera solo quelli della lista corrente.
    da_fare = [d for d in dois
               if args.force or risultati.get(d, {}).get("stato") not in STATI_DEFINITIVI]

    print(f"Istanza    : {BASE_URL}")
    print(f"API key    : {'presente' if API_KEY else 'assente'}")
    print(f"DOI lista  : {len(dois)}   (da analizzare: {len(da_fare)})")
    print("-" * 64)

    for i, doi in enumerate(da_fare, 1):
        r = analizza(doi)
        risultati[doi] = r
        nota = r["stato"]
        if r["stato"] in ("OK", "VUOTO"):
            nota += (f"  ver={r['n_versioni']} major={r['n_major']}"
                     f"  file {r['file_attuale']}->{r['file_dedup']}")
            if r["violazioni_regola"]:
                nota += f"  VIOLAZIONI={len(r['violazioni_regola'])}"
        print(f"[{i}/{len(da_fare)}] {doi.split('/')[-1]:10s} -> {nota}")
        if i % 20 == 0:
            _salva(risultati)
        if args.sleep:
            time.sleep(args.sleep)

    _salva(risultati)
    _riepilogo({d: risultati[d] for d in dois if d in risultati})
    return 0


if __name__ == "__main__":
    sys.exit(main())
