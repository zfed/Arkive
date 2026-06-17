#!/usr/bin/env python3
"""
Invia richieste di cancellazione per uno o piu' AIP nello Storage Service
di Archivematica.

Autore: Federica Zanardini
Università degli Studi di Milano - Direzione ICT
Data: 2026-06
Sviluppato con il supporto di Claude AI (Anthropic)

IMPORTANTE - la cancellazione di un AIP in Archivematica e' un processo a
due fasi:
  1) questo script invia la RICHIESTA di cancellazione tramite l'API
     ufficiale (POST /api/v2/file/<uuid>/delete_aip/);
  2) un amministratore dello Storage Service deve APPROVARE la richiesta
     dall'interfaccia web (Storage Service > Packages). Questo secondo
     passaggio e' un controllo di sicurezza intenzionale di Archivematica
     per evitare cancellazioni accidentali, e non esiste un endpoint
     pubblico documentato per automatizzarlo: lo script si ferma quindi
     dopo lo step 1, come previsto dal disegno del sistema.

Uso:
    python3 delete_aips.py --uuid <uuid1> --uuid <uuid2> --reason "Scaduto"
    python3 delete_aips.py --uuid-file uuids.txt --reason "..." --yes
    python3 delete_aips.py --uuid-file uuids.csv --reason "..." --dry-run
"""
import argparse
import csv
import datetime
import os
import sys
import time

from aip_client import StorageServiceClient, StorageServiceError


def load_uuids_from_file(path):
    uuids = []
    with open(path, newline="", encoding="utf-8") as f:
        sample = f.read(2048)
        f.seek(0)
        first_line = sample.splitlines()[0].lower() if sample.splitlines() else ""
        if "," in first_line and "uuid" in first_line:
            reader = csv.DictReader(f)
            for row in reader:
                if row.get("uuid"):
                    uuids.append(row["uuid"].strip())
        else:
            for line in f:
                line = line.strip()
                if line:
                    uuids.append(line)
    return uuids


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--uuid", action="append", default=[], help="UUID di un AIP (ripetibile)")
    parser.add_argument("--uuid-file", help="File con un UUID per riga, oppure CSV con colonna 'uuid'")
    parser.add_argument("--reason", required=True, help="Motivazione della cancellazione (event_reason)")
    parser.add_argument("--pipeline", help="UUID della pipeline Archivematica (default: env SS_PIPELINE_UUID)")
    parser.add_argument("--user-id", type=int, help="ID utente Archivematica (default: env SS_USER_ID)")
    parser.add_argument("--user-email", help="Email utente (default: env SS_USER_EMAIL)")
    parser.add_argument("--dry-run", action="store_true", help="Mostra cosa verrebbe fatto, senza chiamare l'API")
    parser.add_argument("--yes", action="store_true", help="Non chiedere conferma interattiva")
    parser.add_argument("--delay", type=float, default=0.5, help="Pausa in secondi tra una richiesta e l'altra")
    parser.add_argument("--log", default="delete_aips.log", help="File di log (append)")
    args = parser.parse_args()

    uuids = list(args.uuid)
    if args.uuid_file:
        uuids.extend(load_uuids_from_file(args.uuid_file))
    uuids = [u for u in dict.fromkeys(uuids) if u]  # dedup mantenendo l'ordine

    if not uuids:
        print("Nessun UUID specificato (usa --uuid e/o --uuid-file).", file=sys.stderr)
        sys.exit(1)

    pipeline = args.pipeline or os.environ.get("SS_PIPELINE_UUID")
    user_id = args.user_id or os.environ.get("SS_USER_ID")
    user_email = args.user_email or os.environ.get("SS_USER_EMAIL")

    missing = [name for name, val in
               [("pipeline", pipeline), ("user_id", user_id), ("user_email", user_email)] if not val]
    if missing:
        print(
            f"Parametri mancanti: {', '.join(missing)}. "
            f"Passali come argomenti oppure imposta le variabili d'ambiente "
            f"SS_PIPELINE_UUID / SS_USER_ID / SS_USER_EMAIL.",
            file=sys.stderr,
        )
        sys.exit(1)

    try:
        user_id = int(user_id)
    except (TypeError, ValueError):
        print(
            f"Errore: user_id deve essere un numero intero (l'ID dell'utente nel database "
            f"di Archivematica, non lo username), ma e' stato fornito '{user_id}'.\n"
            f"Per trovarlo: accedi al dashboard di Archivematica come amministratore, vai in "
            f"Administration > Users, apri l'utente desiderato e leggi l'ID dalla URL "
            f"(es. .../administration/accounts/3/edit/ -> user_id=3). "
            f"In alternativa l'amministratore di sistema puo' interrogare il database del "
            f"dashboard: SELECT id, username FROM auth_user;",
            file=sys.stderr,
        )
        sys.exit(1)
    if user_id <= 0:
        print(f"Errore: user_id deve essere maggiore di 0 (valore fornito: {user_id}).", file=sys.stderr)
        sys.exit(1)

    print(f"AIP da elaborare: {len(uuids)}")
    for u in uuids:
        print(f"  - {u}")

    if args.dry_run:
        print("\n[DRY RUN] Nessuna richiesta verra' inviata.")
        return

    if not args.yes:
        confirm = input(
            f"\nConfermi l'invio di {len(uuids)} richieste di cancellazione? "
            f"Restera' comunque necessaria l'approvazione manuale nello "
            f"Storage Service. [scrivi 'si' per confermare] "
        )
        if confirm.strip().lower() not in ("si", "sì", "yes", "y"):
            print("Operazione annullata.")
            return

    try:
        client = StorageServiceClient()
    except StorageServiceError as e:
        print(f"Errore di configurazione: {e}", file=sys.stderr)
        sys.exit(1)

    results = []
    with open(args.log, "a", encoding="utf-8") as logf:
        for i, u in enumerate(uuids, 1):
            ts = datetime.datetime.now().isoformat(timespec="seconds")
            try:
                resp = client.request_deletion(u, args.reason, pipeline, user_id, user_email)
                ok = resp.status_code in (200, 201, 202)
                status_text = f"HTTP {resp.status_code}"
                if not ok:
                    status_text += f" - {resp.text[:300]}"
            except Exception as e:
                ok = False
                status_text = f"ERRORE: {e}"

            line = f"{ts}\t{u}\t{'OK' if ok else 'FALLITO'}\t{status_text}"
            print(f"[{i}/{len(uuids)}] {line}")
            logf.write(line + "\n")
            results.append((u, ok))

            if i < len(uuids):
                time.sleep(args.delay)

    n_ok = sum(1 for _, ok in results if ok)
    print(f"\nCompletato: {n_ok}/{len(results)} richieste inviate con successo.")
    print(
        "Ricorda: le richieste restano 'in sospeso' finche' un amministratore "
        "non le approva dall'interfaccia web dello Storage Service (Packages)."
    )


if __name__ == "__main__":
    main()
