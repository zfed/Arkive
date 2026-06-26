# Arkive — Tools: dataverse-archivematica

Pipeline automatizzata per trasferire dataset da **Dataverse UNIMI**
(`dataverse.unimi.it`) ad **Archivematica** per la conservazione a lungo termine.

## Script

| Script | Descrizione |
|---|---|
| `scarica_dataverse.py` | Scarica dataset e metadati da Dataverse (tutte le versioni) e li organizza in pacchetti compatibili con Archivematica |
| `archivematica_ingest.py` | Trasferisce e ingesta i pacchetti in Archivematica con approvazione automatica del transfer |

## Installazione

```bash
git clone https://github.com/zfed/Arkive.git
cd Arkive/tools/dataverse-archivematica
pip install requests --break-system-packages
cp .env.template .env
nano .env
```

## Configurazione rapida

Compila `.env` con le tue credenziali. Le variabili principali:

```bash
DATAVERSE_API_KEY=       # API key Dataverse (vuoto per dataset pubblici)
EXPORT_DCAT=false        # true per generare dcat.json e schema.json

AM_URL=https://<host>
AM_USER=zfed
AM_API_KEY=

SS_URL=https://<host>:8000
SS_USER=archivematica
SS_API_KEY=

AM_TRANSFER_SOURCE_UUID= # python3 archivematica_ingest.py --list-sources
AM_PROCESSING_CONFIG=dataverse_001
AM_AUTO_APPROVE=true

SSL_VERIFY=false         # false per certificati self-signed
```

## Utilizzo

```bash
# 1. Prepara la lista DOI
echo "doi:10.13130/RD_UNIMI/KS2PUX" > dois.txt

# 2. Scarica i dataset nella Transfer Source
python3 scarica_dataverse.py \
  --output /path/alla/transfer_source

# 2b. Con metadati DCAT-AP e Schema.org
python3 scarica_dataverse.py \
  --output /path/alla/transfer_source \
  --dcat

# 3. Ingesta in Archivematica (completamente automatico)
python3 archivematica_ingest.py
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
        dcat.json       <- DCAT-AP JSON-LD (solo con --dcat)
        schema.json     <- Schema.org JSON-LD (solo con --dcat)
    v2.0/
      ...
  metadata/
    metadata.csv        <- Dublin Core tutte le versioni -> METS dmdSec
  processingMCP.xml     <- processing configuration Archivematica
```

## Documentazione

Vedi `manuale_dataverse_archivematica.pdf` per il manuale completo.

## Autore

Federica Zanardini — Universita' degli Studi di Milano, Direzione ICT (2026)
Sviluppato con il supporto di Claude AI (Anthropic).

## Licenza

MIT
