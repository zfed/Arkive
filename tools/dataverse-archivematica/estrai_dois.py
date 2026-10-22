#!/usr/bin/env python3
# =============================================================================
# estrai_dois.py  —  strumento di sola lettura (preparazione liste DOI)
#
# Progetto Arkive — Università degli Studi di Milano, Direzione ICT
#
# SCOPO
#   Converte l'elenco dei dataset fornito dai colleghi di Dataverse (JSON) nel
#   file di testo letto dalla pipeline (dois.txt): un DOI per riga, nel formato
#   persistentId "doi:10.13130/RD_UNIMI/XXXXXX", come in dois.txt.example.
#
#   Formato di input atteso: una lista di oggetti con un campo che contiene
#   il DOI. Sono riconosciuti automaticamente (in quest'ordine) i formati
#   forniti finora dai colleghi:
#       [{"dsUrl": "https://doi.org/10.13130/RD_UNIMI/KCYYEB",
#         "pubDate": "2020-09-09", "name": "..."}, ...]
#       [{"global_id": "10.13130/RD_UNIMI/KCYYEB",
#         "datasetUrl": "https://doi.org/10.13130/RD_UNIMI/KCYYEB",
#         "published_at": "...", "entity_id": 5011, "name": "...",
#         "size": 112986}, ...]
#   cioe' i campi dsUrl, datasetUrl, global_id, persistentId. Per un campo
#   diverso si usa --campo. Sono accettati anche un oggetto con la lista sotto
#   "data" o "items", e valori nelle forme https://doi.org/...,
#   http://dx.doi.org/..., doi:... oppure 10.xxxx/... nudo.
#
#   Il programma:
#     - normalizza ogni DOI nel formato "doi:10.xxxx/...";
#     - elimina i duplicati (confronto senza distinzione maiuscole/minuscole,
#       come da specifica DOI) mantenendo l'ordine del JSON;
#     - scarta, segnalandoli, i record senza campo o con un valore che non e'
#       un DOI;
#     - con --escludi, toglie i DOI presenti in una o piu' liste (es.
#       doi_non_raggiungibili.txt);
#     - scrive in testa al file una riga di commento con sorgente e data (le
#       righe che iniziano con # sono ignorate da scarica_dataverse.py).
#
#   Non contatta Dataverse e non modifica il JSON. Non sovrascrive un file di
#   output esistente se non con --force.
#
# USO
#   cd tools/dataverse-archivematica
#   python3 estrai_dois.py fullDatasetList.json
#   python3 estrai_dois.py fullDatasetList.json -o dois_scope.txt --force
#   python3 estrai_dois.py fullDatasetList.json --escludi doi_non_raggiungibili.txt
#
#   Default output: dois.txt
#
# Dipendenze: solo libreria standard.
# =============================================================================

import argparse
import json
import os
import re
import sys
from datetime import date

# Prefissi che precedono il DOI vero e proprio (confronto case-insensitive)
PREFISSI = (
    "https://doi.org/",
    "http://doi.org/",
    "https://dx.doi.org/",
    "http://dx.doi.org/",
    "doi:",
)

# Un DOI inizia sempre con "10." seguito dal prefisso numerico del registrante
RE_DOI = re.compile(r"^10\.\d{4,9}/\S+$")

# Campi riconosciuti automaticamente, in ordine di preferenza
CAMPI_NOTI = ("dsUrl", "datasetUrl", "global_id", "persistentId")


def rileva_campo(record: list):
    """Restituisce il primo campo noto presente nei record, oppure None."""
    for campo in CAMPI_NOTI:
        if any(isinstance(r, dict) and campo in r for r in record):
            return campo
    return None


def normalizza(valore: str):
    """Restituisce il DOI nel formato 'doi:10.xxxx/...' oppure None."""
    v = valore.strip()
    for p in PREFISSI:
        if v.lower().startswith(p):
            v = v[len(p):]
            break
    v = v.strip().rstrip("/")
    if not RE_DOI.match(v):
        return None
    return f"doi:{v}"


def carica_record(path: str) -> list:
    with open(path, encoding="utf-8") as fh:
        dati = json.load(fh)
    if isinstance(dati, dict):
        for chiave in ("data", "items"):
            if isinstance(dati.get(chiave), list):
                return dati[chiave]
        raise ValueError("oggetto JSON senza una lista sotto 'data' o 'items'")
    if not isinstance(dati, list):
        raise ValueError("il JSON deve essere una lista di oggetti")
    return dati


def carica_esclusioni(paths: list) -> set:
    """Legge liste DOI in formato dois.txt (righe vuote e # ignorate)."""
    esclusi = set()
    for p in paths:
        with open(p, encoding="utf-8") as fh:
            for riga in fh:
                riga = riga.strip()
                if not riga or riga.startswith("#"):
                    continue
                d = normalizza(riga.split()[0])
                if d:
                    esclusi.add(d.lower())
    return esclusi


def main() -> int:
    ap = argparse.ArgumentParser(
        description="Converte il JSON dei dataset Dataverse in dois.txt")
    ap.add_argument("json", help="file JSON fornito dai colleghi di Dataverse")
    ap.add_argument("-o", "--output", default="dois.txt",
                    help="file di output (default: dois.txt)")
    ap.add_argument("--campo", default=None,
                    help="campo che contiene il DOI (default: rilevato "
                         "automaticamente tra " + ", ".join(CAMPI_NOTI) + ")")
    ap.add_argument("--escludi", action="append", default=[], metavar="LISTA",
                    help="lista di DOI da escludere; ripetibile")
    ap.add_argument("--force", action="store_true",
                    help="sovrascrive il file di output se esiste gia'")
    args = ap.parse_args()

    if not os.path.isfile(args.json):
        print(f"ERRORE: file JSON non trovato: {args.json}", file=sys.stderr)
        return 1
    for p in args.escludi:
        if not os.path.isfile(p):
            print(f"ERRORE: lista di esclusione non trovata: {p}",
                  file=sys.stderr)
            return 1
    if os.path.exists(args.output) and not args.force:
        print(f"ERRORE: {args.output} esiste gia'. Usa --force per "
              f"sovrascriverlo oppure -o per un altro nome.", file=sys.stderr)
        return 1

    try:
        record = carica_record(args.json)
    except (ValueError, json.JSONDecodeError) as e:
        print(f"ERRORE: JSON non valido: {e}", file=sys.stderr)
        return 1

    if args.campo is None:
        args.campo = rileva_campo(record)
        if args.campo is None:
            print("ERRORE: nessun campo DOI riconosciuto nei record (cercati: "
                  + ", ".join(CAMPI_NOTI) + "). Indicalo con --campo.",
                  file=sys.stderr)
            return 1

    esclusi = carica_esclusioni(args.escludi)

    dois, visti = [], set()
    n_dup = n_escl = 0
    scartati = []
    for i, r in enumerate(record):
        valore = r.get(args.campo) if isinstance(r, dict) else None
        d = normalizza(valore) if isinstance(valore, str) else None
        if d is None:
            scartati.append((i, valore))
            continue
        chiave = d.lower()
        if chiave in visti:
            n_dup += 1
            continue
        visti.add(chiave)
        if chiave in esclusi:
            n_escl += 1
            continue
        dois.append(d)

    with open(args.output, "w", encoding="utf-8") as fh:
        fh.write(f"# Generato da estrai_dois.py il {date.today().isoformat()} "
                 f"da {os.path.basename(args.json)} ({len(dois)} DOI)\n")
        for d in dois:
            fh.write(d + "\n")

    print(f"Campo DOI usato     : {args.campo}")
    print(f"Record nel JSON     : {len(record)}")
    print(f"Duplicati ignorati  : {n_dup}")
    print(f"Esclusi da liste    : {n_escl}")
    print(f"Record scartati     : {len(scartati)}")
    for i, v in scartati[:20]:
        print(f"  - record {i}: {args.campo}={v!r}")
    if len(scartati) > 20:
        print(f"  ... e altri {len(scartati) - 20}")
    print(f"DOI scritti         : {len(dois)}  ->  {args.output}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
