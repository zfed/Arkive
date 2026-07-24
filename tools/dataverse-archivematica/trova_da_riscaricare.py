#!/usr/bin/env python3
# =============================================================================
# trova_da_riscaricare.py  —  sola lettura
#
# Individua i pacchetti gia' scaricati che sono INCOMPLETI perche' scaricati
# prima della correzione su `directoryLabel`: i file organizzati in sottocartelle
# venivano salvati tutti nella stessa directory e quelli omonimi si
# sovrascrivevano a vicenda, senza alcun errore.
#
# Il confronto e' fra il numero di file dichiarati dall'API (report di Fase 0)
# e quelli effettivamente presenti su disco.
#
# USO
#   cd tools/dataverse-archivematica
#   python3 trova_da_riscaricare.py [--ts <transfer_source>] [--elimina]
#
#   Senza --elimina non tocca nulla: elenca soltanto.
#   Con --elimina rimuove le cartelle dei DOI incompleti, cosi' il download
#   successivo le ricostruisce da zero (necessario: i file gia' presenti in
#   posizione "piatta" resterebbero come residui).
# =============================================================================

import argparse
import glob
import json
import os
import shutil
import sys


def conta_file(doi_dir: str) -> int:
    """File di dati presenti (esclusi i metadati)."""
    n = 0
    for radice, _dirs, files in os.walk(doi_dir):
        parti = radice.replace(doi_dir, "").split(os.sep)
        if "metadata" in parti:
            continue
        if "objects" not in parti:
            continue
        n += len(files)
    return n


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--ts", default="/mnt/e/DROPBOX/Dropbox/_ARKIVE/TRANSFER_SOURCE")
    ap.add_argument("--report", default="reacquisizione_report.json")
    ap.add_argument("--elimina", action="store_true",
                    help="rimuove le cartelle dei DOI incompleti")
    args = ap.parse_args()

    if not os.path.isfile(args.report):
        print(f"ERRORE: report non trovato: {args.report}", file=sys.stderr)
        return 1

    atteso = {}
    for r in json.load(open(args.report, encoding="utf-8"))["dettaglio"]:
        if r.get("stato") == "OK":
            atteso[r["doi"].split("/")[-1]] = r.get("n_file", 0)

    incompleti, ok, tot_mancanti = [], 0, 0
    for lotto in sorted(glob.glob(os.path.join(args.ts, "LOTTO_*"))):
        for doi_dir in sorted(glob.glob(os.path.join(lotto, "doi_*"))):
            sigla = os.path.basename(doi_dir).split("_")[-1]
            att = atteso.get(sigla)
            if att is None:
                continue
            disco = conta_file(doi_dir)
            if disco < att:
                incompleti.append((os.path.basename(lotto), sigla, att, disco, doi_dir))
                tot_mancanti += att - disco
            else:
                ok += 1

    print(f"Transfer Source : {args.ts}")
    print(f"Pacchetti completi   : {ok}")
    print(f"Pacchetti INCOMPLETI : {len(incompleti)}")
    print(f"File mancanti totali : {tot_mancanti}")
    if incompleti:
        print()
        print(f"  {'lotto':<10} {'DOI':<10} {'atteso':>8} {'disco':>8} {'manca':>8}")
        for lot, sigla, att, disco, _ in incompleti:
            print(f"  {lot:<10} {sigla:<10} {att:>8} {disco:>8} {att-disco:>8}")

        lotti = sorted({x[0] for x in incompleti})
        print()
        print(f"Lotti coinvolti ({len(lotti)}): {', '.join(lotti)}")

    if args.elimina and incompleti:
        print()
        for _lot, sigla, _a, _d, doi_dir in incompleti:
            shutil.rmtree(doi_dir)
            print(f"  rimosso: {doi_dir}")
        print(f"\n{len(incompleti)} cartelle rimosse. Rilanciare il download dei "
              f"lotti coinvolti: i pacchetti completi verranno saltati.")
    elif incompleti:
        print("\nSimulazione: nulla e' stato rimosso. Usare --elimina per procedere.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
