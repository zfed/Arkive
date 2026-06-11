# Manuale d'uso — Pipeline Dataverse → Archivematica

Questo manuale documenta l'uso di due script Python che, insieme, automatizzano il
trasferimento di dataset da **Dataverse UNIMI** (`dataverse.unimi.it`) ad
**Archivematica** per la conservazione a lungo termine:

| Script | Funzione |
|---|---|
| `scarica_dataverse.py` | Scarica dataset e metadati da Dataverse e li organizza in pacchetti compatibili con Archivematica |
| `archivematica_ingest.py` | Trasferisce ed esegue l'ingest dei pacchetti in Archivematica, tenendo traccia di ciò che è già stato processato |

---

## Indice

1. [Panoramica del flusso](#1-panoramica-del-flusso)
2. [Prerequisiti](#2-prerequisiti)
3. [Script 1 — scarica_dataverse.py](#3-script-1--scarica_dataversepy)
4. [Script 2 — archivematica_ingest.py](#4-script-2--archivematica_ingestpy)
5. [Struttura del pacchetto generato](#5-struttura-del-pacchetto-generato)
6. [File di stato e ripresa dopo interruzioni](#6-file-di-stato-e-ripresa-dopo-interruzioni)
7. [Risoluzione dei problemi comuni](#7-risoluzione-dei-problemi-comuni)
8. [Esempio di esecuzione completa](#8-esempio-di-esecuzione-completa)

---

## 1. Panoramica del flusso

```
 dois.txt                scarica_dataverse.py            Transfer Source
 (lista DOI)   ───────▶  - scarica file          ──────▶ Location
                          - scarica metadati                (Archivematica)
                          - genera metadata.csv                  │
                                                                  ▼
                                                      archivematica_ingest.py
                                                      - avvia transfer
                                                      - applica processing config
                                                      - attende transfer + ingest
                                                      - salva stato (AIP UUID)
```

Il flusso tipico è:

1. Si prepara un file `dois.txt` con l'elenco dei DOI dei dataset da archiviare.
2. Si esegue `scarica_dataverse.py`, che scarica tutte le versioni di ciascun
   dataset e le organizza in pacchetti pronti per Archivematica, **direttamente
   nella Transfer Source Location**.
3. Si esegue `archivematica_ingest.py`, che avvia il transfer e l'ingest in
   Archivematica per ogni pacchetto non ancora processato.
4. Lo stato di ogni pacchetto (UUID di transfer, SIP, AIP) viene salvato in un
   file JSON, così le esecuzioni successive elaborano solo i dataset nuovi o
   non completati.

---

## 2. Prerequisiti

### Software

```bash
pip install requests --break-system-packages
```

### Accesso a dataverse.unimi.it

- Una **API key Dataverse** (necessaria solo per dataset ad accesso ristretto;
  per dataset pubblici può essere lasciata vuota).

### Accesso ad Archivematica

- **Dashboard API key** dell'utente Archivematica (es. `zfed`)
- **Storage Service API key** dell'utente Storage Service (es. `archivematica`)
- **UUID della Transfer Source Location** — la cartella che Archivematica
  monitora per i transfer
- I due script devono girare **sullo stesso host** del server Archivematica
  (o avere accesso al medesimo filesystem), perché:
  - leggono/scrivono direttamente nella Transfer Source Location
  - leggono i file XML delle processing configuration da
    `/var/archivematica/sharedDirectory/sharedMicroServiceTasksConfigs/processingMCPConfigs/`

### Trovare l'UUID della Transfer Source Location

```bash
python archivematica_ingest.py --list-sources \
  --ss-user archivematica --ss-apikey <CHIAVE_STORAGE_SERVICE>
```

Output di esempio:
```
Transfer Source Locations disponibili:

  UUID : 5740049b-8b06-425b-bea6-126c799b6113
  Path : /mnt/e/DROPBOX/Dropbox/_ARKIVE/TRANSFER_SOURCE
  Desc : Default transfer source
```

Annota l'UUID: servirà come `--transfer-source` per la fase di ingest.

### Trovare le processing configuration disponibili

```bash
python archivematica_ingest.py --list-processing-configs
```

Output di esempio:
```
Processing configuration disponibili:

  automated
  dataverse_001  (default)
  default
```

---

## 3. Script 1 — scarica_dataverse.py

### Cosa fa

Per ogni DOI elencato in `dois.txt`:

1. Recupera **tutte le versioni** pubblicate del dataset
   (`v1.0`, `v1.1`, `v2.0`, ...) tramite l'API Dataverse
2. Per ciascuna versione:
   - scarica tutti i file del dataset
   - salva i metadati nativi Dataverse e l'export `dataverse_json` in un
     unico file `dataverse.json`
   - genera un `metadata.csv` Dublin Core specifico per quella versione
3. Genera un `metadata.csv` complessivo nella radice del pacchetto, con i
   metadati Dublin Core di **tutte le versioni**, nel formato letto
   nativamente da Archivematica (METS `dmdSec`)

### File di input: dois.txt

Un DOI per riga. Le righe vuote e quelle che iniziano con `#` vengono ignorate.

```
doi:10.13130/RD_UNIMI/KS2PUX
doi:10.13130/RD_UNIMI/NFURUT
# questo è un commento, viene ignorato
doi:10.13130/RD_UNIMI/QBRJNN
```

### Opzioni da riga di comando

| Opzione | Default | Descrizione |
|---|---|---|
| `--dois <file>` | `dois.txt` | File con l'elenco dei DOI |
| `--output <cartella>` | `DATASET_TRASFERITI` | Cartella di destinazione (deve coincidere con la Transfer Source Location di Archivematica) |
| `--apikey <chiave>` | _(vuoto)_ | API key Dataverse |

### API key Dataverse

Tre modi, in ordine di priorità:

1. Argomento da riga di comando: `--apikey <chiave>`
2. Variabile d'ambiente: `DATAVERSE_API_KEY=<chiave>`
3. Costante `API_KEY` nel file (sconsigliato, solo per test locali)

### Esempi di esecuzione

```bash
# Caso base: dataset pubblici, output nella cartella di default
python scarica_dataverse.py

# Specificando la API key e l'output verso la Transfer Source di Archivematica
python scarica_dataverse.py \
  --dois dois.txt \
  --output /mnt/e/DROPBOX/Dropbox/_ARKIVE/TRANSFER_SOURCE \
  --apikey abcd1234efgh5678

# Con API key da variabile d'ambiente
export DATAVERSE_API_KEY=abcd1234efgh5678
python scarica_dataverse.py --output /mnt/e/DROPBOX/Dropbox/_ARKIVE/TRANSFER_SOURCE
```

### Output a schermo

```
============================================================
  Dataverse UNIMI — Download automatico (tutte le versioni)
  Sorgente DOI : dois.txt  (3 DOI)
  Destinazione : /mnt/e/DROPBOX/Dropbox/_ARKIVE/TRANSFER_SOURCE
  API key      : ******5678
============================================================

[1/3] DOI: doi:10.13130/RD_UNIMI/KS2PUX
  → Recupero lista versioni...
  → 1 versione/i trovata/e: v1.0
  ── Versione v1.0  [RELEASED]
    [metadata] dataverse.json salvato
    [data] 4 file trovati, inizio download...
      Scarico: works.tab ... OK (25 KB)
      Scarico: geolocations.tab ... OK (1 KB)
      Scarico: collections.tab ... OK (2 KB)
      Scarico: README.txt ... OK (3 KB)
    [metadata] metadata.csv (versione) generato (4 righe)
  [metadata] metadata.csv generato (4 righe, 1 versione/i)
  [validazione] metadata.csv OK — nessun problema rilevato.
  ✓ Stato: ok | Versioni: 1 | File OK: 4 | Errori file: 0

...

============================================================
  RIEPILOGO FINALE
  Completati  : 3
  Parziali    : 0
  Errori      : 0
  Riepilogo   : DATASET_TRASFERITI/riepilogo_download.json
============================================================
```

### Comportamento idempotente

Lo script può essere rieseguito più volte senza ridondanze:

- i file già scaricati (presenti e non vuoti) vengono **saltati**
  (`[SKIP] Già presente: ...`)
- `dataverse.json` e `metadata.csv` di versione vengono generati solo se
  mancanti
- il `metadata.csv` complessivo (nella cartella `metadata/` radice) viene
  **sempre rigenerato**, in modo da includere automaticamente eventuali nuove
  versioni aggiunte in esecuzioni successive

---

## 4. Script 2 — archivematica_ingest.py

### Cosa fa

Per ogni pacchetto DOI presente nella Transfer Source Location (e non ancora
completato):

1. Copia il file `processingMCP.xml` corrispondente alla **processing
   configuration** scelta nella radice del pacchetto
2. Avvia il **Transfer** tramite la Dashboard API di Archivematica, con nome
   nel formato `AIP-<YYYYMMDD>-<DOI>`
3. Attende il completamento del Transfer (polling)
4. Attende il completamento dell'**Ingest** (SIP → AIP) (polling)
5. Salva l'UUID dell'AIP risultante nel file di stato

**Importante:** ogni cartella DOI (con tutte le sue versioni `v1.0/`, `v2.0/`,
... al suo interno) viene trasferita come **un unico pacchetto**, non una
versione alla volta.

### Opzioni da riga di comando

#### Sorgente e stato

| Opzione | Default | Descrizione |
|---|---|---|
| `--source <cartella>` | _(risolto automaticamente)_ | Directory con i pacchetti DOI. Se omesso, viene usato il path della Transfer Source Location risolto dallo Storage Service — comportamento consigliato |
| `--state-file <file>` | `stato_archivematica.json` | File JSON di tracciamento |

#### Dashboard Archivematica

| Opzione | Default | Descrizione |
|---|---|---|
| `--am-url <url>` | `http://localhost` | URL del Dashboard |
| `--am-user <utente>` | `zfed` | Utente Dashboard |
| `--am-apikey <chiave>` | _(vuoto)_ | API key Dashboard (o variabile `AM_API_KEY`) |

#### Storage Service

| Opzione | Default | Descrizione |
|---|---|---|
| `--ss-url <url>` | `http://localhost:8000` | URL Storage Service |
| `--ss-user <utente>` | `archivematica` | Utente Storage Service |
| `--ss-apikey <chiave>` | _(vuoto)_ | API key Storage Service (o variabile `SS_API_KEY`) |

#### Transfer

| Opzione | Default | Descrizione |
|---|---|---|
| `--transfer-source <UUID>` | _(vuoto)_ | UUID della Transfer Source Location (o variabile `AM_TRANSFER_SOURCE_UUID`) — **obbligatorio** |
| `--transfer-type <tipo>` | `standard` | Tipo di transfer. Valori ammessi: `standard`, `zipped bag`, `unzipped bag`, `dspace`, `dspace destination only`, `maildir`, `TRIM`, `disk image` |
| `--processing-config <nome>` | `dataverse_001` | Nome della processing configuration (vedi `--list-processing-configs`) |

#### Utility

| Opzione | Descrizione |
|---|---|
| `--list-sources` | Elenca le Transfer Source Locations disponibili ed esce |
| `--list-processing-configs` | Elenca le processing configuration disponibili ed esce |
| `--poll-interval <secondi>` | Intervallo tra un controllo di stato e l'altro (default: 15) |
| `--dry-run` | Mostra cosa verrebbe fatto senza eseguire nulla |

### Esempi di esecuzione

```bash
# Esecuzione standard, con credenziali da riga di comando
python archivematica_ingest.py \
  --am-user zfed --am-apikey <CHIAVE_DASHBOARD> \
  --ss-user archivematica --ss-apikey <CHIAVE_STORAGE_SERVICE> \
  --transfer-source 5740049b-8b06-425b-bea6-126c799b6113

# Con credenziali da variabili d'ambiente (consigliato)
export AM_API_KEY=<CHIAVE_DASHBOARD>
export SS_API_KEY=<CHIAVE_STORAGE_SERVICE>
export AM_TRANSFER_SOURCE_UUID=5740049b-8b06-425b-bea6-126c799b6113
python archivematica_ingest.py

# Specificando una processing configuration diversa
python archivematica_ingest.py --processing-config automated

# Tipo di transfer "zipped bag"
python archivematica_ingest.py --transfer-type "zipped bag"

# Anteprima senza eseguire nulla
python archivematica_ingest.py --dry-run

# Polling più frequente (ogni 5 secondi)
python archivematica_ingest.py --poll-interval 5
```

### Output a schermo

```
================================================================
  Archivematica Ingest — trasferimento automatico dataset
  Sorgente      : /mnt/e/DROPBOX/Dropbox/_ARKIVE/TRANSFER_SOURCE  (da Transfer Source Location 5740049b-...)
  Pacchetti DOI : 3
  Dashboard     : http://localhost  (utente: zfed, key: ******05e4)
  Storage Svc   : http://localhost:8000  (utente: archivematica, key: ******77df)
  Transfer Src  : 5740049b-8b06-425b-bea6-126c799b6113
  Transfer type : standard
  Processing cfg: dataverse_001
  File di stato : stato_archivematica.json
================================================================

[1/3] doi_10.13130_RD_UNIMI_KS2PUX
  → Elaborazione: doi_10.13130_RD_UNIMI_KS2PUX
    [debug] processingMCP.xml ('dataverse_001') copiato in .../doi_10.13130_RD_UNIMI_KS2PUX/processingMCP.xml
    Avvio transfer 'AIP-20260611-doi_10.13130_RD_UNIMI_KS2PUX' ...
    [debug] Transfer Source base : /mnt/e/DROPBOX/Dropbox/_ARKIVE/TRANSFER_SOURCE
    [debug] Full path            : /mnt/e/DROPBOX/Dropbox/_ARKIVE/TRANSFER_SOURCE/doi_10.13130_RD_UNIMI_KS2PUX
    [debug] Risposta start_transfer: {'message': 'Copy successful.', 'path': '.../AIP-20260611-doi_10.13130_RD_UNIMI_KS2PUX-<uuid>/'}
    Transfer UUID: <uuid>
    Attendo completamento transfer ...
    [transfer] stato: PROCESSING — attendo 15s ...
    SIP UUID: <uuid>
    Attendo completamento ingest ...
    [ingest]   stato: PROCESSING — attendo 15s ...
    ✓ Ingest completato — AIP UUID: <uuid>

...

================================================================
  RIEPILOGO FINALE
  Completati   : 3
  Già presenti : 0
  Errori       : 0
  File stato   : stato_archivematica.json
================================================================
```

---

## 5. Struttura del pacchetto generato

`scarica_dataverse.py` genera, per ogni DOI, una struttura compatibile con
Archivematica, pronta per essere trasferita **come unico pacchetto**:

```
DATASET_TRASFERITI/                          ← Transfer Source Location
  doi_10.13130_RD_UNIMI_KS2PUX/              ← un pacchetto = un transfer
    objects/
      v1.0/
        objects/                             ← file del dataset
          works.tab
          geolocations.tab
          collections.tab
          README.txt
        metadata/                            ← metadati di questa versione
          dataverse.json                     ← metadati nativi Dataverse + export (preservation)
          metadata.csv                       ← Dublin Core di questa versione
      v2.0/
        objects/
          works_v2.tab
        metadata/
          dataverse.json
          metadata.csv
    metadata/
      metadata.csv                           ← Dublin Core di TUTTE le versioni
                                              ← letto da Archivematica → METS dmdSec
    processingMCP.xml                        ← copiato da archivematica_ingest.py
                                              ← prima dell'avvio del transfer
```

### Formato di metadata.csv (radice del pacchetto)

| filename | dc.title | dc.creator | dc.date | dc.description | dc.identifier | dc.publisher | dc.rights | dc.subject | dc.type |
|---|---|---|---|---|---|---|---|---|---|
| `objects/v1.0/objects/works.tab` | Titolo dataset | Autore 1 \| Autore 2 | 2026-03-04 | Descrizione... | doi:10.13130/RD_UNIMI/KS2PUX | Editore | CC-BY 4.0 | tag1 \| tag2 | Dataset |
| `objects/v1.0/objects/README.txt` | ... | ... | ... | ... | ... | ... | ... | ... | Dataset |
| `objects/v2.0/objects/works_v2.tab` | ... | ... | ... | ... | ... | ... | ... | ... | Dataset |

Questi metadati vengono trasposti automaticamente da Archivematica nel
`METS.xml` generato per l'AIP, come elementi `<dmdSec>` con `MDTYPE="DC"`.

### Validazione automatica

Dopo la generazione, il `metadata.csv` viene validato automaticamente:

- **Errori bloccanti** (impostano lo stato del DOI a `partial`):
  - prima colonna diversa da `filename`
  - colonna `dc.title` mancante
  - nomi di colonna con spazi
  - `filename` che non inizia con `objects/`
  - caratteri di controllo nel `filename`
- **Avvisi** (non bloccanti):
  - colonne non Dublin Core (`dc.*` o `dcterms.*`)
  - `dc.title` vuoto in qualche riga
  - righe completamente vuote

---

## 6. File di stato e ripresa dopo interruzioni

### riepilogo_download.json (generato da scarica_dataverse.py)

Riepilogo di tutte le esecuzioni, per DOI e per versione:

```json
[
  {
    "doi": "doi:10.13130/RD_UNIMI/KS2PUX",
    "status": "ok",
    "versions": [
      {
        "version": "v1.0",
        "state": "RELEASED",
        "status": "ok",
        "files_ok": 4,
        "files_err": 0,
        "error": null
      }
    ],
    "error": null
  }
]
```

### stato_archivematica.json (generato da archivematica_ingest.py)

Traccia lo stato di ogni pacchetto DOI trasferito:

```json
{
  "doi_10.13130_RD_UNIMI_KS2PUX": {
    "transfer_name": "AIP-20260611-doi_10.13130_RD_UNIMI_KS2PUX",
    "transfer_uuid": "bde477ff-f88a-45f6-8714-861248ee194d",
    "sip_uuid": "a1b2c3d4-...",
    "aip_uuid": "e5f6a7b8-...",
    "status": "ingested",
    "completed_at": "2026-06-11T10:42:13+00:00",
    "error": null
  }
}
```

#### Valori possibili di `status`

| Valore | Significato |
|---|---|
| `in_progress` | Transfer/ingest avviato ma non completato (es. script interrotto) |
| `transferred` | Transfer completato, ingest non ancora completato |
| `ingested` | Completato con successo — **pacchetto saltato nelle esecuzioni successive** |
| `failed` | Errore durante transfer o ingest (vedi campo `error`) |

#### Ripresa dopo un'interruzione

Se lo script viene interrotto (es. Ctrl+C, crash, riavvio), al rilancio:

- i pacchetti con `status: "ingested"` vengono **saltati**
- i pacchetti con `transfer_uuid` già presente **non vengono ritrasferiti**:
  lo script riprende il polling da dove si era interrotto
- i pacchetti con `status: "failed"` vengono **ritentati da capo** (nuovo
  transfer)

#### Reset manuale di un pacchetto

Per forzare la rielaborazione di un singolo DOI (es. dopo aver corretto un
problema):

```bash
python3 -c "
import json
s = json.load(open('stato_archivematica.json'))
s.pop('doi_10.13130_RD_UNIMI_KS2PUX', None)
json.dump(s, open('stato_archivematica.json', 'w'), indent=2)
print('Rimosso')
"
```

#### Reset completo

```bash
echo '{}' > stato_archivematica.json
```

---

## 7. Risoluzione dei problemi comuni

### "Nessuna versione trovata" / "Nessun pacchetto DOI trovato"

- Verifica che `--source` (o la Transfer Source Location di default) punti
  alla cartella che contiene le sottocartelle `doi_.../objects/vX.Y/`
- Verifica che `scarica_dataverse.py` sia stato eseguito con `--output`
  puntato alla stessa cartella

### Errore HTTP 500 su `start_transfer`

Tipicamente causato da un path errato. Lo script stampa righe di debug:
```
[debug] Transfer Source base : ...
[debug] Full path            : ...
```
Verifica che `Full path` corrisponda a un percorso realmente esistente sul
filesystem del server Archivematica.

### Il transfer non appare nel Dashboard

- Controlla che la cartella sia stata effettivamente copiata in:
  ```
  /var/archivematica/sharedDirectory/watchedDirectories/activeTransfers/standardTransfer/
  ```
- Verifica che i servizi Archivematica siano attivi:
  ```bash
  sudo systemctl status archivematica-mcp-server archivematica-mcp-client
  ```

### Errore 400 "Unable to determine the status of the unit"

Errore transitorio nei primi secondi dopo l'avvio del transfer (Archivematica
non ha ancora registrato l'unità). Lo script ritenta automaticamente fino a
**2 volte extra**, attendendo **20 secondi** ogni volta. Se l'errore persiste
oltre questi tentativi, verificare lo stato dei servizi MCP.

### La processing configuration scelta non viene applicata

- Verifica che il file esista con il nome corretto:
  ```bash
  ls /var/archivematica/sharedDirectory/sharedMicroServiceTasksConfigs/processingMCPConfigs/
  ```
  Il file deve chiamarsi `<nome>ProcessingMCP.xml` (es.
  `dataverse_001ProcessingMCP.xml`)
- Lo script copia questo file come `processingMCP.xml` nella radice del
  pacchetto **prima** di avviare il transfer — verifica la riga di debug:
  ```
  [debug] processingMCP.xml ('dataverse_001') copiato in .../processingMCP.xml
  ```
- Se compare `[AVVISO] ... non trovata`, il nome passato a
  `--processing-config` non corrisponde a nessun file presente

### Transfer rifiutato (REJECTED)

Controlla nel Dashboard: **Transfer → Failed/Rejected transfers** per il log
dettagliato del microservice che ha causato il rifiuto.

### Elasticsearch non raggiungibile

```
elastic_transport.ConnectionError: ... Connection refused ... 127.0.0.1:9200
```

Riavvia Elasticsearch:
```bash
sudo systemctl status elasticsearch
sudo systemctl start elasticsearch
curl -s http://localhost:9200
```

---

## 8. Esempio di esecuzione completa

```bash
# 1. Configurazione ambiente (una tantum, in ~/.bashrc o simile)
export DATAVERSE_API_KEY="xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx"
export AM_API_KEY="yyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyy"
export SS_API_KEY="zzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzz"
export AM_TRANSFER_SOURCE_UUID="5740049b-8b06-425b-bea6-126c799b6113"

TRANSFER_SOURCE="/mnt/e/DROPBOX/Dropbox/_ARKIVE/TRANSFER_SOURCE"

# 2. Preparare l'elenco dei DOI da archiviare
cat > dois.txt << 'EOF'
doi:10.13130/RD_UNIMI/KS2PUX
doi:10.13130/RD_UNIMI/NFURUT
doi:10.13130/RD_UNIMI/QBRJNN
EOF

# 3. Scaricare i dataset direttamente nella Transfer Source
python scarica_dataverse.py \
  --dois dois.txt \
  --output "$TRANSFER_SOURCE"

# 4. Trasferire e ingestare in Archivematica
python archivematica_ingest.py \
  --processing-config dataverse_001

# 5. (Facoltativo) Verificare lo stato
cat stato_archivematica.json | python3 -m json.tool
```

Per le esecuzioni successive (es. nuovi DOI aggiunti a `dois.txt`, o nuove
versioni pubblicate dei dataset esistenti), è sufficiente ripetere i passi
3 e 4: entrambi gli script elaborano solo ciò che è nuovo o non ancora
completato.
