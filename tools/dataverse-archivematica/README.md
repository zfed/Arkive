# Dataverse → Archivematica

Pipeline di due script Python che automatizzano il trasferimento di dataset da Dataverse UNIMI (`dataverse.unimi.it`) ad Archivematica per la conservazione a lungo termine.

| Script | Funzione |
|---|---|
| `scarica_dataverse.py` | Scarica dataset e metadati da Dataverse e li organizza in pacchetti compatibili con Archivematica |
| `archivematica_ingest.py` | Trasferisce ed esegue l'ingest dei pacchetti in Archivematica, tenendo traccia di ciò che è già stato processato |

📖 **Manuale completo**: [docs/manuale_dataverse_archivematica.md](docs/manuale_dataverse_archivematica.md)

## Installazione

```bash
pip install -r requirements.txt --break-system-packages
```

## Avvio rapido

```bash
# 1. Configurazione ambiente (una tantum, in ~/.bashrc o simile)
export DATAVERSE_API_KEY="xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx"
export AM_API_KEY="yyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyy"
export SS_API_KEY="zzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzz"
export AM_TRANSFER_SOURCE_UUID="5740049b-8b06-425b-bea6-126c799b6113"
TRANSFER_SOURCE="/mnt/e/DROPBOX/Dropbox/_ARKIVE/TRANSFER_SOURCE"

# 2. Preparare l'elenco dei DOI da archiviare (vedi dois.txt.example)
cp dois.txt.example dois.txt

# 3. Scaricare i dataset direttamente nella Transfer Source
python scarica_dataverse.py --dois dois.txt --output "$TRANSFER_SOURCE"

# 4. Trasferire e ingestare in Archivematica
python archivematica_ingest.py --processing-config dataverse_001

# 5. (Facoltativo) Verificare lo stato
cat stato_archivematica.json | python3 -m json.tool
```

> Nota: i due script devono girare sullo stesso host del server Archivematica (o avere accesso al medesimo filesystem). Per tutti i dettagli su prerequisiti, opzioni, struttura dei pacchetti, file di stato e risoluzione dei problemi, consulta il [manuale completo](docs/manuale_dataverse_archivematica.md).
