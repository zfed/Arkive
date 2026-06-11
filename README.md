# Arkive

Raccolta di tool e script sviluppati per operazioni specifiche (importazione dati, automazioni, conversioni, ecc.).

## Struttura del repository

Ogni tool ha una propria sottocartella in `tools/`, con codice, dipendenze e documentazione indipendenti.

```
Arkive/
├── README.md
├── .gitignore
└── tools/
    └── dataverse-archivematica/
        ├── README.md
        ├── requirements.txt
        ├── dois.txt.example
        ├── scarica_dataverse.py
        ├── archivematica_ingest.py
        └── docs/
            └── manuale_dataverse_archivematica.md
```

## Tool disponibili

- [`dataverse-archivematica`](tools/dataverse-archivematica/README.md): pipeline per il trasferimento di dataset da Dataverse UNIMI ad Archivematica per la conservazione a lungo termine.

## Come usare un tool

Entra nella cartella del tool che ti interessa e segui le istruzioni nel suo README:

```bash
cd tools/nome-tool
```
