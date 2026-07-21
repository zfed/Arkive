#!/usr/bin/env python3
# =============================================================================
# diagnostica_checksum.py  —  strumento diagnostico
#
# Progetto Arkive — Università degli Studi di Milano, Direzione ICT
#
# SCOPO
#   Verificare EMPIRICAMENTE, per i file ingeriti come tabellari, a quale
#   rappresentazione si riferisce il checksum registrato nel manifest Dataverse:
#     - all'ORIGINALE (il file proprietario depositato: .dta/.sav/.xlsx/...)
#     - alla versione .tab generata (derivata)
#     - a nessuna delle due
#
#   È il fatto che giustifica il download degli ORIGINALI (format=original) nella
#   pipeline di conservazione: la scelta poggia sull'esito di questo controllo.
#   Lo strumento è conservato in repo per poter RI-dimostrare la proprietà in
#   futuro (nuove versioni Dataverse, audit, verifica su altri dataset).
#
#   Sola lettura: esegue solo GET verso Dataverse, calcola gli hash direttamente
#   dallo stream di risposta e NON scrive nulla su disco. Non modifica né
#   Dataverse né i pacchetti della pipeline.
#
# USO
#   cd tools/dataverse-archivematica          # per leggere le stesse convenzioni
#   # I parametri vengono letti dal file .env della cartella corrente (stesso
#   # .env della pipeline: DATAVERSE_URL, DATAVERSE_API_KEY, SSL_VERIFY). Le
#   # variabili esportate nella shell hanno comunque la precedenza sul .env.
#   python3 diagnostica_checksum.py [doi] [--max N]
#
#   Default doi: doi:10.13130/RD_UNIMI/JMKSAW  (il dataset più problematico)
#   --max N : quanti file ingeriti analizzare (default 3)
#
# Dipendenza: requests (già usata dalla pipeline).
# =============================================================================

import argparse
import hashlib
import os
import sys

import requests
import urllib3


def _load_dotenv(env_file: str = ".env") -> None:
    """
    Carica variabili da un file .env senza dipendenze esterne (stesso
    comportamento di scarica_dataverse.py). Formato: KEY=value, ignora righe
    vuote e commenti (#). Le variabili gia' presenti nell'ambiente NON vengono
    sovrascritte (la shell ha la precedenza sul .env).
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

BASE_URL = os.environ.get("DATAVERSE_URL", "https://dataverse.unimi.it").rstrip("/")
API_KEY  = os.environ.get("DATAVERSE_API_KEY", "")
SSL_VERIFY = os.environ.get("SSL_VERIFY", "true").lower() not in ("false", "0", "no")


def _headers() -> dict:
    return {"X-Dataverse-key": API_KEY} if API_KEY else {}


def _get(url, params=None, stream=False):
    if not SSL_VERIFY:
        urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
    r = requests.get(url, params=params, headers=_headers(),
                     timeout=300, stream=stream, verify=SSL_VERIFY)
    r.raise_for_status()
    return r


def _md5_of_response(r) -> tuple[str, int]:
    """Calcola MD5 e dimensione dei byte scaricati, senza tenere tutto in RAM."""
    h = hashlib.md5()
    n = 0
    for chunk in r.iter_content(chunk_size=1 << 20):
        h.update(chunk)
        n += len(chunk)
    return h.hexdigest(), n


def _manifest_checksums(df: dict) -> dict:
    """Estrae TUTTI i valori di checksum che il manifest espone per il file,
    tenendoli distinti per campo (md5 vecchio stile vs checksum:{type,value})."""
    out = {}
    if df.get("md5"):
        out["md5 (campo legacy)"] = ("md5", df["md5"])
    chk = df.get("checksum")
    if isinstance(chk, dict) and chk.get("value"):
        out["checksum{type,value}"] = (chk.get("type", "?"), chk["value"])
    return out


def analizza_file(df: dict) -> None:
    fid   = df.get("id")
    fname = df.get("filename", f"file_{fid}")
    print("=" * 70)
    print(f"FILE  id={fid}  filename={fname}")
    print(f"  contentType        : {df.get('contentType')}")
    print(f"  originalFileFormat : {df.get('originalFileFormat')}")
    print(f"  originalFileName   : {df.get('originalFileName')}")
    print(f"  originalFileSize   : {df.get('originalFileSize')}")
    print(f"  fileSize/filesize  : {df.get('fileSize') or df.get('filesize')}")

    manifest = _manifest_checksums(df)
    if not manifest:
        print("  [!] Nessun checksum nel manifest per questo file.")
    for label, (algo, val) in manifest.items():
        print(f"  manifest {label}: {algo} = {val}")

    # --- scarica la versione .tab (default) ---
    try:
        r = _get(f"{BASE_URL}/api/access/datafile/{fid}", stream=True)
        tab_md5, tab_size = _md5_of_response(r)
        print(f"  [scaricato .tab]     MD5={tab_md5}  size={tab_size}")
    except Exception as e:
        tab_md5, tab_size = None, None
        print(f"  [scaricato .tab]     ERRORE: {e}")

    # --- scarica l'ORIGINALE (format=original) ---
    try:
        r = _get(f"{BASE_URL}/api/access/datafile/{fid}",
                 params={"format": "original"}, stream=True)
        orig_md5, orig_size = _md5_of_response(r)
        print(f"  [scaricato original] MD5={orig_md5}  size={orig_size}")
    except Exception as e:
        orig_md5, orig_size = None, None
        print(f"  [scaricato original] ERRORE (possibile 404 su file non ingerito): {e}")

    # --- verdetto: a cosa corrisponde ogni checksum del manifest? ---
    print("  VERDETTO:")
    if not manifest:
        print("    (nessun checksum da confrontare)")
    for label, (algo, val) in manifest.items():
        if algo.upper().replace("-", "") != "MD5":
            print(f"    - {label}: algoritmo {algo} != MD5, confronto non eseguito "
                  f"(rilancia adattando l'hash)")
            continue
        v = val.lower()
        dove = []
        if tab_md5 and v == tab_md5.lower():
            dove.append("il .tab GENERATO")
        if orig_md5 and v == orig_md5.lower():
            dove.append("l'ORIGINALE")
        if dove:
            print(f"    - {label} corrisponde a: {', e '.join(dove)}")
        else:
            print(f"    - {label} NON corrisponde né al .tab né all'originale "
                  f"(instabile / rigenerato?)")

    if df.get("originalFileSize") and orig_size is not None:
        ok = "OK" if int(df["originalFileSize"]) == orig_size else "DIVERSA"
        print(f"    - originalFileSize vs dimensione originale scaricata: {ok} "
              f"({df['originalFileSize']} vs {orig_size})")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("doi", nargs="?", default="doi:10.13130/RD_UNIMI/JMKSAW")
    ap.add_argument("--max", type=int, default=3)
    args = ap.parse_args()

    print(f"Istanza     : {BASE_URL}")
    print(f"DOI         : {args.doi}")
    print(f"API key     : {'presente' if API_KEY else 'assente (solo dataset pubblici)'}")
    print(f"SSL verify  : {SSL_VERIFY}")
    print()

    url = f"{BASE_URL}/api/datasets/:persistentId/"
    try:
        data = _get(url, params={"persistentId": args.doi}).json()
    except Exception as e:
        print(f"ERRORE nel recupero del dataset: {e}", file=sys.stderr)
        return 1

    files = data.get("data", {}).get("latestVersion", {}).get("files", [])
    if not files:
        print("Nessun file nella versione più recente.", file=sys.stderr)
        return 1

    # Seleziona i file ingeriti come tabellari (hanno i campi original*)
    ingeriti = [f["dataFile"] for f in files
                if f.get("dataFile", {}).get("originalFileName")
                or f.get("dataFile", {}).get("originalFileFormat")
                or (f.get("dataFile", {}).get("contentType") == "text/tab-separated-values")]

    if not ingeriti:
        print("Nessun file ingerito come tabellare trovato in questo dataset.")
        print("(Il download format=original riguarda solo i file ingeriti.)")
        return 0

    print(f"Trovati {len(ingeriti)} file ingeriti; ne analizzo {min(args.max, len(ingeriti))}.\n")
    for df in ingeriti[:args.max]:
        analizza_file(df)

    print("=" * 70)
    print("\nCONCLUSIONE DA LEGGERE:")
    print("  Se 'manifest ... corrisponde a: l'ORIGINALE' => nel Livello 2 possiamo")
    print("  verificare gli originali col checksum del manifest (fixity forte).")
    print("  Se corrisponde al .tab o a nulla => per gli originali si verifica con")
    print("  originalFileSize e si registra un checksum calcolato all'acquisizione.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
