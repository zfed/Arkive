#!/usr/bin/env python3
"""
scarica_dataverse.py
--------------------
Scarica TUTTE LE VERSIONI di ogni dataset (+ metadati JSON) da dataverse.unimi.it
sulla base dei DOI contenuti in dois.txt.

Struttura output (compatibile con Archivematica):

  DATASET_TRASFERITI/
    <doi_sanitizzato>/
      objects/
        v1.0/
          objects/           <- file del dataset
            file1.csv
          metadata/          <- metadati Dataverse della versione
            dataverse.json
            metadata.csv     <- DC per questa versione (preservation)
            dcat.json        <- DCAT-AP JSON-LD (solo se EXPORT_DCAT=true)
            schema.json      <- Schema.org JSON-LD (solo se EXPORT_DCAT=true)
        v2.0/
          objects/
            file2.csv
          metadata/
            dataverse.json
            metadata.csv
            dcat.json        <- (solo se EXPORT_DCAT=true)
            schema.json      <- (solo se EXPORT_DCAT=true)
      metadata/
        metadata.csv         <- DC per TUTTE le versioni (letto da Archivematica)
  riepilogo_download.json
  doi_vuoti.json             <- DOI senza file (non viene creata la directory)

Nota DCAT: il file dcat.json viene generato localmente a partire dai metadati
gia' scaricati (dataverse.json), senza chiamate HTTP aggiuntive. Ogni versione
produce il proprio dcat.json con i metadati della versione specifica.

La API key puo' essere fornita in tre modi (ordine di priorita'):
  1. Argomento da riga di comando: --apikey <chiave>
  2. Variabile d'ambiente:         DATAVERSE_API_KEY=<chiave>
  3. Costante API_KEY nel codice (sconsigliato per sicurezza)

Uso:
  python scarica_dataverse.py
  python scarica_dataverse.py --apikey <chiave>
  python scarica_dataverse.py --dois mia_lista.txt --output CARTELLA_OUT
  python scarica_dataverse.py --dcat        # abilita DCAT-AP e Schema.org
  EXPORT_DCAT=true python scarica_dataverse.py
"""

import argparse
import csv
import json
import os
import re
import sys
import time
import urllib3
from datetime import datetime, timezone
from pathlib import Path

import requests


def _load_dotenv(env_file: str = ".env") -> None:
    """
    Carica variabili da un file .env senza dipendenze esterne.
    Formato supportato: KEY=value, ignora righe vuote e commenti (#).
    Le variabili gia' presenti nell'ambiente non vengono sovrascritte.
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

# ── Configurazione ────────────────────────────────────────────────────────────

BASE_URL           = "https://dataverse.unimi.it"
DOIS_FILE          = "dois.txt"
OUTPUT_DIR         = os.environ.get("OUTPUT_DIR", "DATASET_TRASFERITI")
RETRY_ATTEMPTS     = 3
RETRY_DELAY        = 5    # secondi tra tentativi
REQUEST_TIMEOUT    = 60   # secondi per ogni richiesta HTTP
PAUSE_BETWEEN_DOIS = 2    # secondi di cortesia tra DOI diversi

# API key Dataverse — lasciare "" per usare --apikey o la variabile d'ambiente
API_KEY = ""

# Verifica certificato SSL (False per certificati self-signed)
SSL_VERIFY = True

# Esportazione DCAT-AP e Schema.org (sovrascrivibile con EXPORT_DCAT=true nel .env)
EXPORT_DCAT: bool = os.environ.get("EXPORT_DCAT", "false").lower() in ("true", "1", "yes")

# ── Utility ───────────────────────────────────────────────────────────────────

def sanitize(text: str) -> str:
    """Rende una stringa sicura come nome di cartella/file."""
    return re.sub(r'[\\/:*?"<>|]', '_', text).strip()


def version_tag(major: int, minor: int) -> str:
    """Restituisce una stringa tipo v2.1 usata come nome cartella versione."""
    return f"v{major}.{minor}"


def load_dois(path: str) -> list[str]:
    """Legge la lista DOI dal file, ignorando righe vuote e commenti (#)."""
    p = Path(path)
    if not p.exists():
        print(f"[ERRORE] File DOI non trovato: {path}", file=sys.stderr)
        sys.exit(1)
    dois = [l.strip() for l in p.read_text(encoding="utf-8").splitlines()
            if l.strip() and not l.strip().startswith("#")]
    if not dois:
        print(f"[ATTENZIONE] Nessun DOI trovato in {path}.", file=sys.stderr)
        sys.exit(0)
    return dois


# ── HTTP helper ───────────────────────────────────────────────────────────────

# Chiave attiva — impostata da main() dopo aver risolto la priorita'
_active_api_key: str = ""

# Verifica SSL attiva — impostata da main()
_ssl_verify: bool = True


def resolve_api_key(cli_value: str) -> str:
    """
    Risolve la API key secondo l'ordine di priorita':
      1. --apikey da riga di comando
      2. Variabile d'ambiente DATAVERSE_API_KEY
      3. Costante API_KEY nel codice
    """
    if cli_value:
        return cli_value
    env = os.environ.get("DATAVERSE_API_KEY", "")
    if env:
        return env
    return API_KEY


def _build_headers() -> dict:
    """Restituisce gli header HTTP con la API key, se presente."""
    headers = {}
    if _active_api_key:
        headers["X-Dataverse-key"] = _active_api_key
    return headers


def get_with_retry(url: str, params: dict = None, stream: bool = False):
    """GET con retry automatico; rilancia subito su 404."""
    for attempt in range(1, RETRY_ATTEMPTS + 1):
        try:
            if not _ssl_verify:
                urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
            r = requests.get(
                url,
                params=params,
                headers=_build_headers(),
                timeout=REQUEST_TIMEOUT,
                stream=stream,
                verify=_ssl_verify,
            )
            r.raise_for_status()
            return r
        except requests.exceptions.HTTPError as e:
            status = e.response.status_code if e.response else "?"
            if status == 404:
                raise
            print(f"  [Tentativo {attempt}/{RETRY_ATTEMPTS}] HTTP {status}: {e}")
        except requests.exceptions.RequestException as e:
            print(f"  [Tentativo {attempt}/{RETRY_ATTEMPTS}] Errore di rete: {e}")
        if attempt < RETRY_ATTEMPTS:
            time.sleep(RETRY_DELAY)
    raise RuntimeError(
        f"Impossibile raggiungere {url} dopo {RETRY_ATTEMPTS} tentativi.")


# ── API Dataverse ─────────────────────────────────────────────────────────────

def get_all_versions(doi: str) -> list[dict]:
    """
    Restituisce tutte le versioni pubblicate di un dataset.
    Endpoint: GET /api/datasets/:persistentId/versions?persistentId=<doi>
    """
    url    = f"{BASE_URL}/api/datasets/:persistentId/versions"
    params = {"persistentId": doi}
    r      = get_with_retry(url, params=params)
    return r.json().get("data", [])


def get_version_export(doi: str, version_str: str,
                       exporter: str = "dataverse_json") -> dict | None:
    """
    Esporta i metadati di una specifica versione.
    Endpoint: GET /api/datasets/export?exporter=...&persistentId=<doi>&version=<major.minor>
    """
    url    = f"{BASE_URL}/api/datasets/export"
    params = {"exporter": exporter, "persistentId": doi, "version": version_str}
    try:
        r = get_with_retry(url, params=params)
        return r.json()
    except Exception as e:
        print(f"  [ATTENZIONE] Export {exporter} non riuscito: {e}")
        return None


# ── Generazione DCAT-AP ───────────────────────────────────────────────────────

def _citation_fields(version: dict) -> dict:
    blocks   = version.get("metadataBlocks", {})
    citation = blocks.get("citation", {}).get("fields", [])
    return {f["typeName"]: f for f in citation}


def _compound_values(field: dict, subfield: str) -> list[str]:
    values = field.get("value", [])
    if not isinstance(values, list):
        return []
    results = []
    for item in values:
        if isinstance(item, dict) and subfield in item:
            v = item[subfield].get("value", "")
            if v:
                results.append(str(v).strip())
    return results


def _simple_value(fields: dict, type_name: str) -> str:
    f = fields.get(type_name)
    if not f:
        return ""
    val = f.get("value", "")
    if isinstance(val, str):
        return val.strip()
    if isinstance(val, list) and val:
        first = val[0]
        return str(first).strip() if isinstance(first, str) else ""
    return ""


def _compound_concat(fields: dict, type_name: str, subfield: str,
                     sep: str = " | ") -> str:
    f = fields.get(type_name)
    if not f:
        return ""
    return sep.join(_compound_values(f, subfield))


def build_dcat_from_version(doi: str, version: dict) -> str:
    """
    Genera un JSON-LD DCAT-AP 2.x dai metadati di una versione Dataverse.
    Non effettua chiamate HTTP: usa esclusivamente i dati gia' in memoria.
    """
    cf      = _citation_fields(version)
    major   = version.get("versionNumber", 0)
    minor   = version.get("versionMinorNumber", 0)
    vtag    = f"{major}.{minor}"
    doi_uri = f"https://doi.org/{doi.replace('doi:', '')}" if doi else ""

    title       = _simple_value(cf, "title")
    description = _compound_concat(cf, "dsDescription", "dsDescriptionValue", " ")
    issued      = (_simple_value(cf, "productionDate")
                   or _simple_value(cf, "distributionDate")
                   or version.get("releaseTime", "")[:10])
    modified    = version.get("releaseTime", "")[:10]
    license_val = _simple_value(cf, "license") or _simple_value(cf, "termsOfUse")
    language    = _simple_value(cf, "language")
    publisher   = _simple_value(cf, "publisher")

    creators = []
    if "author" in cf:
        names  = _compound_values(cf["author"], "authorName")
        affils = _compound_values(cf["author"], "authorAffiliation")
        for i, name in enumerate(names):
            agent: dict = {"@type": "foaf:Person", "foaf:name": name}
            if i < len(affils) and affils[i]:
                agent["org:memberOf"] = {"@type": "foaf:Organization",
                                         "foaf:name": affils[i]}
            creators.append(agent)

    contact_point = None
    if "datasetContact" in cf:
        contact_names  = _compound_values(cf["datasetContact"], "datasetContactName")
        contact_emails = _compound_values(cf["datasetContact"], "datasetContactEmail")
        if contact_names or contact_emails:
            vcard: dict = {"@type": ["vcard:Individual", "vcard:Kind"]}
            if contact_names:
                vcard["vcard:fn"] = contact_names[0]
            if contact_emails:
                vcard["vcard:hasEmail"] = f"mailto:{contact_emails[0]}"
            contact_point = vcard

    themes   = []
    keywords = []
    if "subject" in cf:
        val    = cf["subject"].get("value", [])
        themes = val if isinstance(val, list) else ([val] if val else [])
    if "keyword" in cf:
        keywords = _compound_values(cf["keyword"], "keywordValue")

    distributions = []
    for file_info in version.get("files", []):
        df      = file_info.get("dataFile", {})
        file_id = df.get("id")
        label   = file_info.get("label") or df.get("filename") or f"file_{file_id}"
        mime    = df.get("contentType", "")
        size    = df.get("fileSize") or df.get("filesize")
        md5     = df.get("md5", "")

        dist: dict = {"@type": "dcat:Distribution", "dct:title": label}
        if file_id:
            dist["dcat:accessURL"] = {"@id": f"{BASE_URL}/api/access/datafile/{file_id}"}
        if mime:
            dist["dcat:mediaType"] = mime
        if size:
            dist["dcat:byteSize"] = size
        if md5:
            dist["spdx:checksum"] = {
                "@type":              "spdx:Checksum",
                "spdx:algorithm":     "spdx:checksumAlgorithm_md5",
                "spdx:checksumValue": md5,
            }
        if license_val:
            dist["dct:license"] = license_val
        distributions.append(dist)

    dataset: dict = {
        "@context": {
            "dcat":  "http://www.w3.org/ns/dcat#",
            "dct":   "http://purl.org/dc/terms/",
            "foaf":  "http://xmlns.com/foaf/0.1/",
            "org":   "http://www.w3.org/ns/org#",
            "vcard": "http://www.w3.org/2006/vcard/ns#",
            "spdx":  "http://spdx.org/rdf/terms#",
            "xsd":   "http://www.w3.org/2001/XMLSchema#",
            "owl":   "http://www.w3.org/2002/07/owl#",
        },
        "@type":           "dcat:Dataset",
        "@id":             doi_uri,
        "dct:identifier":  doi,
        "owl:versionInfo": vtag,
    }

    if title:       dataset["dct:title"]       = title
    if description: dataset["dct:description"] = description
    if issued:      dataset["dct:issued"]      = {"@type": "xsd:date", "@value": issued}
    if modified and modified != issued:
        dataset["dct:modified"] = {"@type": "xsd:date", "@value": modified}
    if creators:
        dataset["dct:creator"] = creators if len(creators) > 1 else creators[0]
    if publisher:
        dataset["dct:publisher"] = {"@type": "foaf:Organization", "foaf:name": publisher}
    if contact_point:
        dataset["dcat:contactPoint"] = contact_point
    if themes:    dataset["dcat:theme"]        = themes
    if keywords:  dataset["dcat:keyword"]      = keywords
    if license_val: dataset["dct:license"]     = license_val
    if language:  dataset["dct:language"]      = language
    if distributions:
        dataset["dcat:distribution"] = distributions

    return json.dumps(dataset, ensure_ascii=False, indent=2)


# ── Download file ─────────────────────────────────────────────────────────────

def download_file(file_info: dict, data_dir: Path) -> bool:
    """Scarica un singolo file del dataset. Restituisce True se riuscito."""
    df      = file_info.get("dataFile", {})
    file_id = df.get("id")
    label   = file_info.get("label") or df.get("filename") or f"file_{file_id}"

    if file_id is None:
        print(f"  [ATTENZIONE] ID file mancante, salto: {label}")
        return False

    dest = data_dir / label
    if dest.exists() and dest.stat().st_size > 0:
        print(f"      [SKIP] Gia' presente: {label}")
        return True

    url = f"{BASE_URL}/api/access/datafile/{file_id}"
    print(f"      Scarico: {label} ...", end=" ", flush=True)
    try:
        r = get_with_retry(url, stream=True)
        with open(dest, "wb") as f:
            for chunk in r.iter_content(chunk_size=8192):
                f.write(chunk)
        size_kb = dest.stat().st_size // 1024
        print(f"OK ({size_kb} KB)")
        return True
    except Exception as e:
        print(f"ERRORE ({e})")
        return False


# ── Generazione metadata.csv per Archivematica ───────────────────────────────

DC_VALID_PREFIXES = ("dc.", "dcterms.")
FORBIDDEN_CHARS   = re.compile(r'[\x00-\x1f\x7f]')


def _dc_value(fields: list[dict], field_type_name: str) -> str:
    for f in fields:
        if f.get("typeName") == field_type_name:
            val = f.get("value", "")
            if isinstance(val, str):
                return val.strip()
            if isinstance(val, list):
                parts = []
                for item in val:
                    if isinstance(item, str):
                        parts.append(item.strip())
                    elif isinstance(item, dict):
                        for sub in item.values():
                            if isinstance(sub, dict) and "value" in sub:
                                parts.append(str(sub["value"]).strip())
                return " | ".join(p for p in parts if p)
    return ""


def _dc_fields_from_version(version: dict) -> dict:
    citation = {}
    blocks   = version.get("metadataBlocks", {})
    if "citation" in blocks:
        for f in blocks["citation"].get("fields", []):
            citation[f["typeName"]] = f

    def get(name: str) -> str:
        return _dc_value(list(citation.values()), name)

    return {
        "title":       get("title"),
        "authors":     get("authorName") or get("author"),
        "date":        (get("productionDate") or get("distributionDate")
                        or version.get("releaseTime", "")[:10]),
        "description": get("dsDescription") or get("description"),
        "identifier":  version.get("datasetPersistentId", ""),
        "publisher":   get("publisher") or get("datasetContact"),
        "rights":      get("license") or get("termsOfUse") or "",
        "subjects":    get("subject") or get("keyword"),
    }


def _write_version_csv(version: dict, data_dir: Path, ver_meta_dir: Path) -> None:
    """Scrive metadata.csv DC per una singola versione."""
    dc           = _dc_fields_from_version(version)
    subjects_str = dc["subjects"] or ""

    files_in_objects = sorted(
        [f.name for f in data_dir.iterdir() if f.is_file()]
    ) if data_dir.exists() else []

    fieldnames = ["filename", "dc.title", "dc.creator", "dc.date", "dc.description",
                  "dc.identifier", "dc.publisher", "dc.rights", "dc.subject", "dc.type"]

    targets = ([f"objects/{fn}" for fn in files_in_objects]
               if files_in_objects else ["objects/"])

    rows = [{"filename": fp, "dc.title": dc["title"], "dc.creator": dc["authors"],
             "dc.date": dc["date"], "dc.description": dc["description"],
             "dc.identifier": dc["identifier"], "dc.publisher": dc["publisher"],
             "dc.rights": dc["rights"], "dc.subject": subjects_str,
             "dc.type": "Dataset"} for fp in targets]

    with open(ver_meta_dir / "metadata.csv", "w", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)

    print(f"    [metadata] metadata.csv (versione) generato ({len(rows)} righe)")


def write_archivematica_metadata(versions: list[dict], doi_dir: Path,
                                 meta_dir: Path) -> None:
    """Genera <doi_dir>/metadata/metadata.csv per Archivematica (METS dmdSec)."""
    fieldnames = ["filename", "dc.title", "dc.creator", "dc.date", "dc.description",
                  "dc.identifier", "dc.publisher", "dc.rights", "dc.subject", "dc.type"]
    rows = []

    for version in versions:
        major = version.get("versionNumber", 0)
        minor = version.get("versionMinorNumber", 0)
        vtag  = version_tag(major, minor)
        dc    = _dc_fields_from_version(version)

        objects_dir      = doi_dir / "objects" / vtag / "objects"
        files_in_objects = sorted(
            [f.name for f in objects_dir.iterdir() if f.is_file()]
        ) if objects_dir.exists() else []

        targets = ([f"objects/{vtag}/objects/{fname}" for fname in files_in_objects]
                   if files_in_objects else [f"objects/{vtag}/objects/"])

        for filepath in targets:
            rows.append({
                "filename":       filepath,
                "dc.title":       dc["title"],
                "dc.creator":     dc["authors"],
                "dc.date":        dc["date"],
                "dc.description": dc["description"],
                "dc.identifier":  dc["identifier"],
                "dc.publisher":   dc["publisher"],
                "dc.rights":      dc["rights"],
                "dc.subject":     dc["subjects"],
                "dc.type":        "Dataset",
            })

    csv_path = meta_dir / "metadata.csv"
    with open(csv_path, "w", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)

    print(f"  [metadata] metadata.csv generato ({len(rows)} righe, "
          f"{len(versions)} versione/i)")


def validate_metadata_csv(csv_path: Path) -> list[str]:
    """Valida metadata.csv. Restituisce lista di errori/avvisi."""
    issues = []
    try:
        raw = csv_path.read_bytes().decode("utf-8")
    except UnicodeDecodeError as e:
        return [f"[ERRORE] Encoding non UTF-8: {e}"]

    import io
    reader  = csv.DictReader(io.StringIO(raw))
    headers = reader.fieldnames or []

    if not headers:
        return ["[ERRORE] Il CSV e' vuoto o senza intestazione."]
    if headers[0] != "filename":
        issues.append(f'[ERRORE] Prima colonna deve essere "filename", trovato: "{headers[0]}"')
    for col in ("filename", "dc.title"):
        if col not in headers:
            issues.append(f'[ERRORE] Colonna obbligatoria mancante: "{col}"')
    for col in headers:
        if col == "filename":
            continue
        if not any(col.startswith(p) for p in DC_VALID_PREFIXES):
            issues.append(f'[AVVISO] Colonna non-Dublin Core "{col}".')

    rows = list(reader)
    if not rows:
        issues.append("[AVVISO] Il CSV non contiene righe di dati.")
        return issues

    for i, row in enumerate(rows, start=2):
        fn    = (row.get("filename") or "").strip()
        title = (row.get("dc.title") or "").strip()
        if fn and not fn.startswith("objects/"):
            issues.append(f'[ERRORE] Riga {i}: filename deve iniziare con "objects/", trovato: "{fn}"')
        if fn and FORBIDDEN_CHARS.search(fn):
            issues.append(f'[ERRORE] Riga {i}: filename contiene caratteri vietati: "{fn}"')
        if not title:
            issues.append(f'[AVVISO] Riga {i}: dc.title e\' vuoto.')

    return issues


def report_csv_validation(csv_path: Path) -> bool:
    """Esegue la validazione e stampa i risultati."""
    issues   = validate_metadata_csv(csv_path)
    errors   = [i for i in issues if i.startswith("[ERRORE]")]
    warnings = [i for i in issues if i.startswith("[AVVISO]")]

    if not issues:
        print(f"  [validazione] metadata.csv OK — nessun problema rilevato.")
        return True
    for w in warnings:
        print(f"  {w}")
    for e in errors:
        print(f"  {e}")
    if errors:
        print(f"  [validazione] {len(errors)} errore/i.")
        return False
    print(f"  [validazione] {len(warnings)} avviso/i — accettabile.")
    return True


# ── Controllo dataset vuoto ───────────────────────────────────────────────────

def _dataset_has_files(versions: list[dict]) -> tuple[bool, str]:
    """
    Controlla se almeno una versione del dataset contiene file scaricabili.
    Restituisce (True, "") se ci sono file, oppure (False, motivo) se vuoto.
    """
    if not versions:
        return False, "nessuna versione pubblica trovata"
    for v in versions:
        if v.get("files", []):
            return True, ""
    return False, (
        f"{len(versions)} versione/i trovata/e ma nessun file presente "
        f"(dataset vuoto o accesso ristretto)"
    )


# ── Elaborazione per versione ─────────────────────────────────────────────────

def process_version(doi: str, version: dict, doi_dir: Path,
                    meta_dir: Path) -> dict:
    """
    Elabora una singola versione di un dataset.
    """
    major     = version.get("versionNumber", 0)
    minor     = version.get("versionMinorNumber", 0)
    vtag      = version_tag(major, minor)
    ver_str   = f"{major}.{minor}"
    ver_state = version.get("versionState", "")

    result = {
        "version":   vtag,
        "state":     ver_state,
        "status":    "ok",
        "files_ok":  0,
        "files_err": 0,
        "error":     None,
    }

    print(f"  ── Versione {vtag} [{ver_state}]")

    data_dir     = doi_dir / "objects" / vtag / "objects"
    ver_meta_dir = doi_dir / "objects" / vtag / "metadata"
    data_dir.mkdir(parents=True, exist_ok=True)
    ver_meta_dir.mkdir(parents=True, exist_ok=True)

    # ── dataverse.json ────────────────────────────────────────────────────────
    dv_path = ver_meta_dir / "dataverse.json"
    if not dv_path.exists():
        export   = get_version_export(doi, ver_str, exporter="dataverse_json")
        combined = {"version_metadata": version, "dataverse_export": export}
        with open(dv_path, "w", encoding="utf-8") as f:
            json.dump(combined, f, ensure_ascii=False, indent=2)
        print(f"    [metadata] dataverse.json salvato")
    else:
        print(f"    [metadata] dataverse.json gia' presente, skip")

    # ── dcat.json (DCAT-AP generato localmente) ───────────────────────────────
    if EXPORT_DCAT:
        dcat_path = ver_meta_dir / "dcat.json"
        if not dcat_path.exists():
            try:
                dcat_text = build_dcat_from_version(doi, version)
                dcat_path.write_text(dcat_text, encoding="utf-8")
                print(f"    [metadata] dcat.json generato")
            except Exception as e:
                print(f"    [ATTENZIONE] Generazione DCAT non riuscita: {e}")
        else:
            print(f"    [metadata] dcat.json gia' presente, skip")

    # ── schema.json (Schema.org da API Dataverse) ─────────────────────────────
    if EXPORT_DCAT:
        schema_path = ver_meta_dir / "schema.json"
        if not schema_path.exists():
            schema_data = get_version_export(doi, ver_str, exporter="schema.org")
            if schema_data:
                with open(schema_path, "w", encoding="utf-8") as f:
                    json.dump(schema_data, f, ensure_ascii=False, indent=2)
                print(f"    [metadata] schema.json salvato")
            else:
                print(f"    [metadata] schema.json non disponibile, skip")
        else:
            print(f"    [metadata] schema.json gia' presente, skip")

    # ── File del dataset ──────────────────────────────────────────────────────
    files = version.get("files", [])
    if not files:
        print(f"    [data] Nessun file in questa versione.")
    else:
        print(f"    [data] {len(files)} file trovati, inizio download...")
        for file_info in files:
            ok = download_file(file_info, data_dir)
            if ok:
                result["files_ok"] += 1
            else:
                result["files_err"] += 1

    if result["files_err"] > 0:
        result["status"] = "partial"

    # ── metadata.csv per questa versione ─────────────────────────────────────
    ver_csv_path = ver_meta_dir / "metadata.csv"
    if not ver_csv_path.exists():
        _write_version_csv(version, data_dir, ver_meta_dir)
    else:
        print(f"    [metadata] metadata.csv (versione) gia' presente, skip")

    return result


# ── Elaborazione per DOI ──────────────────────────────────────────────────────

def process_doi(doi: str, output_root: Path) -> dict:
    """Elabora un singolo DOI scaricando tutte le versioni disponibili."""
    result = {
        "doi":          doi,
        "status":       "ok",
        "versions":     [],
        "error":        None,
        "empty_reason": None,
    }

    doi_dir = output_root / sanitize(doi)

    # Recupera tutte le versioni PRIMA di creare la directory
    print(f"  → Recupero lista versioni...")
    try:
        versions = get_all_versions(doi)
    except requests.exceptions.HTTPError as e:
        msg = f"HTTP {e.response.status_code}" if e.response else str(e)
        print(f"  [ERRORE] {msg}")
        result["status"] = "error"
        result["error"]  = msg
        return result
    except Exception as e:
        print(f"  [ERRORE] {e}")
        result["status"] = "error"
        result["error"]  = str(e)
        return result

    if not versions:
        print(f"  [INFO] Nessuna versione pubblica trovata.")
        result["status"]       = "empty"
        result["empty_reason"] = "nessuna versione pubblica trovata"
        return result

    # Controlla se il dataset contiene file prima di creare la directory
    has_files, empty_reason = _dataset_has_files(versions)
    if not has_files:
        print(f"  [INFO] Dataset vuoto: {empty_reason}")
        print(f"  [INFO] Directory non creata.")
        result["status"]       = "empty"
        result["empty_reason"] = empty_reason
        return result

    # Dataset non vuoto: crea la directory e procedi
    doi_dir.mkdir(parents=True, exist_ok=True)

    versions.sort(key=lambda v: (v.get("versionNumber", 0),
                                 v.get("versionMinorNumber", 0)))

    print(f"  → {len(versions)} versione/i trovata/e: "
          + ", ".join(version_tag(v.get("versionNumber", 0),
                                  v.get("versionMinorNumber", 0))
                      for v in versions))

    meta_dir = doi_dir / "metadata"
    meta_dir.mkdir(parents=True, exist_ok=True)

    any_error = False
    for ver in versions:
        vres = process_version(doi, ver, doi_dir, meta_dir)
        result["versions"].append(vres)
        if vres["status"] == "error":
            any_error = True
        elif vres["status"] == "partial" and result["status"] == "ok":
            result["status"] = "partial"

    if any_error and result["status"] == "ok":
        result["status"] = "partial"

    # Genera metadata.csv unico per tutte le versioni (sempre rigenerato)
    write_archivematica_metadata(versions, doi_dir, meta_dir)
    csv_meta_path = meta_dir / "metadata.csv"
    valid = report_csv_validation(csv_meta_path)
    if not valid and result["status"] == "ok":
        result["status"] = "partial"

    return result


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(
        description="Scarica TUTTE LE VERSIONI di dataset e metadati da dataverse.unimi.it"
    )
    parser.add_argument(
        "--dois", default=DOIS_FILE,
        help=f"File con la lista dei DOI (default: {DOIS_FILE})"
    )
    parser.add_argument(
        "--output", default=None,
        help="Cartella di destinazione. Se omessa: variabile OUTPUT_DIR nel "
             ".env, oppure 'DATASET_TRASFERITI' come fallback."
    )
    parser.add_argument(
        "--apikey", default="",
        help="API key Dataverse (sovrascrive la variabile d'ambiente e la costante API_KEY)"
    )
    parser.add_argument(
        "--no-verify-ssl", action="store_true",
        help="Disabilita la verifica SSL (utile con certificati self-signed). "
             "Equivalente a SSL_VERIFY=false nel .env"
    )
    parser.add_argument(
        "--dcat", action="store_true",
        help="Abilita l'esportazione DCAT-AP (dcat.json) e Schema.org (schema.json) "
             "per ogni versione. Equivalente a EXPORT_DCAT=true nel .env"
    )
    args = parser.parse_args()

    global _active_api_key, _ssl_verify, EXPORT_DCAT
    _active_api_key = resolve_api_key(args.apikey)

    env_ssl = os.environ.get("SSL_VERIFY", "true").lower()
    if args.no_verify_ssl or env_ssl in ("false", "0", "no"):
        _ssl_verify = False
        print("[AVVISO] Verifica certificato SSL disabilitata.")

    if args.dcat:
        EXPORT_DCAT = True

    dois        = load_dois(args.dois)
    output_dir  = args.output or OUTPUT_DIR
    output_root = Path(output_dir)
    output_root.mkdir(parents=True, exist_ok=True)

    key_display = ("*" * 6 + _active_api_key[-4:]) if len(_active_api_key) > 4 else \
                  ("(non impostata)" if not _active_api_key else "****")

    print(f"{'='*62}")
    print(f"  Dataverse UNIMI — Download automatico (tutte le versioni)")
    print(f"  Sorgente DOI : {args.dois} ({len(dois)} DOI)")
    print(f"  Destinazione : {output_root.resolve()}")
    print(f"  API key      : {key_display}")
    print(f"  SSL verify   : {_ssl_verify}")
    print(f"  Export DCAT  : {EXPORT_DCAT}")
    print(f"{'='*62}\n")

    summary = []

    for i, doi in enumerate(dois, 1):
        print(f"[{i}/{len(dois)}] DOI: {doi}")
        result = process_doi(doi, output_root)
        summary.append(result)

        icon    = {"ok": "✓", "partial": "⚠", "empty": "○"}.get(result["status"], "✗")
        tot_ok  = sum(v["files_ok"]  for v in result["versions"])
        tot_err = sum(v["files_err"] for v in result["versions"])
        print(
            f"  {icon} Stato: {result['status']} | "
            f"Versioni: {len(result['versions'])} | "
            f"File OK: {tot_ok} | Errori file: {tot_err}"
        )
        if result.get("empty_reason"):
            print(f"  Motivo: {result['empty_reason']}")
        if result["error"]:
            print(f"  Dettaglio errore: {result['error']}")
        print()

        if i < len(dois):
            time.sleep(PAUSE_BETWEEN_DOIS)

    # Riepilogo JSON completo
    summary_path = output_root / "riepilogo_download.json"
    with open(summary_path, "w", encoding="utf-8") as f:
        json.dump(summary, f, ensure_ascii=False, indent=2)

    # Report DOI vuoti (aggiornamento incrementale)
    empty_records = [
        {
            "doi":          r["doi"],
            "empty_reason": r.get("empty_reason", ""),
            "rilevato_il":  datetime.now(timezone.utc).isoformat(timespec="seconds"),
        }
        for r in summary if r["status"] == "empty"
    ]

    empty_path = output_root / "doi_vuoti.json"
    if empty_records:
        existing: dict = {}
        if empty_path.exists():
            try:
                for rec in json.loads(empty_path.read_text(encoding="utf-8")):
                    existing[rec["doi"]] = rec
            except Exception:
                pass
        for rec in empty_records:
            existing[rec["doi"]] = rec
        empty_path.write_text(
            json.dumps(list(existing.values()), ensure_ascii=False, indent=2),
            encoding="utf-8"
        )

    ok      = sum(1 for r in summary if r["status"] == "ok")
    partial = sum(1 for r in summary if r["status"] == "partial")
    errors  = sum(1 for r in summary if r["status"] == "error")
    empty   = sum(1 for r in summary if r["status"] == "empty")

    print(f"{'='*62}")
    print(f"  RIEPILOGO FINALE")
    print(f"  Completati  : {ok}")
    print(f"  Parziali    : {partial}")
    print(f"  Errori      : {errors}")
    print(f"  Vuoti       : {empty}")
    print(f"  Riepilogo   : {summary_path}")
    if empty_records:
        print(f"  DOI vuoti   : {empty_path}")
    print(f"{'='*62}")


if __name__ == "__main__":
    main()
