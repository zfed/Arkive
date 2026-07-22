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
import logging
import json
import os
import shutil
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import requests

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
            key   = key.strip()
            value = value.strip().strip('"').strip("'")
            if key and key not in os.environ:
                os.environ[key] = value

_load_dotenv()

# ── Logging ───────────────────────────────────────────────────────────────────

LOG_DIR = os.environ.get("LOG_DIR", "logs")


def setup_logging(script_name: str = "archivematica_ingest") -> logging.Logger:
    """
    Configura il logging su file e su console contemporaneamente.
    Il file di log viene creato in LOG_DIR con nome:
      <script_name>_<YYYYMMDD>_<HHMMSS>.log
    LOG_DIR e' configurabile nel .env (default: logs/).
    """
    log_dir = Path(LOG_DIR)
    log_dir.mkdir(parents=True, exist_ok=True)

    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    log_file  = log_dir / f"{script_name}_{timestamp}.log"

    logger = logging.getLogger(script_name)
    logger.setLevel(logging.DEBUG)
    logger.handlers.clear()

    fh = logging.FileHandler(log_file, encoding="utf-8")
    fh.setLevel(logging.DEBUG)
    fh.setFormatter(logging.Formatter("%(asctime)s  %(message)s",
                                       datefmt="%Y-%m-%d %H:%M:%S"))

    ch = logging.StreamHandler(sys.stdout)
    ch.setLevel(logging.DEBUG)
    ch.setFormatter(logging.Formatter("%(message)s"))

    logger.addHandler(fh)
    logger.addHandler(ch)

    import builtins
    _original_print = builtins.print
    def _logging_print(*args, **kwargs):
        sep  = kwargs.get("sep", " ")
        text = sep.join(str(a) for a in args)
        logger.info(text)
    builtins.print = _logging_print

    logger.info(f"Log salvato in: {log_file}")
    return logger


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

# Verifica certificato SSL (False per certificati self-signed)
SSL_VERIFY           = True

# Valore attivo — impostato da main() dopo aver risolto le opzioni CLI
_ssl_verify: bool = True

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

# Copia locale versionata del processing config XML (RACCOMANDATO).
# Se impostato (via --processing-mcp-path o AM_PROCESSING_MCP_PATH nel .env),
# lo script usa questo file e NON dipende più dalla shared directory di
# Archivematica, i cui permessi possono cambiare con update/riavvii.
# Esempio: /home/zfed/arkive/config/dataverse_001ProcessingMCP.xml
PROCESSING_MCP_PATH = ""

POLL_INTERVAL        = 15    # secondi tra un polling e l'altro
UNIT_NOT_FOUND_WAIT    = 20   # secondi di attesa se l'unità non è ancora registrata
UNIT_NOT_FOUND_RETRIES = 2    # numero di tentativi extra in caso di unità non trovata
APPROVE_TRANSFER_WAIT  = 10   # secondi di attesa prima di tentare l'approve automatico
POLL_MAX_WAIT        = 7200  # secondi massimi di attesa (2 ore)

# Timeout delle chiamate API (secondi). Configurabile con AM_API_TIMEOUT nel
# .env o con --api-timeout da CLI. Il default è generoso: sotto carico
# (molti ingest in coda) o con dataset molto grandi, Archivematica può
# impiegare più di 30s a rispondere anche a chiamate semplici.
REQUEST_TIMEOUT      = int(os.environ.get("AM_API_TIMEOUT", "120"))

# Errori di rete transitori (ReadTimeout, ConnectionError): un timeout NON
# significa che l'operazione sia fallita lato Archivematica — significa solo
# che la risposta non è arrivata in tempo. Le GET vengono ritentate.
TRANSIENT_RETRIES    = 3     # tentativi totali per le GET
TRANSIENT_RETRY_WAIT = 15    # secondi di attesa tra i tentativi

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
    """
    GET con retry automatico sugli errori di rete transitori
    (ReadTimeout, ConnectionError): sotto carico Archivematica può
    rispondere lentamente, ma un timeout di lettura NON significa che
    l'unità interrogata sia fallita. Prima di rinunciare, lo script
    ritenta TRANSIENT_RETRIES volte con TRANSIENT_RETRY_WAIT s di pausa.
    """
    last_err: Exception | None = None
    for attempt in range(1, TRANSIENT_RETRIES + 1):
        try:
            r = requests.get(url, headers=headers, timeout=REQUEST_TIMEOUT,
                             verify=_ssl_verify)
            break
        except (requests.exceptions.Timeout,
                requests.exceptions.ConnectionError) as e:
            last_err = e
            if attempt < TRANSIENT_RETRIES:
                print(f"    [retry] Errore di rete transitorio ({e}) — "
                      f"tentativo {attempt + 1}/{TRANSIENT_RETRIES} "
                      f"tra {TRANSIENT_RETRY_WAIT}s ...")
                time.sleep(TRANSIENT_RETRY_WAIT)
    else:
        raise RuntimeError(
            f"Errore di rete persistente dopo {TRANSIENT_RETRIES} tentativi "
            f"su {url}: {last_err}"
        ) from last_err

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
    Restituisce None se la cartella non è accessibile (permessi insufficienti).
    """
    p = Path(PROCESSING_CONFIGS_DIR)
    try:
        if not p.exists():
            return []
        names = []
        for f in p.glob("*ProcessingMCP.xml"):
            names.append(f.name[:-len("ProcessingMCP.xml")])
        return sorted(names)
    except PermissionError:
        return None


def resolve_processing_mcp_source(processing_mcp_path: str,
                                  processing_config: str) -> Path:
    """
    Determina il file XML sorgente della processing configuration.

    Ordine di risoluzione:
      1. Copia locale versionata (--processing-mcp-path / AM_PROCESSING_MCP_PATH)
         — RACCOMANDATO: nessuna dipendenza dalla shared directory di
         Archivematica, i cui permessi possono cambiare con update/riavvii.
      2. Fallback legacy: <PROCESSING_CONFIGS_DIR>/<nome>ProcessingMCP.xml

    Restituisce il Path del sorgente (senza verificarne la leggibilità:
    la verifica fail-fast è in check_processing_mcp_source()).
    """
    if processing_mcp_path:
        return Path(processing_mcp_path)
    return Path(PROCESSING_CONFIGS_DIR) / f"{processing_config}ProcessingMCP.xml"


def check_processing_mcp_source(src_xml: Path, ignore_missing: bool) -> bool:
    """
    Verifica FAIL-FAST (all'avvio, prima di elaborare qualunque pacchetto)
    che il processing config XML sorgente esista e sia leggibile.

    Un pacchetto trasferito senza processingMCP.xml viene processato con la
    configurazione di default di Archivematica (es. con scansione virus),
    e il problema emerge solo a ingest avviato — molto più costoso da
    diagnosticare. Meglio fermarsi subito.

    Restituisce True se il file è utilizzabile, False se manca ma l'utente
    ha esplicitamente scelto di proseguire (--ignore-missing-processing-config).
    In tutti gli altri casi termina lo script con exit code 1.
    """
    problema = None
    try:
        if not src_xml.is_file():
            problema = "file non trovato"
        elif not os.access(src_xml, os.R_OK):
            problema = "Permission denied"
    except PermissionError:
        problema = "Permission denied su una directory del percorso"

    if problema is None:
        return True

    if ignore_missing:
        print(
            f"[AVVISO] Processing config non utilizzabile: {src_xml} ({problema}).\n"
            f"         Si prosegue SENZA processingMCP.xml come richiesto "
            f"(--ignore-missing-processing-config): i transfer useranno la\n"
            f"         configurazione di DEFAULT di Archivematica."
        )
        return False

    print(
        f"[ERRORE] Impossibile leggere il processing config: {src_xml} ({problema}).\n"
        f"         Interrompo PRIMA di avviare qualunque transfer: senza\n"
        f"         processingMCP.xml i pacchetti verrebbero processati con la\n"
        f"         configurazione di default di Archivematica.\n"
        f"\n"
        f"Soluzioni:\n"
        f"  1. (RACCOMANDATO) Usa una copia locale versionata del file:\n"
        f"       mkdir -p ~/arkive/config\n"
        f"       sudo cp {PROCESSING_CONFIGS_DIR}/<nome>ProcessingMCP.xml ~/arkive/config/\n"
        f"       sudo chown $(whoami): ~/arkive/config/<nome>ProcessingMCP.xml\n"
        f"     e impostala con --processing-mcp-path oppure nel .env:\n"
        f"       AM_PROCESSING_MCP_PATH=/home/<utente>/arkive/config/<nome>ProcessingMCP.xml\n"
        f"  2. Sistema i permessi sulla shared directory di Archivematica\n"
        f"     (fragile: possono cambiare a ogni update/riavvio).\n"
        f"  3. Prosegui consapevolmente senza processing config:\n"
        f"       --ignore-missing-processing-config  (SCONSIGLIATO)"
    )
    sys.exit(1)


def apply_processing_config(doi_path: Path, src_xml: Path,
                            retries: int = 3, retry_wait: int = 2) -> None:
    """
    Copia il processing config XML sorgente nella radice del pacchetto
    come "processingMCP.xml", e VERIFICA che la copia sia avvenuta.

    Questo è il meccanismo con cui Archivematica applica una processing
    configuration ai transfer avviati tramite /api/transfer/start_transfer/,
    che NON supporta il parametro "processing_config" nel payload.

    Robustezze per Transfer Source su mount Windows/Dropbox (WSL drvfs):
      - se la destinazione esiste già e non è scrivibile (attributo
        read-only, copia manuale precedente con sudo), viene resa
        scrivibile e rimossa prima della copia;
      - in caso di PermissionError/OSError (es. lock transitorio del
        client Dropbox durante la sincronizzazione) la copia viene
        ritentata fino a `retries` volte con `retry_wait` secondi di pausa.

    Solleva RuntimeError se tutti i tentativi falliscono: il pacchetto
    viene marcato "failed" nel file di stato e NESSUN transfer viene
    avviato (mai più pacchetti silenziosamente non conformi).
    """
    dst_xml = doi_path / "processingMCP.xml"
    last_err: Exception | None = None

    for attempt in range(1, retries + 1):
        try:
            # Rimuovi un'eventuale destinazione preesistente non scrivibile
            if dst_xml.exists():
                try:
                    os.chmod(dst_xml, 0o644)
                except OSError:
                    pass  # best-effort: su drvfs può non essere supportato
                try:
                    dst_xml.unlink()
                except OSError:
                    pass  # se la unlink fallisce, ci prova comunque copyfile
            shutil.copyfile(src_xml, dst_xml)
            last_err = None
            break
        except (PermissionError, OSError) as e:
            last_err = e
            if attempt < retries:
                print(
                    f"    [retry] Copia processingMCP.xml fallita ({e}) — "
                    f"tentativo {attempt + 1}/{retries} tra {retry_wait}s "
                    f"(possibile lock transitorio, es. sync Dropbox) ..."
                )
                time.sleep(retry_wait)

    if last_err is not None:
        raise RuntimeError(
            f"Copia di {src_xml} in {dst_xml} fallita dopo {retries} tentativi: "
            f"{last_err}. Transfer NON avviato per questo pacchetto. "
            f"Verifica permessi/attributi della destinazione "
            f"(ls -l {dst_xml}) ed eventuali lock del client di sincronizzazione."
        ) from last_err

    # Verifica post-copia: il file deve esistere davvero nella radice
    if not dst_xml.is_file() or dst_xml.stat().st_size == 0:
        raise RuntimeError(
            f"Verifica post-copia fallita: {dst_xml} assente o vuoto. "
            f"Transfer NON avviato per questo pacchetto."
        )
    print(f"    [debug] processingMCP.xml copiato e verificato in {dst_xml}")


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
                         timeout=REQUEST_TIMEOUT, verify=_ssl_verify)
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

    transfer_rel_path: path della cartella da trasferire. Puo' essere ASSOLUTO
                       (purche' interno alla Transfer Source Location, es.
                       "/mnt/e/.../TRANSFER_SOURCE/PILOTA/doi_10.13130_...")
                       oppure RELATIVO alla radice della location
                       (es. "doi_10.13130_RD_UNIMI_ABC").
    """
    # 1. Recupera il path assoluto della location dallo Storage Service
    source_base = get_transfer_source_path(ss_url, ss_user, ss_key,
                                            transfer_source_uuid)
    if not source_base:
        raise RuntimeError(
            f"Impossibile recuperare il path della Transfer Source "
            f"{transfer_source_uuid}"
        )

    # 2. Path assoluto completo della cartella da trasferire.
    #    transfer_rel_path puo' essere:
    #      - ASSOLUTO: necessario quando i pacchetti stanno in una sottocartella
    #        della location (es. .../TRANSFER_SOURCE/PILOTA/doi_x). Si verifica
    #        che sia effettivamente interno alla location, altrimenti lo Storage
    #        Service non potrebbe risolverlo.
    #      - RELATIVO alla radice della Transfer Source (comportamento storico).
    _p    = Path(transfer_rel_path)
    _base = Path(source_base)
    if _p.is_absolute():
        try:
            _p.relative_to(_base)
        except ValueError:
            raise RuntimeError(
                f"Il pacchetto {_p} non si trova dentro la Transfer Source "
                f"Location {_base}: Archivematica non potrebbe raggiungerlo. "
                f"Spostare i pacchetti dentro la location oppure usare la "
                f"location corretta con --transfer-source."
            )
        full_path = str(_p)
    else:
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
    try:
        r = requests.post(url, headers=h, data=payload,
                          timeout=REQUEST_TIMEOUT, verify=_ssl_verify)
    except (requests.exceptions.Timeout,
            requests.exceptions.ConnectionError) as e:
        # ATTENZIONE: il timeout della RISPOSTA non implica che la RICHIESTA
        # non sia arrivata. Con dataset grandi la copia nella watched
        # directory può superare il timeout: il transfer parte lato
        # Archivematica mentre lo script non riceve risposta. Dichiarare
        # subito "failed" produrrebbe un transfer zombie in Dashboard e un
        # futuro DUPLICATO al retry. Prima di rinunciare, cerchiamo il
        # transfer per nome tra quelli in attesa di approvazione.
        print(f"    [AVVISO] start_transfer in timeout ({e}) — la richiesta "
              f"potrebbe comunque essere arrivata: cerco il transfer "
              f"'{transfer_name}' tra quelli in attesa ...")
        uuid = find_transfer_uuid_by_name(am_url, am_user, am_key,
                                          transfer_name)
        if uuid:
            print(f"    [riaggancio] Transfer trovato in Archivematica "
                  f"(UUID: {uuid}) — proseguo normalmente.")
            return uuid
        raise RuntimeError(
            f"start_transfer in timeout e transfer '{transfer_name}' non "
            f"trovato tra quelli in attesa. Se in Dashboard compare un "
            f"transfer con questo nome, approvarlo/gestirlo manualmente e "
            f"riconciliare con riconcilia_stato.py; altrimenti rilanciare "
            f"lo script."
        ) from e

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

def find_transfer_uuid_by_name(am_url: str, am_user: str, am_key: str,
                               transfer_name: str,
                               tries: int = 4, wait: int = 15) -> str | None:
    """
    Cerca un transfer per nome tra quelli in attesa di approvazione
    (/api/transfer/unapproved/) e ne estrae l'UUID dal nome della
    directory (Archivematica appende -<uuid> al nome del pacchetto
    nella watched directory).

    Usato per RIAGGANCIARE un transfer quando start_transfer va in
    timeout ma la richiesta è comunque arrivata ad Archivematica.
    Restituisce l'UUID se trovato, None altrimenti.
    """
    import re
    uuid_pattern = re.compile(
        r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}",
        re.IGNORECASE,
    )
    h = am_headers(am_key, am_user)
    for attempt in range(1, tries + 1):
        print(f"    [riaggancio] Attendo {wait}s prima della ricerca "
              f"(tentativo {attempt}/{tries}) ...")
        time.sleep(wait)
        try:
            r = requests.get(f"{am_url}/api/transfer/unapproved/",
                             headers=h, timeout=REQUEST_TIMEOUT,
                             verify=_ssl_verify)
            if not r.ok:
                continue
            for t in r.json().get("results", []):
                directory = t.get("directory", "")
                if transfer_name in directory:
                    found = uuid_pattern.findall(directory)
                    if found:
                        return found[-1]
        except requests.exceptions.RequestException:
            continue
    return None


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


# ── Approve Transfer automatico ───────────────────────────────────────────────

def approve_transfer(
    am_url: str, am_user: str, am_key: str,
    transfer_name: str,
    transfer_type: str = "standard",
) -> bool:
    """
    Approva automaticamente un transfer in attesa tramite l'API Archivematica.

    Archivematica aggiunge un UUID al nome nella watched directory
    (es. AIP-20260619-doi_...-<uuid>/), quindi recupera prima i transfer
    in attesa tramite /api/transfer/unapproved/ e trova quello corrispondente
    al nome del pacchetto, poi lo approva con /api/transfer/approve/.
    """
    h = am_headers(am_key, am_user)
    max_retries = 5

    for attempt in range(1, max_retries + 1):
        # Attendi che il transfer sia visibile nella coda
        wait = APPROVE_TRANSFER_WAIT if attempt == 1 else APPROVE_TRANSFER_WAIT * 2
        print(f"    [approve] Attendo {wait}s prima di cercare il transfer "
              f"in attesa (tentativo {attempt}/{max_retries}) ...")
        time.sleep(wait)

        # Recupera i transfer in attesa di approvazione
        try:
            r = requests.get(
                f"{am_url}/api/transfer/unapproved/",
                headers=h, timeout=REQUEST_TIMEOUT, verify=_ssl_verify
            )
            if not r.ok:
                print(f"    [approve] Errore nel recupero unapproved: "
                      f"HTTP {r.status_code}")
                continue
            unapproved = r.json().get("results", [])
        except requests.exceptions.RequestException as e:
            print(f"    [approve] Errore rete: {e}")
            continue

        if not unapproved:
            print(f"    [approve] Nessun transfer in attesa trovato.")
            continue

        # Cerca il transfer che corrisponde al nostro nome
        match = None
        for t in unapproved:
            directory = t.get("directory", "")
            # Il nome nella watched dir contiene il nostro transfer_name
            if transfer_name in directory:
                match = t
                break

        if not match:
            print(f"    [approve] Transfer '{transfer_name}' non trovato "
                  f"tra i {len(unapproved)} in attesa.")
            # Stampa quelli disponibili per debug
            for t in unapproved:
                print(f"      - {t.get('directory', '?')}")
            continue

        # Approva il transfer trovato
        directory = match.get("directory", "")
        print(f"    [approve] Trovato: {directory} — invio approvazione ...")
        try:
            r = requests.post(
                f"{am_url}/api/transfer/approve/",
                headers=h,
                data={"directory": directory, "type": transfer_type},
                timeout=REQUEST_TIMEOUT,
                verify=_ssl_verify
            )
            if r.ok:
                data = r.json()
                if data.get("uuid"):
                    print(f"    [approve] Approvato (UUID: {data['uuid']})")
                    return True
                print(f"    [approve] Risposta inattesa: {data}")
                return False
            print(f"    [approve] HTTP {r.status_code}: {r.text[:300]}")
        except requests.exceptions.RequestException as e:
            print(f"    [approve] Errore rete nell'approvazione: {e}")

    print(f"    [AVVISO] Impossibile approvare automaticamente dopo "
          f"{max_retries} tentativi. Approvare manualmente dal Dashboard.")
    return False


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
        # Marcatore effimero per il conteggio nel riepilogo ("Già presenti"):
        # senza, gli skip verrebbero contati come "Completati". Viene rimosso
        # dal chiamante prima del salvataggio dello stato.
        record["_skipped"] = True
        return record

    print(f"  → Elaborazione: {state_key}")

    # Riusa il nome generato in precedenza se il transfer è già stato avviato,
    # altrimenti generane uno nuovo con la data odierna
    transfer_name     = record.get("transfer_name") or make_transfer_name(doi_folder)
    # Path ASSOLUTO del pacchetto, non il solo nome della cartella: i pacchetti
    # possono trovarsi in una SOTTOCARTELLA della Transfer Source Location
    # (es. .../TRANSFER_SOURCE/PILOTA/doi_x). Passando solo il nome, il percorso
    # veniva ricostruito come <radice_location>/<nome> e la sottocartella andava
    # persa, con errore "Filepath ... does not exist" lato Archivematica.
    transfer_rel_path = str(doi_path.resolve())

    record["transfer_name"] = transfer_name
    record["status"]        = "in_progress"
    record.setdefault("error", None)

    try:
        # ── 1. Avvio Transfer ─────────────────────────────────────────────────
        # Se il record ha uno stato di errore o un transfer precedente
        # fallito/rifiutato, azzera il transfer_uuid per ripartire da zero
        if record.get("status") in ("failed",) and record.get("transfer_uuid"):
            print(f"    [info] Transfer precedente fallito/rifiutato — riavvio da zero.")
            record.pop("transfer_uuid", None)
            record.pop("sip_uuid", None)
            record.pop("aip_uuid", None)

        if not record.get("transfer_uuid"):
            # Applica la processing configuration scelta copiando
            # processingMCP.xml nella radice del pacchetto.
            # Se la sorgente non è utilizzabile e l'utente ha scelto
            # --ignore-missing-processing-config, si procede senza (default AM).
            if args.processing_mcp_source is not None:
                apply_processing_config(doi_path, args.processing_mcp_source)

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

        # ── 2. Approve Transfer automatico (se richiesto) ───────────────────────
        if not record.get("sip_uuid") and args.auto_approve:
            approve_transfer(
                args.am_url, args.am_user, args.am_apikey,
                transfer_name, args.transfer_type,
            )

        # ── 3. Polling Transfer ───────────────────────────────────────────────
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
    global REQUEST_TIMEOUT  # override-abile da --api-timeout

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
    parser.add_argument("--processing-mcp-path", default="",
        help="Percorso di una copia locale versionata del processing config XML "
             "(RACCOMANDATO, o variabile AM_PROCESSING_MCP_PATH nel .env). "
             "Se impostato, ha priorità sulla shared directory di Archivematica "
             "e --processing-config viene usato solo per la verifica via API.")
    parser.add_argument("--ignore-missing-processing-config", action="store_true",
        help="Prosegue anche se il processing config XML non è leggibile: "
             "i transfer useranno la configurazione di DEFAULT di Archivematica "
             "(SCONSIGLIATO — es. scansione virus attiva).")

    # Utility
    parser.add_argument("--no-verify-ssl",   action="store_true",
        help=(
            "Disabilita la verifica del certificato SSL (utile con certificati "
            "self-signed). Equivalente a impostare SSL_VERIFY=false nel .env"
        ))
    parser.add_argument("--auto-approve",  action="store_true",
        help="Approva automaticamente il transfer via API invece di aspettare "
             "conferma manuale (utile se la processing config non ha Approve Transfer "
             "pre-configurato)")
    parser.add_argument("--list-sources",  action="store_true",
        help="Elenca le Transfer Source Locations disponibili ed esce")
    parser.add_argument("--list-processing-configs", action="store_true",
        help="Elenca le processing configuration disponibili (richiede accesso "
             "al filesystem del server Archivematica) ed esce")
    parser.add_argument("--poll-interval", type=int, default=POLL_INTERVAL,
        help=f"Secondi tra un polling e l'altro (default: {POLL_INTERVAL})")
    parser.add_argument("--api-timeout", type=int, default=0,
        help=f"Timeout in secondi per le chiamate API (default: "
             f"{REQUEST_TIMEOUT}, configurabile con AM_API_TIMEOUT nel .env). "
             f"Con dataset grandi o Archivematica sotto carico servono "
             f"valori generosi.")
    parser.add_argument("--dry-run",       action="store_true",
        help="Mostra cosa verrebbe fatto senza eseguire nulla")

    args = parser.parse_args()

    setup_logging()

    global _ssl_verify

    # Verifica SSL: disabilitata se --no-verify-ssl o SSL_VERIFY=false nel .env
    env_ssl = os.environ.get("SSL_VERIFY", "true").lower()
    if args.no_verify_ssl or env_ssl in ("false", "0", "no"):
        _ssl_verify = False
        import urllib3
        urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
        print("[AVVISO] Verifica certificato SSL disabilitata (certificati self-signed accettati).")

    # Risolvi credenziali da env se non passate da CLI
    # Risolvi tutte le variabili da .env / ambiente (CLI ha priorità)
    if args.am_url == AM_URL:
        args.am_url = os.environ.get("AM_URL", AM_URL)
    if args.am_user == AM_USER:
        args.am_user = os.environ.get("AM_USER", AM_USER)
    if not args.am_apikey:
        args.am_apikey = os.environ.get("AM_API_KEY", AM_API_KEY)
    if args.ss_url == SS_URL:
        args.ss_url = os.environ.get("SS_URL", SS_URL)
    if args.ss_user == SS_USER:
        args.ss_user = os.environ.get("SS_USER", SS_USER)
    if not args.ss_apikey:
        args.ss_apikey = os.environ.get("SS_API_KEY", SS_API_KEY)
    if not args.transfer_source:
        args.transfer_source = os.environ.get(
            "AM_TRANSFER_SOURCE_UUID", TRANSFER_SOURCE_UUID
        )
    if not args.auto_approve:
        args.auto_approve = os.environ.get("AM_AUTO_APPROVE", "false").lower() in ("true", "1", "yes")
    if args.transfer_type == TRANSFER_TYPE:
        args.transfer_type = os.environ.get("AM_TRANSFER_TYPE", TRANSFER_TYPE)
    if args.processing_config == PROCESSING_CONFIG:
        args.processing_config = os.environ.get("AM_PROCESSING_CONFIG", PROCESSING_CONFIG)
    if not args.processing_mcp_path:
        args.processing_mcp_path = os.environ.get(
            "AM_PROCESSING_MCP_PATH", PROCESSING_MCP_PATH
        )
    if args.api_timeout > 0:
        # Override da CLI del timeout API (variabile di modulo usata da
        # tutte le chiamate requests; global dichiarato a inizio main)
        REQUEST_TIMEOUT = args.api_timeout

    # ── Modalità --list-processing-configs ────────────────────────────────────
    if args.list_processing_configs:
        configs = list_processing_configs()
        if configs is None:
            print(
                f"[ERRORE] Permission denied su {PROCESSING_CONFIGS_DIR}\n"
                f"L'utente corrente non ha i permessi per leggere la cartella.\n"
                f"Soluzioni possibili:\n"
                f"  1. Aggiungere l'utente al gruppo 'archivematica':\n"
                f"       sudo usermod -aG archivematica $USER\n"
                f"       (richiede logout/login per avere effetto)\n"
                f"  2. Eseguire lo script come utente archivematica:\n"
                f"       sudo -u archivematica python3 archivematica_ingest.py ...\n"
                f"  3. Specificare --processing-config <nome> direttamente,\n"
                f"     senza usare --list-processing-configs.\n"
                f"\nConfigurazioni tipicamente disponibili: default, automated, dataverse_001"
            )
        elif configs:
            print("Processing configuration disponibili:\n")
            for c in configs:
                marker = "  (default)" if c == PROCESSING_CONFIG else ""
                print(f"  {c}{marker}")
        else:
            print(
                f"[ATTENZIONE] Nessuna processing configuration trovata in "
                f"{PROCESSING_CONFIGS_DIR}."
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
            else:
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

    # ── Fail-fast sul processing config XML ───────────────────────────────────
    # Verifica PRIMA di elaborare qualunque pacchetto che il file sorgente
    # di processingMCP.xml sia leggibile. Se non lo è, lo script termina
    # (salvo --ignore-missing-processing-config): mai più pacchetti
    # silenziosamente trasferiti con la configurazione di default.
    src_xml = resolve_processing_mcp_source(
        args.processing_mcp_path, args.processing_config
    )
    if check_processing_mcp_source(src_xml, args.ignore_missing_processing_config):
        args.processing_mcp_source = src_xml
    else:
        args.processing_mcp_source = None  # scelta esplicita dell'utente

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
    print(f"  MCP XML       : {args.processing_mcp_source or '(NESSUNO — default Archivematica)'}")
    print(f"  Auto-approve  : {args.auto_approve}")
    print(f"  File di stato : {args.state_file}")
    print(f"  SSL verify    : {_ssl_verify}")
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

        if record.get("_skipped"):
            stats["skip"] += 1
            record.pop("_skipped", None)
        elif record["status"] == "ingested":
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
