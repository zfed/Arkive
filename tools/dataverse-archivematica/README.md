# Arkive — Tools: dataverse-archivematica

Pipeline automatizzata per trasferire dataset da **Dataverse UNIMI**
(`dataverse.unimi.it`) ad **Archivematica** per la conservazione a lungo termine.

## Script

| Script | Descrizione |
|---|---|
| `scarica_dataverse.py` | Scarica dataset e metadati da Dataverse (tutte le versioni), verifica che non siano vuoti, e li organizza in pacchetti compatibili con Archivematica |
| `archivematica_ingest.py` | Trasferisce e ingesta i pacchetti in Archivematica con approvazione automatica del transfer |
| `riconcilia_stato.py` | Riconcilia i pacchetti "failed" del file di stato con gli AIP realmente presenti nello Storage Service (es. dopo approvazioni manuali), evitando ingest duplicati |

## Installazione

```bash
git clone https://github.com/zfed/Arkive.git
cd Arkive/tools/dataverse-archivematica
pip install requests --break-system-packages
cp .env.template .env
nano .env
```

### Setup della copia locale del processing config (una tantum, RACCOMANDATO)

```bash
mkdir -p ~/arkive/config
sudo cp /var/archivematica/sharedDirectory/sharedMicroServiceTasksConfigs/processingMCPConfigs/dataverse_001ProcessingMCP.xml ~/arkive/config/
sudo chown $(whoami): ~/arkive/config/dataverse_001ProcessingMCP.xml
```

Imposta poi `AM_PROCESSING_MCP_PATH` nel `.env` (vedi sotto). Se la processing
configuration viene modificata nel Dashboard, riesportare la copia.

## Configurazione rapida

Compila `.env` con le tue credenziali:

```bash
# Dataverse
DATAVERSE_API_KEY=          # lasciare vuoto per dataset pubblici
OUTPUT_DIR=/path/alla/transfer_source
EXPORT_DCAT=false           # true per generare dcat.json e schema.json

# Archivematica Dashboard
AM_URL=https://<host>
AM_USER=zfed
AM_API_KEY=

# Archivematica Storage Service
SS_URL=https://<host>:8000
SS_USER=archivematica
SS_API_KEY=

# Transfer
AM_TRANSFER_SOURCE_UUID=    # python3 archivematica_ingest.py --list-sources
AM_PROCESSING_CONFIG=dataverse_001
# Copia locale versionata del processing config XML (RACCOMANDATO):
# elimina la dipendenza dalla shared directory di Archivematica
AM_PROCESSING_MCP_PATH=/home/zfed/arkive/config/dataverse_001ProcessingMCP.xml
AM_AUTO_APPROVE=true
AM_API_TIMEOUT=120          # timeout API in secondi (generoso: dataset grandi)

SSL_VERIFY=false            # false per certificati self-signed

LOG_DIR=logs                # cartella dove salvare i log di esecuzione
```

## Utilizzo

```bash
# 1. Prepara la lista DOI
echo "doi:10.13130/RD_UNIMI/KS2PUX" > dois.txt

# 2. Scarica i dataset (OUTPUT_DIR letto dal .env)
python3 scarica_dataverse.py

# 2b. Con metadati DCAT-AP e Schema.org
python3 scarica_dataverse.py --dcat

# 3. Ingesta in Archivematica (completamente automatico)
python3 archivematica_ingest.py

# 4. (solo se servito un intervento manuale in Dashboard, es. approve a mano)
#    Riconcilia il file di stato con gli AIP reali nello Storage Service
python3 riconcilia_stato.py --dry-run   # anteprima
python3 riconcilia_stato.py             # applica
```

## Struttura del pacchetto

```
<doi>/
  objects/
    v1.0/
      objects/          <- file del dataset
      metadata/
        dataverse.json  <- metadati Dataverse
        metadata.csv    <- Dublin Core (preservation)
        dcat.json       <- DCAT-AP JSON-LD (solo con --dcat o EXPORT_DCAT=true)
        schema.json     <- Schema.org JSON-LD (solo con --dcat o EXPORT_DCAT=true)
    v2.0/  ...
  metadata/
    metadata.csv        <- Dublin Core tutte le versioni -> METS dmdSec
  processingMCP.xml     <- processing configuration Archivematica
```

## File generati

| File | Generato da | Descrizione |
|---|---|---|
| `riepilogo_download.json` | `scarica_dataverse.py` | Riepilogo completo di ogni DOI |
| `doi_vuoti.json` | `scarica_dataverse.py` | DOI senza file (aggiornato incrementalmente) |
| `stato_archivematica.json` | `archivematica_ingest.py` | Stato di ogni transfer/ingest |
| `stato_archivematica.json.bak_<timestamp>` | `riconcilia_stato.py` | Backup automatico dello stato prima di ogni riconciliazione |
| `logs/<script>_<timestamp>.log` | entrambi | Log completo di ogni esecuzione |

## Documentazione

Vedi `manuale_dataverse_archivematica.pdf` per il manuale completo.

## Note

- I dataset vuoti (nessun file in nessuna versione) vengono rilevati automaticamente:
  la directory non viene creata e il DOI viene registrato in `doi_vuoti.json`
- **Fail-fast sul processing config**: se il file XML della processing
  configuration non e' leggibile, `archivematica_ingest.py` si interrompe
  PRIMA di avviare qualunque transfer (senza `processingMCP.xml` i pacchetti
  verrebbero processati con la configurazione di default di Archivematica,
  es. con scansione virus attiva). Per proseguire comunque, consapevolmente:
  `--ignore-missing-processing-config`
- Dopo ogni copia di `processingMCP.xml` lo script verifica che il file sia
  effettivamente presente nella radice del pacchetto; in caso contrario il
  pacchetto viene marcato `failed` e il transfer non parte
- **Timeout e riaggancio**: gli errori di rete transitori (Read timed out)
  vengono ritentati automaticamente; se `start_transfer` va in timeout ma la
  richiesta e' comunque arrivata ad Archivematica, lo script RIAGGANCIA il
  transfer cercandolo per nome tra quelli in attesa, invece di dichiararlo
  fallito (evitando transfer zombie in Dashboard e AIP duplicati al retry)
- **Mai lanciare gli script con `sudo -u archivematica`** (o come root): le
  cartelle create nella Transfer Source risulterebbero di proprieta' di un
  altro utente e le esecuzioni successive fallirebbero con Permission denied
  sulla copia di `processingMCP.xml`
- Il workflow e' completamente automatico: nessun intervento manuale richiesto
  dopo l'avvio degli script
- Gli script sono idempotenti: possono essere rieseguiti senza duplicare operazioni
  gia' completate
- Ogni esecuzione crea automaticamente un file di log in `logs/` con timestamp,
  contenente l'intero output a schermo

## Autori

Fede (zfed) — Universita' degli Studi di Milano, Direzione ICT (2026)
Sviluppato con il supporto di Claude AI (Anthropic).

## Licenza

MIT
