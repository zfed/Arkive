#!/usr/bin/env python3
"""
bulk_register_aips.py

Registra in blocco nello Storage Service di Archivematica un insieme di AIP
già presenti su disco (in una cartella) ma non ancora tracciati nel database
della Storage Service (es. AIP prodotti da un'altra pipeline, migrati da un
altro server, o creati fuori dal normale workflow di ingest).

Logica:
  1. Inventario FISICO: scansiona la cartella locale indicata e trova i file
     AIP (per pattern di nome), estraendone l'UUID dal nome file.
  2. Inventario LOGICO: interroga l'API v2 della Storage Service per sapere
     quali UUID sono già registrati come package di tipo AIP.
  3. Calcola la differenza (AIP fisicamente presenti ma non registrati).
  4. Per ciascun AIP orfano, esegue una POST /api/v2/file/ per creare il
     record di package nel database della Storage Service.

Uso:
    python3 bulk_register_aips.py --scan-dir /percorso/cartella/aip --dry-run
    python3 bulk_register_aips.py --scan-dir /percorso/cartella/aip

Autore: Fede - Direzione ICT, Università degli Studi di Milano
Data: 2026-07
Nota: script sviluppato con l'assistenza di Claude AI (Anthropic)
"""

import argparse
import json
import os
import re
import subprocess
import sys
import tarfile
import uuid as uuid_lib
import zipfile

import requests
import urllib3

# ---------------------------------------------------------------------------
# Config da .env (nessuna dipendenza esterna tipo python-dotenv)
# ---------------------------------------------------------------------------

def _load_dotenv(path=".env"):
    """Carica variabili da un file .env nell'environment, se presente.
    Deve essere chiamata PRIMA di leggere qualunque variabile a livello
    di modulo, altrimenti si rischia il bug di ordering già visto altrove."""
    if not os.path.exists(path):
        return
    with open(path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            key = key.strip()
            value = value.strip().strip('"').strip("'")
            os.environ.setdefault(key, value)


_load_dotenv()

SS_URL = os.environ.get("SS_URL", "https://192.168.139.39:8000").rstrip("/")
SS_USER = os.environ.get("SS_USER", "archivematica")
SS_API_KEY = os.environ.get("SS_API_KEY", "")
SS_VERIFY = os.environ.get("SSL_VERIFY", "false").lower() == "true"

# UUID della location AIP Storage di destinazione (dove gli AIP devono
# risultare registrati). La trovi in Storage Service > Locations.
TARGET_LOCATION_UUID = os.environ.get("TARGET_LOCATION_UUID", "")

# Pattern per riconoscere un file AIP e estrarne l'UUID dal nome.
# Default: <nome>-<uuid>.<estensione> es. mio-dataset-3fa85f64-5717-4562-b3fc-2c963f66afa6.7z
AIP_FILENAME_RE = re.compile(
    r"^(?P<name>.+)-(?P<uuid>[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})"
    r"(?P<ext>\.(7z|tar\.gz|tar\.bz2|tar|zip))?$",
    re.IGNORECASE,
)

# Estensioni di archivio dentro cui e' possibile cercare il METS come fallback
ARCHIVE_EXTENSIONS = (".7z", ".tar.gz", ".tgz", ".tar.bz2", ".tbz2", ".tar", ".zip")

# Il METS di un AIP Archivematica si chiama tipicamente METS.<uuid>.xml:
# l'UUID e' gia' nel nome del file interno, quindi spesso basta leggere la
# lista dei contenuti dell'archivio senza doverlo aprire e parsare.
METS_FILENAME_RE = re.compile(
    r"METS\.([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})\.xml$",
    re.IGNORECASE,
)
# Fallback: OBJID nell'elemento <mets:mets> contiene spesso l'UUID come
# URN (es. OBJID="3fa85f64-...") quando il nome del METS non lo riporta.
METS_OBJID_RE = re.compile(
    rb'OBJID="[^"]*?([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})"',
    re.IGNORECASE,
)


def _list_archive_entries(path):
    """Elenca i percorsi interni di un archivio zip/tar/7z senza estrarlo
    tutto su disco. Restituisce None se il formato non e' supportato o la
    lettura fallisce (con un warning stampato)."""
    lower = path.lower()
    try:
        if lower.endswith(".zip"):
            with zipfile.ZipFile(path) as zf:
                return zf.namelist()
        if lower.endswith((".tar", ".tar.gz", ".tgz", ".tar.bz2", ".tbz2")):
            with tarfile.open(path, "r:*") as tf:
                return tf.getnames()
        if lower.endswith(".7z"):
            return _list_7z_entries(path)
    except Exception as exc:  # noqa: BLE001 - vogliamo solo loggare e continuare
        print(f"[WARN] impossibile leggere l'archivio {path}: {exc}")
        return None
    return None


def _list_7z_entries(path):
    """Usa il binario 7z (p7zip) per elencare i contenuti, in formato
    'tecnico' (-slt) per un parsing robusto indipendente dalla lingua."""
    try:
        result = subprocess.run(
            ["7z", "l", "-slt", path],
            capture_output=True, text=True, timeout=120,
        )
    except FileNotFoundError:
        print("[WARN] comando '7z' non trovato nel PATH: impossibile leggere "
              "l'interno dei file .7z. Installa p7zip-full per abilitare il "
              "fallback via METS su questi archivi.")
        return None
    if result.returncode != 0:
        print(f"[WARN] 7z ha restituito un errore leggendo {path}: "
              f"{result.stderr.strip()[:200]}")
        return None
    entries = [line[len("Path = "):] for line in result.stdout.splitlines()
               if line.startswith("Path = ")]
    # La prima occorrenza di "Path = " e' di solito l'archivio stesso.
    return entries[1:] if len(entries) > 1 else entries


def _read_archive_entry(path, entry_name):
    """Legge il contenuto (bytes) di una singola voce dentro l'archivio,
    senza estrarre l'intero AIP."""
    lower = path.lower()
    try:
        if lower.endswith(".zip"):
            with zipfile.ZipFile(path) as zf:
                return zf.read(entry_name)
        if lower.endswith((".tar", ".tar.gz", ".tgz", ".tar.bz2", ".tbz2")):
            with tarfile.open(path, "r:*") as tf:
                member = tf.extractfile(entry_name)
                return member.read() if member else None
        if lower.endswith(".7z"):
            result = subprocess.run(
                ["7z", "e", "-so", path, entry_name],
                capture_output=True, timeout=120,
            )
            return result.stdout if result.returncode == 0 else None
    except Exception as exc:  # noqa: BLE001
        print(f"[WARN] impossibile leggere '{entry_name}' da {path}: {exc}")
        return None
    return None


def get_mets_uuid(path):
    """Legge l'UUID autoritativo dal METS interno all'archivio, se possibile.
    Usata sia come fallback di identificazione (quando il filename non
    contiene un UUID) sia come cross-check di coerenza (quando il filename
    ne contiene uno, per verificare che non sia stato rinominato/alterato).

    1. Cerca un'entry con nome METS.<uuid>.xml (piu' veloce).
    2. Se non trovata, cerca una entry che sembra un METS e ne legge il
       contenuto cercando l'OBJID.

    Restituisce l'UUID (str, minuscolo) o None se non trovato/non leggibile.
    """
    entries = _list_archive_entries(path)
    if not entries:
        return None

    for entry in entries:
        m = METS_FILENAME_RE.search(entry)
        if m:
            return m.group(1).lower()

    candidate_mets = next(
        (e for e in entries if os.path.basename(e).lower().startswith("mets")
         and e.lower().endswith(".xml")),
        None,
    )
    if not candidate_mets:
        return None

    content = _read_archive_entry(path, candidate_mets)
    if not content:
        return None
    m = METS_OBJID_RE.search(content)
    return m.group(1).decode().lower() if m else None

if not SS_VERIFY:
    urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)


def compute_quadrant_path(aip_uuid, filename):
    """Calcola il path standard 'a quadranti' (pairtree) usato da Archivematica
    per organizzare gli AIP dentro una location di tipo Local Filesystem:

        <4hex>/<4hex>/<4hex>/<4hex>/<uuid-completo>/<filename>

    I quadranti sono i primi 16 caratteri esadecimali dell'UUID (senza
    trattini), divisi in 4 blocchi da 4 caratteri. Essendo derivato solo
    dall'UUID dell'AIP, questo path e' identico indipendentemente da quale
    istanza Archivematica lo ha generato.
    """
    hex_str = aip_uuid.replace("-", "")
    quadrants = [hex_str[i:i + 4] for i in range(0, 16, 4)]
    return "/".join(quadrants + [aip_uuid, filename])


def resolve_current_path(aip_uuid, info):
    """Decide quale current_path usare per la registrazione:

    - Se il rel_path trovato sul disco corrisponde gia' allo schema a
      quadranti standard per questo UUID (es. AIP prodotto da un'altra
      istanza Archivematica), lo riusa cosi' com'e': nessuno spostamento
      necessario, la struttura e' gia' corretta.
    - Altrimenti (es. AIP piatto prodotto da a3m, DART3 o simili) calcola
      il path a quadranti atteso, cosi' la Storage Service lo sposta nella
      posizione standard coerente con il resto della location.
    """
    expected = compute_quadrant_path(aip_uuid, info["filename"])
    found_normalized = info["rel_path"].replace(os.sep, "/")
    if found_normalized == expected:
        return info["rel_path"], False  # gia' a quadranti, nessun move reale
    return expected, True  # verra' spostato dal path piatto/originale


def _headers():
    return {
        "Authorization": f"ApiKey {SS_USER}:{SS_API_KEY}",
        "Content-Type": "application/json",
    }


# ---------------------------------------------------------------------------
# 1. Inventario fisico: cosa c'è davvero nella cartella
# ---------------------------------------------------------------------------

def find_local_aips(scan_dir, use_mets_fallback=True):
    """Restituisce un dict {uuid: {...}} per ogni file nella cartella
    riconosciuto come AIP. Chiavi di ciascun valore:
      - path, filename, rel_path, size
      - uuid_source: "filename" o "mets" (da dove viene l'UUID usato)
      - uuid_mismatch: True se il nome del file e il METS interno riportano
        UUID diversi (incoerenza da investigare), False se coincidono o
        source e' "mets", None se non verificato (mets fallback disabilitato
        o METS non leggibile).

    L'UUID viene determinato in due modi, in ordine:
      1. dal nome del file, se rispetta il pattern <nome>-<uuid>.<ext>
         (veloce); in questo caso, se use_mets_fallback e' attivo, viene
         comunque fatto un cross-check leggendo l'UUID dal METS interno,
         per intercettare file rinominati o con UUID nel nome non
         corrispondente a quello reale;
      2. se il filename non matcha ma l'estensione e' un formato di
         archivio noto, aprendo l'archivio e cercando l'UUID autoritativo
         nel METS interno (necessario per AIP con nomi non standard, es.
         da a3m/DART3 o rinominati manualmente).

    rel_path e' il percorso relativo a scan_dir (preserva l'eventuale
    struttura pairtree/quadrant) ed e' quello da usare in origin_path,
    NON il solo filename.
    """
    found = {}
    for root, _dirs, files in os.walk(scan_dir):
        for fname in files:
            full_path = os.path.join(root, fname)
            is_archive = fname.lower().endswith(ARCHIVE_EXTENSIONS)

            aip_uuid = None
            uuid_source = None
            uuid_mismatch = None

            m = AIP_FILENAME_RE.match(fname)
            if m and m.group("uuid"):
                aip_uuid = m.group("uuid").lower()
                uuid_source = "filename"
                if use_mets_fallback and is_archive:
                    mets_uuid = get_mets_uuid(full_path)
                    if mets_uuid is None:
                        uuid_mismatch = None  # non verificabile
                    elif mets_uuid != aip_uuid:
                        uuid_mismatch = True
                        print(f"[WARN] incoerenza UUID in {full_path}: "
                              f"nome file={aip_uuid} vs METS={mets_uuid}")
                    else:
                        uuid_mismatch = False
            elif use_mets_fallback and is_archive:
                found_uuid = get_mets_uuid(full_path)
                if found_uuid:
                    aip_uuid = found_uuid
                    uuid_source = "mets"
                    uuid_mismatch = False  # e' l'unica fonte, coincide con se stessa

            if not aip_uuid:
                continue

            if aip_uuid in found:
                print(f"[WARN] UUID {aip_uuid} trovato piu' volte "
                      f"({found[aip_uuid]['path']} e {full_path}): "
                      f"tengo il primo, ignoro il secondo.")
                continue

            rel_path = os.path.relpath(full_path, scan_dir)
            found[aip_uuid] = {
                "path": full_path,
                "filename": fname,
                "rel_path": rel_path,
                "size": os.path.getsize(full_path),
                "uuid_source": uuid_source,
                "uuid_mismatch": uuid_mismatch,
            }
    return found


# ---------------------------------------------------------------------------
# 2. Inventario logico: cosa sa già la Storage Service
# ---------------------------------------------------------------------------

def get_registered_aip_uuids():
    """Interroga /api/v2/file/?package_type=AIP con paginazione e restituisce
    l'insieme degli UUID già registrati."""
    registered = set()
    url = f"{SS_URL}/api/v2/file/?package_type=AIP&limit=100"
    while url:
        resp = requests.get(url, headers=_headers(), verify=SS_VERIFY, timeout=60)
        resp.raise_for_status()
        data = resp.json()
        for obj in data.get("objects", []):
            registered.add(obj["uuid"].lower())
        next_path = data.get("meta", {}).get("next")
        url = f"{SS_URL}{next_path}" if next_path else None
    return registered


# ---------------------------------------------------------------------------
# 3. Diff: AIP fisicamente presenti ma non registrati
# ---------------------------------------------------------------------------

def find_orphan_aips(scan_dir, use_mets_fallback=True):
    """Restituisce (orphans, conflicts, local, registered).

    orphans: AIP fisicamente presenti, non registrati, senza incoerenze UUID
             note tra filename e METS -> sicuri da registrare.
    conflicts: AIP fisicamente presenti, non registrati, MA con UUID nel
               filename diverso da quello nel METS interno -> richiedono
               una verifica manuale prima di essere registrati (l'UUID
               giusto e' quasi certamente quello nel METS, ma va confermato
               caso per caso, es. controllando se l'AIP e' stato rinominato
               o e' una copia/reingest di un altro).
    """
    local = find_local_aips(scan_dir, use_mets_fallback=use_mets_fallback)
    registered = get_registered_aip_uuids()

    orphans = {}
    conflicts = {}
    for u, info in local.items():
        if u in registered:
            continue
        if info.get("uuid_mismatch") is True:
            conflicts[u] = info
        else:
            orphans[u] = info

    return orphans, conflicts, local, registered


# ---------------------------------------------------------------------------
# 4. Registrazione via POST /api/v2/file/
# ---------------------------------------------------------------------------

def build_payload(aip_uuid, info, target_location_uri):
    """Costruisce il payload per la registrazione.

    origin_path e' sempre la posizione REALE del file cosi' come trovato
    sul disco (info["rel_path"]). current_path e' risolto da
    resolve_current_path: se il file e' gia' organizzato secondo lo schema
    a quadranti standard (tipico di un AIP prodotto da un'altra istanza
    Archivematica) coincide con origin_path e non scatta nessuno
    spostamento fisico; altrimenti (es. AIP piatto da a3m/DART3) e' il
    path a quadranti calcolato, e la Storage Service sposta davvero il
    file li' durante la registrazione.
    """
    current_path, will_move = resolve_current_path(aip_uuid, info)
    payload = {
        "uuid": aip_uuid,
        "origin_location": target_location_uri,
        "origin_path": info["rel_path"],
        "current_location": target_location_uri,
        "current_path": current_path,
        "package_type": "AIP",
        "size": info["size"],
    }
    return payload, will_move


def register_aip(aip_uuid, info, target_location_uri, dry_run=True):
    payload, will_move = build_payload(aip_uuid, info, target_location_uri)
    move_note = "CON spostamento fisico -> quadrant path" if will_move else "senza spostamento (gia' a quadranti)"
    source_note = "uuid da METS" if info.get("uuid_source") == "mets" else "uuid da filename"

    if dry_run:
        print(f"[DRY-RUN] Registrerei {aip_uuid} ({info['rel_path']}, "
              f"{info['size']} bytes) [{move_note}] [{source_note}]")
        print(f"           current_path -> {payload['current_path']}")
        return {"dry_run": True, "will_move": will_move, "uuid_source": info.get("uuid_source"),
                "payload": payload}

    resp = requests.post(
        f"{SS_URL}/api/v2/file/",
        headers=_headers(),
        data=json.dumps(payload),
        verify=SS_VERIFY,
        timeout=120,
    )
    if resp.status_code in (200, 201, 202):
        print(f"[OK] Registrato {aip_uuid} ({info['rel_path']}) [{move_note}] [{source_note}]")
        return {"success": True, "status_code": resp.status_code, "will_move": will_move,
                "uuid_source": info.get("uuid_source")}
    else:
        print(f"[ERRORE] {aip_uuid} ({info['rel_path']}): "
              f"HTTP {resp.status_code} - {resp.text[:300]}")
        return {"success": False, "status_code": resp.status_code, "body": resp.text}


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def _save_report(report_path, scan_dir, dry_run, results, conflicts):
    report = {
        "scan_dir": scan_dir,
        "dry_run": dry_run,
        "results": results,
        "conflicts": {
            u: {"rel_path": info["rel_path"], "filename_uuid": u}
            for u, info in conflicts.items()
        },
    }
    with open(report_path, "w", encoding="utf-8") as f:
        json.dump(report, f, indent=2, ensure_ascii=False)
    print(f"\nReport salvato in: {report_path}")


def main():
    parser = argparse.ArgumentParser(
        description="Registra in blocco AIP orfani nella Storage Service di Archivematica."
    )
    parser.add_argument("--scan-dir", required=True,
                         help="Cartella locale da scansionare alla ricerca di AIP")
    parser.add_argument("--target-location-uuid", default=TARGET_LOCATION_UUID,
                         help="UUID della location AIP Storage di destinazione "
                              "(default da TARGET_LOCATION_UUID in .env)")
    parser.add_argument("--dry-run", action="store_true",
                         help="Mostra cosa verrebbe fatto senza eseguire alcuna POST")
    parser.add_argument("--no-mets-fallback", action="store_true",
                         help="Disabilita l'apertura degli archivi per leggere l'UUID "
                              "dal METS quando il nome del file non lo contiene "
                              "(piu' veloce su collezioni molto grandi, ma ignora "
                              "gli AIP con nomi non standard)")
    parser.add_argument("--report", default="bulk_register_report.json",
                         help="File dove salvare il report JSON dell'esecuzione")
    args = parser.parse_args()

    if not args.target_location_uuid:
        sys.exit("Errore: manca --target-location-uuid (o TARGET_LOCATION_UUID in .env)")

    if not SS_API_KEY:
        sys.exit("Errore: manca SS_API_KEY in .env")

    target_location_uri = f"/api/v2/location/{args.target_location_uuid}/"

    print(f"Scansione cartella: {args.scan_dir}")
    orphans, conflicts, local, registered = find_orphan_aips(
        args.scan_dir, use_mets_fallback=not args.no_mets_fallback
    )

    n_from_mets = sum(1 for info in local.values() if info["uuid_source"] == "mets")
    print(f"AIP trovati fisicamente: {len(local)} "
          f"(di cui {n_from_mets} identificati aprendo il METS, non dal filename)")
    print(f"AIP già registrati nella Storage Service: {len(registered)}")
    print(f"AIP orfani da registrare: {len(orphans)}")

    if conflicts:
        print(f"\n[ATTENZIONE] {len(conflicts)} AIP hanno un'incoerenza tra "
              f"UUID nel nome file e UUID nel METS interno. NON verranno "
              f"registrati automaticamente (vanno verificati a mano):")
        for u, info in conflicts.items():
            print(f"  - {info['rel_path']} (nome file: {u})")

    if not orphans and not conflicts:
        print("Nessun AIP orfano da registrare. Fine.")
        return
    if not orphans:
        print("\nNessun AIP orfano sicuro da registrare (solo conflitti da rivedere). Fine.")
        _save_report(args.report, args.scan_dir, args.dry_run, {}, conflicts)
        return

    if args.dry_run:
        print("\n--- MODALITA' DRY-RUN: nessuna modifica verrà applicata ---\n")

    report = {"scan_dir": args.scan_dir, "dry_run": args.dry_run, "results": {}}
    for aip_uuid, info in orphans.items():
        result = register_aip(aip_uuid, info, target_location_uri, dry_run=args.dry_run)
        report["results"][aip_uuid] = result

    _save_report(args.report, args.scan_dir, args.dry_run, report["results"], conflicts)


if __name__ == "__main__":
    main()
