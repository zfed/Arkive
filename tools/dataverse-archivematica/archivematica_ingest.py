#!/usr/bin/env python3
"""
archivematica_ingest.py
-----------------------
Trasferisce e ingesta in Archivematica i dataset presenti nella Transfer
Source Location, tenendo traccia dei pacchetti già processati in un file
di stato (stato_archivematica.json) per non ripetere operazioni già
completate.

Ogni cartella DOI (con tutte le sue versioni v1.0, v2.0, ... così come
organizzate al suo interno) viene trasferita come UN UNICO pacchetto.
Il nome del pacchetto è nel formato:

    AIP-<YYYYMMDD>-<DOI>

dove <YYYYMMDD> è la data di avvio del transfer e <DOI> è il nome
sanitizzato della cartella del dataset.

Flusso per ogni pacchetto DOI:
  1. Recupero del path assoluto della Transfer Source dallo Storage Service
  2. Avvio del Transfer (intera cartella DOI) tramite Dashboard API
  3. Polling fino al completamento del Transfer
  4. Polling fino al completamento dell'Ingest (SIP → AIP)
  5. Salvataggio dell'AIP UUID nel file di stato

Struttura attesa nella Transfer Source Location:
  <transfer_source>/
    <doi_sanitizzato>/
      objects/
        v1.0/
          objects/
          metadata/
        v2.0/
          ...
      metadata/
        metadata.csv

File di stato  stato_archivematica.json:
  {
    "<doi_sanitizzato>": {
      "transfer_name":  "AIP-20240115-doi_10.13130_RD_UNIMI_ABC",
      "transfer_uuid":  "...",
      "sip_uuid":       "...",
      "aip_uuid":       "...",
      "status":         "ingested" | "transferred" | "failed" | "in_progress",
      "completed_at":   "2024-01-15T12:00:00+00:00",
      "error":          null
    }
  }

Uso:
  python archivematica_ingest.py \\
      --am-user zfed --am-apikey <chiave> \\
      --ss-user archivematica --ss-apikey <chiave> \\
      --transfer-source <UUID-location>

  # oppure con variabili d'ambiente:
  export AM_API_KEY=<chiave>
  export SS_API_KEY=<chiave>
  export AM_TRANSFER_SOURCE_UUID=<UUID>
  python archivematica_ingest.py

  # elenca le Transfer Source disponibili:
  python archivematica_ingest.py --list-sources \\
      --ss-user archivematica --ss-apikey <chiave>
"""

import argparse
import base64
import json
import os
import shutil
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import requests

# ── Configurazione ────────────────────────────────────────────────────────────

STATE_FILE           = "stato_archivematica.json"

AM_URL               = "http://localhost"       # Dashboard Archivematica
AM_USER              = "zfed"
AM_API_KEY           = ""                       # sovrascrivibile da CLI / env

SS_URL               = "http://localhost:8000"  # Storage Service
SS_USER              = "archivematica"
SS_API_KEY           = ""                       # sovrascrivibile da CLI / env

TRANSFER_SOURCE_UUID = ""    # UUID Location di tipo Transfer Source nello SS
TRANSFER_TYPE        = "standard"
PROCESSING_CONFIG    = "dataverse_001"  # nome della processing configuration in Archivematica

# Tipi di transfer accettati dall'API Archivematica
VALID_TRANSFER_TYPES = [
    "standard",
    "zipped bag",
    "unzipped bag",
    "dspace",
    "dspace destination only",
    "maildir",
    "TRIM",
    "disk image",
]

# Path locale delle processing configurations XML (lette se accessibili)
PROCESSING_CONFIGS_DIR = (
    "/var/archivematica/sharedDirectory/sharedMicroServiceTasksConfigs/"
    "processingMCPConfigs"
)

POLL_INTERVAL        = 15    # secondi tra un polling e l'altro
UNIT_NOT_FOUND_WAIT    = 20   # secondi di attesa se l'unità non è ancora registrata
UNIT_NOT_FOUND_RETRIES = 2    # numero di tentativi extra in caso di unità non trovata
POLL_MAX_WAIT        = 7200  # secondi massimi di attesa (2 ore)
REQUEST_TIMEOUT      = 30

# ── Utility ───────────────────────────────────────────────────────────────────

def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def load_state(path: str) -> dict:
    p = Path(path)
    if p.exists():
        try:
            return json.loads(p.read_text(encoding="utf-8"))
        except Exception:
            print(f"[ATTENZIONE] File di stato corrotto, si riparte da zero: {path}")
    return {}


def save_state(state: dict, path: str) -> None:
    Path(path).write_text(
        json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8"
    )


def am_headers(api_key: str, user: str) -> dict:
    return {"Authorization": f"ApiKey {user}:{api_key}"}


def ss_headers(api_key: str, user: str) -> dict:
    return {"Authorization": f"ApiKey {user}:{api_key}"}


def get_json(url: str, headers: dict) -> dict:
    r = requests.get(url, headers=headers, timeout=REQUEST_TIMEOUT)
    if not r.ok:
        try:
            body = r.json()
        except Exception:
            body = r.text[:600]
        raise requests.exceptions.HTTPError(
            f"{r.status_code} Client/Server Error for url: {r.url}\n"
            f"        Risposta: {body}",
            response=r,
        )
    return r.json()


# ── Ricerca Transfer Source ───────────────────────────────────────────────────

def list_transfer_sources(ss_url: str, ss_user: str, ss_key: str) -> list[dict]:
    """Elenca le Transfer Source Locations disponibili nello Storage Service."""
    url  = f"{ss_url}/api/v2/location/?purpose=TS"
    data = get_json(url, ss_headers(ss_key, ss_user))
    return data.get("objects", [])


def list_processing_configs() -> list[str]:
    """
    Elenca le processing configuration XML disponibili localmente
    (richiede accesso al filesystem del server Archivematica).

    I file sono nominati "<nome>ProcessingMCP.xml" (es. "dataverse_001ProcessingMCP.xml",
    "automatedProcessingMCP.xml", "defaultProcessingMCP.xml").
    Restituisce i nomi senza il suffisso, es. ["default", "automated", "dataverse_001"].
    """
    p = Path(PROCESSING_CONFIGS_DIR)
    if not p.exists():
        return []
    names = []
    for f in p.glob("*ProcessingMCP.xml"):
        names.append(f.name[:-len("ProcessingMCP.xml")])
    return sorted(names)


def apply_processing_config(doi_path: Path, processing_config: str) -> bool:
    """
    Copia il file <processing_config>.xml da PROCESSING_CONFIGS_DIR nella
    radice del pacchetto come "processingMCP.xml".

    Questo è il meccanismo con cui Archivematica applica una processing
    configuration ai transfer avviati tramite /api/transfer/start_transfer/,
    che NON supporta il parametro "processing_config" nel payload.

    Restituisce True se il file è stato copiato, False se la configurazione
    non è stata trovata (in tal caso si procede con il default di Archivematica).
    """
    src_xml = Path(PROCESSING_CONFIGS_DIR) / f"{processing_config}ProcessingMCP.xml"
    if not src_xml.exists():
        print(
            f"    [AVVISO] Processing config '{src_xml.name}' non trovata "
            f"in {PROCESSING_CONFIGS_DIR}. Verrà usata la configurazione di default "
            f"di Archivematica."
        )
        return False

    dst_xml = doi_path / "processingMCP.xml"
    shutil.copyfile(src_xml, dst_xml)
    print(f"    [debug] processingMCP.xml ('{processing_config}') copiato in {dst_xml}")
    return True


def processing_config_exists(am_url: str, am_user: str, am_key: str,
                              name: str) -> bool | None:
    """
    Verifica se una processing configuration esiste tramite l'API del Dashboard.
    Restituisce True/False, oppure None se la verifica non è possibile
    (es. endpoint non disponibile in questa versione di Archivematica).
    """
    url = f"{am_url}/api/processing-configuration/{name}"
    try:
        r = requests.get(url, headers=am_headers(am_key, am_user),
                         timeout=REQUEST_TIMEOUT)
        if r.status_code == 200:
            return True
        if r.status_code == 404:
            return False
        return None
    except requests.exceptions.RequestException:
        return None


def get_transfer_source_path(ss_url: str, ss_user: str, ss_key: str,
                              location_uuid: str) -> str:
    """
    Recupera il path assoluto di una Transfer Source Location.
    Es. /home/zfed/archivematica_script/DATASET_TRASFERITI
    """
    url  = f"{ss_url}/api/v2/location/{location_uuid}/"
    data = get_json(url, ss_headers(ss_key, ss_user))
    return data.get("path", "").rstrip("/")


# ── Avvio Transfer ────────────────────────────────────────────────────────────

def _encode_path(location_uuid: str, absolute_path: str) -> str:
    """
    Archivematica vuole il path come base64(<location_uuid>:<path_assoluto>).
    """
    raw = f"{location_uuid}:{absolute_path}"
    return base64.b64encode(raw.encode()).decode()


def start_transfer(
    am_url: str, am_user: str, am_key: str,
    ss_url: str, ss_user: str, ss_key: str,
    transfer_name: str,
    transfer_rel_path: str,
    transfer_source_uuid: str,
    transfer_type: str = "standard",
    processing_config: str = "default",
) -> str:
    """
    Avvia un Transfer tramite la Dashboard API.
    Restituisce il transfer_uuid.

    transfer_rel_path: path relativo alla radice della Transfer Source,
                       es. "doi_10.13130_RD_UNIMI_ABC/v1.0"
    """
    # 1. Recupera il path assoluto della location dallo Storage Service
    source_base = get_transfer_source_path(ss_url, ss_user, ss_key,
                                            transfer_source_uuid)
    if not source_base:
        raise RuntimeError(
            f"Impossibile recuperare il path della Transfer Source "
            f"{transfer_source_uuid}"
        )

    # 2. Path assoluto completo della cartella da trasferire
    full_path = f"{source_base}/{transfer_rel_path}"
    encoded   = _encode_path(transfer_source_uuid, full_path)

    print(f"    [debug] Transfer Source base : {source_base}")
    print(f"    [debug] Full path            : {full_path}")
    print(f"    [debug] Encoded path         : {encoded}")

    # 3. Chiamata API — usa form-urlencoded (non JSON, non multipart)
    url = f"{am_url}/api/transfer/start_transfer/"
    # NOTA: /api/transfer/start_transfer/ non supporta il parametro
    # "processing_config" — la configurazione viene applicata copiando
    # processingMCP.xml nella cartella prima della chiamata (vedi
    # apply_processing_config), non tramite questo payload.
    payload = {
        "name":      transfer_name,
        "type":      transfer_type,
        "accession": "",
        "paths[]":   encoded,
        "row_ids[]": "",
    }
    h = am_headers(am_key, am_user)
    r = requests.post(url, headers=h, data=payload, timeout=REQUEST_TIMEOUT)

    if not r.ok:
        try:
            body = r.json()
        except Exception:
            body = r.text[:600]
        raise RuntimeError(f"start_transfer HTTP {r.status_code}: {body}")

    data = r.json()
    if data.get("message") != "Copy successful.":
        raise RuntimeError(f"Avvio transfer fallito: {data}")

    print(f"    [debug] Risposta start_transfer: {data}")

    # Cerca l'UUID nella risposta: prima in "uuid", poi nel path
    import re
    uuid_pattern = re.compile(
        r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}",
        re.IGNORECASE,
    )

    # Alcuni Archivematica restituiscono direttamente "uuid"
    if data.get("uuid") and uuid_pattern.match(str(data["uuid"])):
        return data["uuid"]

    # Altrimenti estraiamo dal path restituito
    raw_path = data.get("path", "")
    matches  = uuid_pattern.findall(raw_path)
    if matches:
        return matches[-1]   # l'ultimo UUID nel path è quello del transfer

    raise RuntimeError(
        f"Impossibile estrarre UUID transfer dalla risposta: {data}"
    )


# ── Polling Transfer ──────────────────────────────────────────────────────────

def _is_unit_not_found_yet(exc: Exception) -> bool:
    """
    Restituisce True se l'errore HTTP indica che Archivematica non ha ancora
    registrato l'unità (transfer/SIP appena avviato), errore tipicamente
    transitorio nei primi secondi dopo start_transfer:

      400 {'error': True, 'message': 'Unable to determine the status of the unit ...'}
    """
    resp = getattr(exc, "response", None)
    if resp is None or resp.status_code != 400:
        return False
    try:
        body = resp.json()
    except Exception:
        return False
    return "unable to determine the status" in str(body.get("message", "")).lower()


def poll_transfer(
    am_url: str, am_user: str, am_key: str,
    transfer_uuid: str,
    poll_interval: int = POLL_INTERVAL,
) -> tuple[str, str]:
    """
    Attende il completamento del Transfer.
    Restituisce (status_finale, sip_uuid).
    """
    url     = f"{am_url}/api/transfer/status/{transfer_uuid}/"
    headers = am_headers(am_key, am_user)
    elapsed = 0
    not_found_retries = 0

    while elapsed < POLL_MAX_WAIT:
        try:
            data = get_json(url, headers)
        except requests.exceptions.HTTPError as e:
            if _is_unit_not_found_yet(e):
                not_found_retries += 1
                if not_found_retries > UNIT_NOT_FOUND_RETRIES:
                    raise RuntimeError(
                        f"Transfer {transfer_uuid}: unità non trovata da "
                        f"Archivematica dopo {not_found_retries} tentativi."
                    ) from e
                print(f"    [transfer] unità non ancora registrata da Archivematica "
                      f"({not_found_retries}/{UNIT_NOT_FOUND_RETRIES}), "
                      f"attendo {UNIT_NOT_FOUND_WAIT}s ...")
                time.sleep(UNIT_NOT_FOUND_WAIT)
                elapsed += UNIT_NOT_FOUND_WAIT
                continue
            raise

        status = data.get("status", "").upper()
        sip    = data.get("sip_uuid", "")

        if status == "COMPLETE":
            return status, sip
        if status in ("FAILED", "REJECTED", "USER_INPUT"):
            raise RuntimeError(
                f"Transfer {transfer_uuid} terminato con stato: {status}"
            )

        print(f"    [transfer] stato: {status} — attendo {poll_interval}s ...")
        time.sleep(poll_interval)
        elapsed += poll_interval

    raise TimeoutError(
        f"Transfer {transfer_uuid} non completato entro {POLL_MAX_WAIT}s"
    )


# ── Polling Ingest ────────────────────────────────────────────────────────────

def poll_ingest(
    am_url: str, am_user: str, am_key: str,
    sip_uuid: str,
    poll_interval: int = POLL_INTERVAL,
) -> tuple[str, str]:
    """
    Attende il completamento dell'Ingest (SIP → AIP).
    Restituisce (status_finale, aip_uuid).
    """
    url     = f"{am_url}/api/ingest/status/{sip_uuid}/"
    headers = am_headers(am_key, am_user)
    elapsed = 0
    not_found_retries = 0

    while elapsed < POLL_MAX_WAIT:
        try:
            data = get_json(url, headers)
        except requests.exceptions.HTTPError as e:
            if _is_unit_not_found_yet(e):
                not_found_retries += 1
                if not_found_retries > UNIT_NOT_FOUND_RETRIES:
                    raise RuntimeError(
                        f"Ingest {sip_uuid}: unità non trovata da "
                        f"Archivematica dopo {not_found_retries} tentativi."
                    ) from e
                print(f"    [ingest]   unità non ancora registrata da Archivematica "
                      f"({not_found_retries}/{UNIT_NOT_FOUND_RETRIES}), "
                      f"attendo {UNIT_NOT_FOUND_WAIT}s ...")
                time.sleep(UNIT_NOT_FOUND_WAIT)
                elapsed += UNIT_NOT_FOUND_WAIT
                continue
            raise

        status = data.get("status", "").upper()

        if status == "COMPLETE":
            aip_uuid = data.get("uuid", sip_uuid)
            return status, aip_uuid
        if status in ("FAILED", "REJECTED", "USER_INPUT"):
            raise RuntimeError(
                f"Ingest {sip_uuid} terminato con stato: {status}"
            )

        print(f"    [ingest]   stato: {status} — attendo {poll_interval}s ...")
        time.sleep(poll_interval)
        elapsed += poll_interval

    raise TimeoutError(
        f"Ingest {sip_uuid} non completato entro {POLL_MAX_WAIT}s"
    )


# ── Raccolta pacchetti DOI da trasferire ──────────────────────────────────────

def collect_doi_packages(source_dir: Path) -> list[tuple[str, Path]]:
    """
    Scansiona DATASET_TRASFERITI e restituisce una lista di tuple:
      (doi_folder_name, doi_path)

    Ogni cartella DOI contiene TUTTE le sue versioni e viene trasferita
    in Archivematica come un UNICO pacchetto:

      <doi_folder>/
        objects/
          v1.0/
            objects/
            metadata/
          v2.0/
            ...
        metadata/
          metadata.csv
    """
    packages = []
    if not source_dir.exists():
        print(f"[ERRORE] Directory sorgente non trovata: {source_dir}")
        sys.exit(1)

    for doi_dir in sorted(source_dir.iterdir()):
        if not doi_dir.is_dir():
            continue

        objects_root = doi_dir / "objects"
        if not objects_root.is_dir():
            continue

        # Verifica che ci sia almeno una versione vX.Y
        has_version = any(
            d.is_dir() and d.name.startswith("v")
            for d in objects_root.iterdir()
        )
        if not has_version:
            continue

        packages.append((doi_dir.name, doi_dir))

    return packages


# ── Elaborazione singola versione ─────────────────────────────────────────────

def make_transfer_name(doi_folder: str) -> str:
    """
    Genera il nome del pacchetto nel formato:
      AIP-<YYYYMMDD>-<DOI>

    doi_folder è già sanitizzato per il filesystem (es. doi_10.13130_RD_UNIMI_ABC),
    quindi viene usato direttamente come componente <DOI>.
    """
    date_str = datetime.now().strftime("%Y%m%d")
    return f"AIP-{date_str}-{doi_folder}"


def process_doi_package(
    doi_folder: str,
    doi_path: Path,
    state: dict,
    args,
) -> dict:
    """
    Esegue (o salta) il transfer + ingest per un intero pacchetto DOI
    (tutte le versioni incluse, così come sono organizzate nella cartella).
    Aggiorna e restituisce il record di stato per questo DOI.
    """
    state_key = doi_folder
    record    = state.get(state_key, {})

    # ── Skip se già completato ────────────────────────────────────────────────
    if record.get("status") == "ingested":
        print(f"  [SKIP] {state_key} — già ingestato (AIP: {record.get('aip_uuid')})")
        return record

    print(f"  → Elaborazione: {state_key}")

    # Riusa il nome generato in precedenza se il transfer è già stato avviato,
    # altrimenti generane uno nuovo con la data odierna
    transfer_name     = record.get("transfer_name") or make_transfer_name(doi_folder)
    transfer_rel_path = doi_folder

    record["transfer_name"] = transfer_name
    record["status"]        = "in_progress"
    record.setdefault("error", None)

    try:
        # ── 1. Avvio Transfer ─────────────────────────────────────────────────
        if not record.get("transfer_uuid"):
            # Applica la processing configuration scelta copiando
            # processingMCP.xml nella radice del pacchetto
            apply_processing_config(doi_path, args.processing_config)

            print(f"    Avvio transfer '{transfer_name}' ...")
            transfer_uuid = start_transfer(
                am_url               = args.am_url,
                am_user              = args.am_user,
                am_key               = args.am_apikey,
                ss_url               = args.ss_url,
                ss_user              = args.ss_user,
                ss_key               = args.ss_apikey,
                transfer_name        = transfer_name,
                transfer_rel_path    = transfer_rel_path,
                transfer_source_uuid = args.transfer_source,
                transfer_type        = args.transfer_type,
                processing_config    = args.processing_config,
            )
            record["transfer_uuid"] = transfer_uuid
            print(f"    Transfer UUID: {transfer_uuid}")
        else:
            transfer_uuid = record["transfer_uuid"]
            print(f"    Transfer UUID (ripresa): {transfer_uuid}")

        # ── 2. Polling Transfer ───────────────────────────────────────────────
        if not record.get("sip_uuid"):
            print(f"    Attendo completamento transfer ...")
            _, sip_uuid = poll_transfer(
                args.am_url, args.am_user, args.am_apikey,
                transfer_uuid,
                poll_interval=args.poll_interval,
            )
            record["sip_uuid"] = sip_uuid
            record["status"]   = "transferred"
            print(f"    SIP UUID: {sip_uuid}")
        else:
            sip_uuid = record["sip_uuid"]
            print(f"    SIP UUID (ripresa): {sip_uuid}")

        # ── 3. Polling Ingest ─────────────────────────────────────────────────
        print(f"    Attendo completamento ingest ...")
        _, aip_uuid = poll_ingest(
            args.am_url, args.am_user, args.am_apikey,
            sip_uuid,
            poll_interval=args.poll_interval,
        )
        record["aip_uuid"]     = aip_uuid
        record["status"]       = "ingested"
        record["completed_at"] = now_iso()
        record["error"]        = None
        print(f"    ✓ Ingest completato — AIP UUID: {aip_uuid}")

    except Exception as e:
        record["status"] = "failed"
        record["error"]  = str(e)
        print(f"    [ERRORE] {e}")

    return record


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(
        description="Transfer e ingest in Archivematica dei dataset in DATASET_TRASFERITI"
    )

    parser.add_argument("--source",     default=None,
        help=(
            "Directory sorgente dei dataset. Se omesso, viene usato il path "
            "della Transfer Source Location (--transfer-source) recuperato "
            "dallo Storage Service — questo è il comportamento consigliato, "
            "perché garantisce che i dataset si trovino dove Archivematica "
            "se li aspetta per il transfer."
        ))
    parser.add_argument("--state-file", default=STATE_FILE,
        help=f"File JSON di tracciamento (default: {STATE_FILE})")

    # Dashboard
    parser.add_argument("--am-url",    default=AM_URL,
        help=f"URL Dashboard (default: {AM_URL})")
    parser.add_argument("--am-user",   default=AM_USER,
        help=f"Utente Dashboard (default: {AM_USER})")
    parser.add_argument("--am-apikey", default="",
        help="API key Dashboard (o variabile d'ambiente AM_API_KEY)")

    # Storage Service
    parser.add_argument("--ss-url",    default=SS_URL,
        help=f"URL Storage Service (default: {SS_URL})")
    parser.add_argument("--ss-user",   default=SS_USER,
        help=f"Utente Storage Service (default: {SS_USER})")
    parser.add_argument("--ss-apikey", default="",
        help="API key Storage Service (o variabile d'ambiente SS_API_KEY)")

    # Transfer
    parser.add_argument("--transfer-source", default="",
        help="UUID della Transfer Source Location nello Storage Service")
    parser.add_argument("--transfer-type",      default=TRANSFER_TYPE,
        choices=VALID_TRANSFER_TYPES,
        help=f"Tipo di transfer (default: {TRANSFER_TYPE}). "
             f"Valori possibili: {', '.join(VALID_TRANSFER_TYPES)}")
    parser.add_argument("--processing-config",  default=PROCESSING_CONFIG,
        help=f"Nome della processing configuration / profilo di processing "
             f"(default: {PROCESSING_CONFIG}). Usa --list-processing-configs "
             f"per vedere quelli disponibili.")

    # Utility
    parser.add_argument("--list-sources",  action="store_true",
        help="Elenca le Transfer Source Locations disponibili ed esce")
    parser.add_argument("--list-processing-configs", action="store_true",
        help="Elenca le processing configuration disponibili (richiede accesso "
             "al filesystem del server Archivematica) ed esce")
    parser.add_argument("--poll-interval", type=int, default=POLL_INTERVAL,
        help=f"Secondi tra un polling e l'altro (default: {POLL_INTERVAL})")
    parser.add_argument("--dry-run",       action="store_true",
        help="Mostra cosa verrebbe fatto senza eseguire nulla")

    args = parser.parse_args()

    # Risolvi credenziali da env se non passate da CLI
    if not args.am_apikey:
        args.am_apikey = os.environ.get("AM_API_KEY", AM_API_KEY)
    if not args.ss_apikey:
        args.ss_apikey = os.environ.get("SS_API_KEY", SS_API_KEY)
    if not args.transfer_source:
        args.transfer_source = os.environ.get(
            "AM_TRANSFER_SOURCE_UUID", TRANSFER_SOURCE_UUID
        )

    # ── Modalità --list-processing-configs ────────────────────────────────────
    if args.list_processing_configs:
        configs = list_processing_configs()
        if configs:
            print("Processing configuration disponibili:\n")
            for c in configs:
                marker = "  (default)" if c == PROCESSING_CONFIG else ""
                print(f"  {c}{marker}")
        else:
            print(
                f"[ATTENZIONE] Impossibile leggere {PROCESSING_CONFIGS_DIR}\n"
                f"Verifica che lo script sia eseguito sullo stesso host di "
                f"Archivematica e di avere i permessi di lettura.\n\n"
                f"Configurazioni note di default: default, automated, dataverse_001"
            )
        sys.exit(0)

    # ── Modalità --list-sources ───────────────────────────────────────────────
    if args.list_sources:
        print("Transfer Source Locations disponibili:\n")
        try:
            sources = list_transfer_sources(
                args.ss_url, args.ss_user, args.ss_apikey
            )
            if not sources:
                print("  (nessuna trovata)")
            for s in sources:
                print(f"  UUID : {s['uuid']}")
                print(f"  Path : {s.get('path', '?')}")
                print(f"  Desc : {s.get('description', '')}\n")
        except Exception as e:
            print(f"[ERRORE] {e}")
        sys.exit(0)

    # ── Validazioni ───────────────────────────────────────────────────────────
    missing = []
    if not args.am_apikey:
        missing.append("--am-apikey / AM_API_KEY")
    if not args.transfer_source:
        missing.append("--transfer-source / AM_TRANSFER_SOURCE_UUID")
    if missing:
        print("[ERRORE] Parametri obbligatori mancanti:")
        for m in missing:
            print(f"  • {m}")
        print("\nUsa --list-sources per trovare l'UUID della Transfer Source.")
        sys.exit(1)

    # ── Risoluzione directory sorgente ────────────────────────────────────────
    if args.source:
        source_dir = Path(args.source)
        source_origin = "specificata con --source"
    else:
        ts_path = get_transfer_source_path(
            args.ss_url, args.ss_user, args.ss_apikey, args.transfer_source
        )
        if not ts_path:
            print(
                f"[ERRORE] Impossibile determinare il path della Transfer Source "
                f"Location {args.transfer_source} dallo Storage Service.\n"
                f"Specifica esplicitamente --source <directory>."
            )
            sys.exit(1)
        source_dir = Path(ts_path)
        source_origin = f"da Transfer Source Location {args.transfer_source}"

    # ── Raccolta pacchetti DOI ─────────────────────────────────────────────────
    packages = collect_doi_packages(source_dir)

    if not packages:
        print(f"[INFO] Nessun pacchetto DOI trovato in {source_dir} ({source_origin})")
        sys.exit(0)

    state = load_state(args.state_file)

    # Verifica (best-effort) che la processing configuration esista
    pc_exists = processing_config_exists(
        args.am_url, args.am_user, args.am_apikey, args.processing_config
    )
    if pc_exists is False:
        print(
            f"[ERRORE] La processing configuration '{args.processing_config}' "
            f"non esiste in Archivematica.\n"
            f"Usa --list-processing-configs per vedere quelle disponibili."
        )
        sys.exit(1)
    elif pc_exists is None:
        print(
            f"[AVVISO] Impossibile verificare l'esistenza della processing "
            f"configuration '{args.processing_config}' (endpoint non disponibile). "
            f"Si procede comunque."
        )

    def mask(k):
        return ("*" * 6 + k[-4:]) if len(k) > 4 else ("(non impostata)" if not k else "****")

    print(f"{'='*64}")
    print(f"  Archivematica Ingest — trasferimento automatico dataset")
    print(f"  Sorgente      : {source_dir.resolve()}  ({source_origin})")
    print(f"  Pacchetti DOI : {len(packages)}")
    print(f"  Dashboard     : {args.am_url}  (utente: {args.am_user}, key: {mask(args.am_apikey)})")
    print(f"  Storage Svc   : {args.ss_url}  (utente: {args.ss_user}, key: {mask(args.ss_apikey)})")
    print(f"  Transfer Src  : {args.transfer_source}")
    print(f"  Transfer type : {args.transfer_type}")
    print(f"  Processing cfg: {args.processing_config}")
    print(f"  File di stato : {args.state_file}")
    if args.dry_run:
        print(f"  MODALITÀ      : DRY-RUN (nessuna operazione verrà eseguita)")
    print(f"{'='*64}\n")

    # ── Elaborazione ─────────────────────────────────────────────────────────
    stats = {"ok": 0, "skip": 0, "failed": 0}

    for i, (doi_folder, doi_path) in enumerate(packages, 1):
        state_key = doi_folder
        print(f"[{i}/{len(packages)}] {state_key}")

        if args.dry_run:
            if state.get(state_key, {}).get("status") == "ingested":
                print(f"  [DRY-RUN] SKIP — già ingestato")
                stats["skip"] += 1
            else:
                tname = state.get(state_key, {}).get("transfer_name") \
                        or make_transfer_name(doi_folder)
                print(f"  [DRY-RUN] Verrebbe avviato transfer+ingest per: {doi_path}")
                print(f"  [DRY-RUN] Nome pacchetto: {tname}")
            print()
            continue

        record = process_doi_package(doi_folder, doi_path, state, args)
        state[state_key] = record
        save_state(state, args.state_file)

        if record["status"] == "ingested":
            stats["ok"] += 1
        elif record["status"] == "failed":
            stats["failed"] += 1
        else:
            stats["skip"] += 1

        print()

    # ── Riepilogo ─────────────────────────────────────────────────────────────
    print(f"{'='*64}")
    print(f"  RIEPILOGO FINALE")
    print(f"  Completati   : {stats['ok']}")
    print(f"  Già presenti : {stats['skip']}")
    print(f"  Errori       : {stats['failed']}")
    print(f"  File stato   : {Path(args.state_file).resolve()}")
    print(f"{'='*64}")

    if stats["failed"] > 0:
        sys.exit(1)


if __name__ == "__main__":
    main()
