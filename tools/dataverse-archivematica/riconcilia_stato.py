#!/usr/bin/env python3
"""
riconcilia_stato.py
-------------------
Riconcilia il file di stato (stato_archivematica.json) con lo Storage
Service di Archivematica: per i pacchetti marcati "failed" il cui ingest
è in realtà andato a buon fine (es. approvazione manuale dopo un timeout
dello script), recupera l'AIP UUID reale dallo Storage Service e aggiorna
la voce a "ingested".

Senza questa riconciliazione, un successivo lancio di
archivematica_ingest.py riprocesserebbe da zero i pacchetti "failed",
creando AIP DUPLICATI per gli stessi DOI.

Criteri di aggancio (prudenziali):
  - l'AIP deve avere status UPLOADED nello Storage Service;
  - il DOI (o frammento) deve comparire nel current_path dell'AIP;
  - il match deve essere UNIVOCO (esattamente 1 AIP): con 0 o 2+ match
    la voce NON viene toccata e viene segnalata per verifica manuale.

Configurazione (stessa filosofia della pipeline):
  priorità: argomento CLI > variabile d'ambiente esportata > file .env
  variabili usate: SS_URL, SS_USER, SS_API_KEY, SSL_VERIFY

Uso:
  # anteprima senza modifiche (consigliato come primo passo)
  python3 riconcilia_stato.py --dry-run

  # riconcilia tutti i pacchetti "failed" nel file di stato
  python3 riconcilia_stato.py

  # riconcilia solo DOI specifici (frammenti, separati da virgola)
  python3 riconcilia_stato.py --dois H4W0JR,NZRX1C

  # file di stato in un percorso diverso
  python3 riconcilia_stato.py --state-file /percorso/stato_archivematica.json

Prima di ogni scrittura viene creato automaticamente un backup:
  <state-file>.bak_<YYYYMMDD>_<HHMMSS>

Autore: Fede (zfed) — Università degli Studi di Milano, Direzione ICT (2026)
Sviluppato con il supporto di Claude AI (Anthropic).
Licenza: MIT
"""

import argparse
import json
import os
import shutil
import sys
from datetime import datetime
from pathlib import Path

import requests

# ── Caricamento .env (identico ad archivematica_ingest.py) ────────────────────


def _load_dotenv(env_file: str = ".env") -> None:
    """
    Carica variabili da un file .env senza dipendenze esterne.
    Formato supportato: KEY=value, ignora righe vuote e commenti (#).
    Le variabili già presenti nell'ambiente non vengono sovrascritte.
    """
    p = Path(env_file)
    if not p.exists():
        return
    with open(p, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            key = key.strip()
            value = value.strip().strip('"').strip("'")
            if key and key not in os.environ:
                os.environ[key] = value


_load_dotenv()

# ── Configurazione (letta DOPO _load_dotenv, mai prima!) ──────────────────────

SS_URL = os.environ.get("SS_URL", "")
SS_USER = os.environ.get("SS_USER", "")
SS_API_KEY = os.environ.get("SS_API_KEY", "")
SSL_VERIFY = os.environ.get("SSL_VERIFY", "true").strip().lower() not in (
    "false", "0", "no",
)

PAGE_LIMIT = 100  # AIP per pagina nelle chiamate allo Storage Service


# ── Funzioni ──────────────────────────────────────────────────────────────────


def fetch_all_aips(ss_url: str, ss_user: str, ss_api_key: str,
                   ssl_verify: bool, timeout: int) -> list[dict]:
    """
    Scarica l'elenco completo degli AIP dallo Storage Service (paginato).
    """
    headers = {"Authorization": f"ApiKey {ss_user}:{ss_api_key}"}
    aips: list[dict] = []
    url = f"{ss_url}/api/v2/file/?package_type=AIP&limit={PAGE_LIMIT}"
    while url:
        r = requests.get(url, headers=headers, verify=ssl_verify,
                         timeout=timeout)
        r.raise_for_status()
        data = r.json()
        aips.extend(data.get("objects", []))
        nxt = data.get("meta", {}).get("next")
        url = f"{ss_url}{nxt}" if nxt else None
        print(f"  [SS] AIP recuperati finora: {len(aips)}", end="\r")
    print()
    return aips


def match_aip(doi_fragment: str, aips: list[dict]) -> list[dict]:
    """
    Restituisce gli AIP con status UPLOADED il cui current_path
    contiene il frammento di DOI indicato.
    """
    return [
        a for a in aips
        if a.get("status") == "UPLOADED"
        and doi_fragment in (a.get("current_path") or "")
    ]


def backup_state_file(state_path: Path) -> Path:
    """Crea una copia di backup del file di stato con timestamp."""
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    bak = state_path.with_name(f"{state_path.name}.bak_{ts}")
    shutil.copyfile(state_path, bak)
    return bak


def main() -> int:
    parser = argparse.ArgumentParser(
        description=("Riconcilia i pacchetti 'failed' del file di stato con "
                     "gli AIP realmente presenti nello Storage Service."))
    parser.add_argument("--state-file", default="stato_archivematica.json",
        help="File di stato da riconciliare "
             "(default: stato_archivematica.json)")
    parser.add_argument("--dois", default="",
        help="Frammenti di DOI da riconciliare, separati da virgola "
             "(default: tutti i pacchetti 'failed' nel file di stato)")
    parser.add_argument("--ss-url", default="",
        help="URL Storage Service (default: SS_URL dal .env)")
    parser.add_argument("--ss-user", default="",
        help="Utente Storage Service (default: SS_USER dal .env)")
    parser.add_argument("--ss-apikey", default="",
        help="API key Storage Service (default: SS_API_KEY dal .env)")
    parser.add_argument("--no-verify-ssl", action="store_true",
        help="Disabilita la verifica SSL (per certificati self-signed; "
             "equivale a SSL_VERIFY=false nel .env)")
    parser.add_argument("--timeout", type=int, default=120,
        help="Timeout in secondi per le chiamate allo Storage Service "
             "(default: 120)")
    parser.add_argument("--dry-run", action="store_true",
        help="Mostra cosa verrebbe aggiornato senza modificare nulla")
    args = parser.parse_args()

    # Priorità: CLI > ambiente/.env
    ss_url = (args.ss_url or SS_URL).rstrip("/")
    ss_user = args.ss_user or SS_USER
    ss_api_key = args.ss_apikey or SS_API_KEY
    ssl_verify = False if args.no_verify_ssl else SSL_VERIFY

    if not (ss_url and ss_user and ss_api_key):
        print("[ERRORE] SS_URL, SS_USER e SS_API_KEY sono obbligatori "
              "(via .env, variabili d'ambiente o argomenti CLI).")
        return 1

    if not ssl_verify:
        import urllib3
        urllib3.disable_warnings()

    state_path = Path(args.state_file)
    if not state_path.is_file():
        print(f"[ERRORE] File di stato non trovato: {state_path}")
        return 1

    with open(state_path, encoding="utf-8") as f:
        state = json.load(f)

    # Selezione delle voci da riconciliare
    if args.dois:
        fragments = [d.strip() for d in args.dois.split(",") if d.strip()]
        targets = {}
        for frag in fragments:
            keys = [k for k in state if frag in k]
            if not keys:
                print(f"[AVVISO] Nessuna voce nel file di stato contiene "
                      f"'{frag}' — ignorato.")
            for k in keys:
                targets[k] = frag
    else:
        targets = {k: k for k, v in state.items()
                   if v.get("status") == "failed"}

    if not targets:
        print("Nessuna voce da riconciliare: niente da fare.")
        return 0

    print(f"Voci da riconciliare: {len(targets)}")
    print(f"Storage Service     : {ss_url}  (utente: {ss_user})")
    print(f"SSL verify          : {ssl_verify}")
    if args.dry_run:
        print("Modalità            : DRY-RUN (nessuna modifica)")
    print()

    print("Recupero elenco AIP dallo Storage Service ...")
    aips = fetch_all_aips(ss_url, ss_user, ss_api_key, ssl_verify,
                          args.timeout)
    print(f"AIP totali nello Storage Service: {len(aips)}\n")

    aggiornati, da_verificare = [], []
    for key, fragment in sorted(targets.items()):
        matches = match_aip(fragment, aips)
        if len(matches) == 1:
            aip = matches[0]
            aggiornati.append(key)
            print(f"  OK  {key}")
            print(f"      -> AIP UUID {aip['uuid']}")
            if not args.dry_run:
                state[key].update(
                    status="ingested",
                    aip_uuid=aip["uuid"],
                    error=None,
                    completed_at=datetime.now().astimezone().isoformat(),
                )
        else:
            da_verificare.append(key)
            print(f"  !!  {key}: {len(matches)} AIP UPLOADED trovati "
                  f"— voce NON modificata, verifica manuale necessaria")
            for m in matches:
                print(f"      candidato: {m['uuid']}  {m['current_path']}")

    print()
    if not args.dry_run and aggiornati:
        bak = backup_state_file(state_path)
        print(f"Backup del file di stato: {bak}")
        with open(state_path, "w", encoding="utf-8") as f:
            json.dump(state, f, indent=2, ensure_ascii=False)
        print(f"File di stato aggiornato: {state_path}")

    print(f"\nRiepilogo: {len(aggiornati)} aggiornat"
          f"{'a' if len(aggiornati) == 1 else 'e'}, "
          f"{len(da_verificare)} da verificare manualmente"
          f"{' (DRY-RUN: nessuna modifica applicata)' if args.dry_run else ''}")
    return 0 if not da_verificare else 2


if __name__ == "__main__":
    sys.exit(main())
