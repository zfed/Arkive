# bulk_register_aips

Script per registrare in blocco, nello Storage Service di Archivematica, un
insieme di AIP che esistono già fisicamente su disco (in una cartella) ma
non sono ancora tracciati nel database della Storage Service.

Casi d'uso tipici:
- AIP prodotti da un'altra istanza Archivematica (es. quella di Federica
  Zanardini) da consolidare nella Storage Service principale.
- AIP prodotti da strumenti diversi da Archivematica (a3m, DART3) e non
  ancora ingestiti/registrati.
- Migrazioni o recuperi da backup dove gli AIP sono presenti sul
  filesystem ma il record nel database è andato perso o non è mai stato
  creato.

Per la spiegazione completa della logica (quadrant path, fallback via
METS, gestione dei conflitti UUID) vedi [`docs/MANUALE.md`](docs/MANUALE.md).

## Requisiti

- Python 3.8+
- Pacchetti Python: vedi `requirements.txt`
- Il binario `7z` (pacchetto `p7zip-full`) nel `PATH`, se nella cartella
  scansionata sono presenti AIP compressi in `.7z` e serve leggerne il METS
  interno (fallback di identificazione o cross-check UUID)

## Installazione

```bash
pip install -r requirements.txt --break-system-packages
cp .env.example .env
# poi modifica .env con i tuoi valori (SS_API_KEY, TARGET_LOCATION_UUID, ...)
```

## Utilizzo rapido

Prima un dry-run, per vedere cosa farebbe lo script senza modificare nulla:

```bash
python3 bulk_register_aips.py --scan-dir /percorso/cartella/aip --dry-run
```

Se il risultato è quello atteso, esecuzione reale:

```bash
python3 bulk_register_aips.py --scan-dir /percorso/cartella/aip
```

Alla fine viene generato un file `bulk_register_report.json` (o il nome
passato con `--report`) con l'esito di ciascun AIP e l'elenco degli
eventuali conflitti da rivedere a mano.

## Opzioni principali

| Opzione                     | Descrizione                                                                 |
|------------------------------|------------------------------------------------------------------------------|
| `--scan-dir`                 | Cartella da scansionare (obbligatoria)                                       |
| `--target-location-uuid`     | UUID della location AIP Storage di destinazione (o da `.env`)               |
| `--dry-run`                  | Simula senza eseguire alcuna POST                                            |
| `--no-mets-fallback`         | Disabilita apertura archivi per fallback identificazione e cross-check UUID |
| `--report`                   | Percorso del file di report JSON (default `bulk_register_report.json`)      |

## ⚠️ Prima di un run massivo

- Testa sempre su **un solo AIP** in un ambiente non di produzione.
- Verifica che `--scan-dir` punti alla radice corretta della struttura
  (vedi la sezione sul path a quadranti nel manuale).
- Controlla la sezione "conflitti" del report/output: sono AIP con UUID nel
  nome file diverso da quello nel METS interno, e **non vengono registrati
  automaticamente**.
