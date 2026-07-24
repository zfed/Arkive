#!/usr/bin/env python3
# =============================================================================
# prepara_lotti.py  —  pianificazione dei lotti di ri-acquisizione (sola lettura)
#
# Progetto Arkive — Università degli Studi di Milano, Direzione ICT
#
# SCOPO
#   Suddividere lo scope della ri-acquisizione in LOTTI bilanciati, a partire dal
#   report prodotto da verifica_raggiungibilita.py.
#
#   Il bilanciamento NON avviene per numero di DOI: i dataset sono estremamente
#   disomogenei (da 1 file a diverse migliaia), quindi lotti "da N DOI"
#   avrebbero durate imprevedibili. Si bilancia invece su NUMERO DI FILE e
#   VOLUME, che sono i due fattori che determinano davvero i tempi di download
#   e di ingest.
#
#   Esclusi automaticamente:
#     - i DOI non raggiungibili (404 reale) e ad accesso negato
#     - i DOI vuoti (nessun file in nessuna versione)
#     - i DOI in stato DA_RIVERIFICARE (quadro non stabile: prima riverificarli)
#
#   Un dataset che da solo supera le soglie diventa un lotto a se' stante.
#
#   Non scrive nulla se non i file di lista dei lotti. Non tocca Dataverse ne'
#   Archivematica.
#
# USO
#   cd tools/dataverse-archivematica
#   python3 prepara_lotti.py [--report reacquisizione_report.json]
#                            [--max-file 2000] [--max-gb 4] [--max-doi 100]
#                            [--outdir lotti] [--scrivi]
#
#   Senza --scrivi esegue una simulazione e stampa solo il piano.
# =============================================================================

import argparse
import json
import os
import sys

GB = 1024 ** 3


def carica_scope(path: str):
    with open(path, encoding="utf-8") as fh:
        dati = json.load(fh).get("dettaglio", [])

    scope, esclusi = [], {"NON_RAGGIUNGIBILE": [], "ACCESSO_NEGATO": [],
                          "VUOTO": [], "DA_RIVERIFICARE": []}
    for r in dati:
        st = r.get("stato")
        if st == "OK":
            scope.append(r)
        elif st in esclusi:
            esclusi[st].append(r["doi"])
    return scope, esclusi


def crea_lotti(scope, max_file: int, max_gb: float, max_doi: int):
    """
    Riempimento sequenziale con ordinamento decrescente per numero di file:
    i dataset piu' pesanti vengono collocati per primi, i piccoli riempiono gli
    spazi residui. Un dataset che da solo supera una soglia forma un lotto
    dedicato (segnalato come 'oversize').
    """
    max_byte = max_gb * GB
    ordinati = sorted(scope, key=lambda r: (-r.get("n_file", 0),
                                            -r.get("size_acquisizione", 0)))
    lotti, corrente = [], {"doi": [], "file": 0, "byte": 0, "oversize": False}

    def chiudi():
        if corrente["doi"]:
            lotti.append(dict(corrente))
            corrente.update({"doi": [], "file": 0, "byte": 0, "oversize": False})

    for r in ordinati:
        nf = r.get("n_file", 0)
        nb = r.get("size_acquisizione", 0)

        # Dataset che da solo eccede le soglie: lotto dedicato
        if nf > max_file or nb > max_byte:
            chiudi()
            lotti.append({"doi": [r["doi"]], "file": nf, "byte": nb,
                          "oversize": True})
            continue

        if (corrente["file"] + nf > max_file
                or corrente["byte"] + nb > max_byte
                or len(corrente["doi"]) >= max_doi):
            chiudi()

        corrente["doi"].append(r["doi"])
        corrente["file"] += nf
        corrente["byte"] += nb

    chiudi()
    return lotti


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--report", default="reacquisizione_report.json")
    ap.add_argument("--max-file", type=int, default=2000,
                    help="massimo numero di file per lotto (default 2000)")
    ap.add_argument("--max-gb", type=float, default=4.0,
                    help="massimo volume per lotto in GB (default 4)")
    ap.add_argument("--max-doi", type=int, default=100,
                    help="massimo numero di DOI per lotto (default 100)")
    ap.add_argument("--outdir", default="lotti")
    ap.add_argument("--scrivi", action="store_true",
                    help="scrive i file di lista; senza, solo simulazione")
    args = ap.parse_args()

    if not os.path.isfile(args.report):
        print(f"ERRORE: report non trovato: {args.report}", file=sys.stderr)
        print("Eseguire prima verifica_raggiungibilita.py", file=sys.stderr)
        return 1

    scope, esclusi = carica_scope(args.report)
    if not scope:
        print("ERRORE: nessun DOI in stato OK nel report.", file=sys.stderr)
        return 1

    if esclusi["DA_RIVERIFICARE"]:
        print(f"ATTENZIONE: {len(esclusi['DA_RIVERIFICARE'])} DOI sono ancora "
              f"DA_RIVERIFICARE e vengono esclusi.\n"
              f"           Rilanciare verifica_raggiungibilita.py per chiudere "
              f"il quadro prima di procedere.\n")

    lotti = crea_lotti(scope, args.max_file, args.max_gb, args.max_doi)

    tot_file = sum(l["file"] for l in lotti)
    tot_byte = sum(l["byte"] for l in lotti)
    tot_doi  = sum(len(l["doi"]) for l in lotti)

    print("=" * 66)
    print("PIANO DEI LOTTI" + ("" if args.scrivi else "  (SIMULAZIONE)"))
    print("=" * 66)
    print(f"  DOI in scope (OK)      : {len(scope)}")
    for k, v in esclusi.items():
        if v:
            print(f"  Esclusi {k:<18}: {len(v)}")
    print(f"  Soglie per lotto       : {args.max_file} file, "
          f"{args.max_gb} GB, {args.max_doi} DOI")
    print(f"  Lotti generati         : {len(lotti)}")
    print(f"  Totale ripartito       : {tot_doi} DOI, {tot_file} file, "
          f"{tot_byte/GB:.2f} GB")
    print("-" * 66)
    print(f"  {'lotto':<10}{'DOI':>6}{'file':>9}{'GB':>9}   note")
    for i, l in enumerate(lotti, 1):
        nota = "OVERSIZE (lotto dedicato)" if l["oversize"] else ""
        print(f"  {'LOTTO_%02d' % i:<10}{len(l['doi']):>6}{l['file']:>9}"
              f"{l['byte']/GB:>9.2f}   {nota}")

    if tot_doi != len(scope):
        print(f"\n  [!] Ripartiti {tot_doi} DOI su {len(scope)}: verificare.")

    if not args.scrivi:
        print("\nSimulazione: nessun file scritto. Aggiungere --scrivi per "
              "generare le liste.")
        return 0

    os.makedirs(args.outdir, exist_ok=True)
    for i, l in enumerate(lotti, 1):
        p = os.path.join(args.outdir, f"dois_lotto_{i:02d}.txt")
        with open(p, "w", encoding="utf-8") as fh:
            fh.write("\n".join(l["doi"]) + "\n")
    print(f"\nScritte {len(lotti)} liste in {args.outdir}/")
    print("Per ogni lotto NN:")
    print("  python3 scarica_dataverse.py --dois lotti/dois_lotto_NN.txt \\")
    print("      --output \"$TS_PATH/LOTTO_NN\" --dcat")
    print("  python3 archivematica_ingest.py --source \"$TS_PATH/LOTTO_NN\" \\")
    print("      --auto-approve --state-file stato_riacquisizione.json")
    return 0


if __name__ == "__main__":
    sys.exit(main())
