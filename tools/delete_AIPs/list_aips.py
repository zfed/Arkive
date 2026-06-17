#!/usr/bin/env python3
"""
Elenca gli AIP presenti nello Storage Service di Archivematica, con la
possibilita' di filtrare per stato e di esportare il risultato in CSV.
Utile per individuare le candidate da passare poi a delete_aips.py.

Autore: Federica Zanardini
Università degli Studi di Milano - Direzione ICT
Data: 2026-06
Sviluppato con il supporto di Claude AI (Anthropic)

Esempi:
    python3 list_aips.py
    python3 list_aips.py --status UPLOADED
    python3 list_aips.py --status DEL_REQ --csv da_rivedere.csv
"""
import argparse
import csv
import sys

from aip_client import StorageServiceClient, StorageServiceError


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--status", help="Filtra per stato del package (es. UPLOADED, DEL_REQ, DELETED)")
    parser.add_argument("--csv", help="Percorso del file CSV di output (uuid,status,size,current_path)")
    args = parser.parse_args()

    try:
        client = StorageServiceClient()
    except StorageServiceError as e:
        print(f"Errore di configurazione: {e}", file=sys.stderr)
        sys.exit(1)

    rows = []
    for pkg in client.list_packages(package_type="AIP"):
        if args.status and pkg.get("status") != args.status:
            continue
        rows.append({
            "uuid": pkg.get("uuid"),
            "status": pkg.get("status"),
            "size": pkg.get("size"),
            "current_path": pkg.get("current_path"),
        })

    for r in rows:
        print(f"{r['uuid']}\t{r['status']}\t{r['size']}\t{r['current_path']}")

    print(f"\nTotale: {len(rows)} AIP", file=sys.stderr)

    if args.csv:
        with open(args.csv, "w", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=["uuid", "status", "size", "current_path"])
            writer.writeheader()
            writer.writerows(rows)
        print(f"Scritto: {args.csv}", file=sys.stderr)


if __name__ == "__main__":
    main()
