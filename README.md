# Arkive

Raccolta di tool e script sviluppati per operazioni specifiche (importazione dati, automazioni, conversioni, ecc.).

## Struttura del repository

Ogni tool ha una propria sottocartella in `tools/`, con codice, dipendenze e documentazione indipendenti.

```
Arkive/
├── README.md
├── LICENSE
├── .gitignore
└── tools/
    ├── dataverse-archivematica/
    │   ├── README.md
    │   ├── requirements.txt
    │   ├── .env.template
    │   ├── dois.txt.example
    │   ├── scarica_dataverse.py
    │   ├── archivematica_ingest.py
    │   ├── riconcilia_stato.py
    │   └── docs/
    │       ├── manuale_dataverse_archivematica.md
    │       └── manuale_dataverse_archivematica.pdf
    ├── bulk_register_aips/
    │   ├── README.md
    │   ├── requirements.txt
    │   ├── bulk_register_aips.py
    │   ├── generate_test_aips.py
    │   └── docs/
    └── delete_AIPs/
        ├── README.md
        ├── requirements.txt
        ├── .env.example
        ├── aip_client.py
        ├── list_aips.py
        └── delete_aips.py
```

## Tool disponibili

- [`dataverse-archivematica`](tools/dataverse-archivematica/README.md): pipeline per il trasferimento di dataset da Dataverse UNIMI ad Archivematica per la conservazione a lungo termine, con riconciliazione dello stato.
- [`bulk_register_aips`](tools/bulk_register_aips/README.md): registrazione via API di AIP orfani (presenti su disco ma assenti dal database dello Storage Service).
- [`delete_AIPs`](tools/delete_AIPs/README.md): elenco e richiesta di cancellazione di AIP nello Storage Service di Archivematica.

## Come usare un tool

Entra nella cartella del tool che ti interessa e segui le istruzioni nel suo README:

```bash
cd tools/nome-tool
```

## Autore e licenza

Autore: Federica Zanardini — Università degli Studi di Milano, Direzione ICT
Sviluppato con il supporto di Claude AI (Anthropic)

Distribuito con licenza [MIT](LICENSE).
