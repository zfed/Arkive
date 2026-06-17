# Manuale d'uso — Strumenti per la cancellazione AIP in Archivematica

## 1. A cosa serve questo strumento

Questo strumento è composto da tre script Python che usano le API REST ufficiali dello **Storage Service** di Archivematica per individuare gli AIP (Archival Information Package) presenti nel sistema e per inviarne la richiesta di cancellazione in batch, senza dover ripetere l'operazione manualmente per ciascun AIP dall'interfaccia web.

### Concetto fondamentale da capire prima di iniziare

In Archivematica la cancellazione di un AIP è **sempre** un processo a due fasi, sia che la si esegua dall'interfaccia web sia tramite API:

1. **Richiesta** — qualcuno (un utente, o questo script) richiede la cancellazione di un AIP specifico, indicando una motivazione.
2. **Approvazione** — un amministratore dello Storage Service deve aprire l'interfaccia web (Storage Service > Packages) e approvare o rifiutare ciascuna richiesta.

Questo secondo passaggio è un controllo di sicurezza voluto dal progetto Archivematica per evitare cancellazioni accidentali o non supervisionate, e non esiste un endpoint pubblico documentato per automatizzarlo. Questi script automatizzano quindi **solo la fase 1**: dopo averli eseguiti, le richieste resteranno "in sospeso" finché qualcuno non le approva manualmente.

## 2. Struttura dei file

```
.
├── README.md
├── .env.example
├── .gitignore
└── tools/
    ├── aip_client.py     # client per le chiamate alle API dello Storage Service
    ├── list_aips.py      # elenca/filtra gli AIP presenti
    └── delete_aips.py    # invia le richieste di cancellazione in batch
```

`aip_client.py` non si lancia direttamente: contiene le funzioni usate dagli altri due script. `list_aips.py` e `delete_aips.py` sono gli strumenti che usi davvero da riga di comando.

## 3. Prerequisiti

Serve Python 3 con la libreria `requests`:

```bash
pip install requests --break-system-packages
```

(oppure, meglio, in un virtualenv dedicato: `python3 -m venv .venv && source .venv/bin/activate && pip install requests`)

Servono inoltre cinque informazioni sul tuo ambiente Archivematica:

| Informazione | Cosa è | Dove si trova |
|---|---|---|
| `SS_BASE_URL` | URL dello Storage Service | es. `http://localhost:8000`, o l'indirizzo del tuo server |
| `SS_USERNAME` | Username di un utente dello Storage Service | quello con cui accedi all'interfaccia dello Storage Service |
| `SS_API_KEY` | API key di quell'utente | Storage Service → Administration → Users → seleziona l'utente → API key |
| `SS_PIPELINE_UUID` | UUID della pipeline Archivematica | Storage Service → Administration → Pipelines |
| `SS_USER_ID` | ID **numerico** dell'utente nel database del dashboard Archivematica (non lo username!) | Dashboard → Administration → Users → apri l'utente → leggi l'ID dalla URL, es. `.../administration/accounts/3/edit/` → `3` |
| `SS_USER_EMAIL` | Email da associare alla richiesta di cancellazione | l'email dell'utente sopra |

Attenzione particolare a `SS_USER_ID`: deve essere un numero (es. `1`, `3`), non una stringa come il nome utente. Se ci metti per errore una stringa, lo script si ferma con un messaggio che te lo segnala.

## 4. Configurazione

Copia `.env.example` in `.env` e compila i valori:

```bash
cp .env.example .env
nano .env   # o l'editor che preferisci
```

Poi carica le variabili nella shell **ogni volta che apri un nuovo terminale o modifichi il file**:

```bash
export $(grep -v '^#' .env | xargs)
```

Verifica che siano state caricate correttamente:

```bash
echo $SS_USER_ID
```

Se modifichi `.env` in un secondo momento e l'output di `echo $SS_USER_ID` mostra ancora il vecchio valore, significa che non hai ripetuto il comando `export` qui sopra: è l'errore più comune.

In alternativa, puoi sempre passare i valori direttamente come argomenti da riga di comando agli script (vedi sotto): in quel caso hanno sempre la precedenza sulle variabili d'ambiente.

## 5. Trovare gli AIP da cancellare — `list_aips.py`

Questo script interroga lo Storage Service ed elenca gli AIP presenti, con la possibilità di filtrarli per stato e di esportarli in CSV.

Elencare tutti gli AIP:

```bash
python3 tools/list_aips.py
```

L'output mostra, per ogni AIP, UUID, stato, dimensione e percorso:

```
2930b0d9-d237-429b-838f-b33370fa5275   UPLOADED   1048576   ...
```

Esportare l'elenco completo in un file CSV:

```bash
python3 tools/list_aips.py --csv tutti_gli_aip.csv
```

Filtrare solo gli AIP con un certo stato (utile per restringere il campo prima di decidere cosa cancellare):

```bash
python3 tools/list_aips.py --status UPLOADED --csv da_valutare.csv
```

Nota: i valori esatti dello stato (`UPLOADED`, `DEL_REQ`, `DELETED`, ecc.) possono variare leggermente tra versioni di Archivematica. La prima volta è consigliabile lanciare il comando senza filtri per vedere quali valori compaiono nel tuo ambiente specifico.

Il file CSV generato ha tre colonne (`uuid`, `status`, `size`, `current_path`) ed è pensato per essere passato direttamente a `delete_aips.py` come lista di candidati.

## 6. Inviare le richieste di cancellazione — `delete_aips.py`

### 6.1 Verificare prima con `--dry-run`

Prima di inviare qualsiasi richiesta reale, conviene sempre simulare l'operazione:

```bash
python3 tools/delete_aips.py --uuid-file da_valutare.csv --reason "Periodo di conservazione scaduto" --dry-run
```

Questo comando mostra quali AIP verrebbero processati, senza contattare l'API.

### 6.2 Invio reale

Per inviare effettivamente le richieste di cancellazione:

```bash
python3 tools/delete_aips.py --uuid-file da_valutare.csv --reason "Periodo di conservazione scaduto"
```

Lo script chiede una conferma interattiva (digitare `si`) prima di procedere, poi elabora gli AIP uno per uno, con una breve pausa tra una richiesta e l'altra per non sovraccaricare l'API.

### 6.3 Opzioni principali

| Opzione | Significato |
|---|---|
| `--uuid <uuid>` | Specifica un singolo AIP da elaborare; ripetibile più volte |
| `--uuid-file <percorso>` | File con un UUID per riga, oppure CSV con colonna `uuid` (es. quello generato da `list_aips.py`) |
| `--reason "testo"` | Motivazione della cancellazione (obbligatoria) |
| `--pipeline <uuid>` | UUID della pipeline (se non impostato come variabile d'ambiente) |
| `--user-id <numero>` | ID numerico utente (se non impostato come variabile d'ambiente) |
| `--user-email <email>` | Email utente (se non impostata come variabile d'ambiente) |
| `--dry-run` | Simula senza inviare nulla |
| `--yes` | Salta la conferma interattiva (utile in script automatici/cron) |
| `--delay <secondi>` | Pausa tra una richiesta e l'altra (default 0.5s) |
| `--log <percorso>` | File di log (default `delete_aips.log`, modalità append) |

### 6.4 Esempio con un singolo AIP

```bash
python3 tools/delete_aips.py --uuid 2930b0d9-d237-429b-838f-b33370fa5275 --reason "Duplicato" --yes
```

### 6.5 Il log

Ogni esecuzione aggiunge righe a `delete_aips.log` (o al file indicato con `--log`), con timestamp, UUID, risultato (`OK`/`FALLITO`) e dettaglio della risposta. È lo storico di cosa è stato richiesto e quando.

## 7. Dopo l'invio: l'approvazione

Le richieste inviate da `delete_aips.py` compaiono nello Storage Service in stato "in sospeso" (deletion requested). Un amministratore deve:

1. Accedere all'interfaccia web dello Storage Service.
2. Andare nella sezione Packages.
3. Aprire ciascuna richiesta in sospeso e approvarla o rifiutarla.

Questo passaggio resta manuale per le ragioni di sicurezza spiegate al punto 1.

## 8. Problemi comuni

**Errore `invalid literal for int() with base 10: '...'`**
Significa che il valore passato come `user_id` non è un numero. Controlla di aver impostato `SS_USER_ID` con l'ID numerico dell'utente (vedi tabella al punto 3), non con lo username.

**Ho cambiato `.env` ma l'errore persiste**
Quasi sempre significa che non hai ripetuto `export $(grep -v '^#' .env | xargs)` dopo la modifica: la shell tiene in memoria il valore vecchio finché non lo ri-esporti. Verifica con `echo $SS_USER_ID`.

**Errore di configurazione "Configurazione mancante"**
Una o più tra `SS_BASE_URL`, `SS_USERNAME`, `SS_API_KEY` non sono impostate. Verificale con `echo $SS_BASE_URL` ecc.

**La richiesta torna un errore HTTP (4xx/5xx)**
Il messaggio di errore restituito dall'API viene riportato nel log e a schermo: di solito indica credenziali non valide, UUID non esistente, o un AIP già in uno stato che non permette la richiesta (es. già cancellato).

## 9. Note di sicurezza

Non condividere o pubblicare mai il file `.env`: contiene la tua API key. Il `.gitignore` incluso lo esclude automaticamente da eventuali repository Git.

Esegui sempre `--dry-run` prima di un invio massivo, specialmente la prima volta che usi un nuovo file di UUID.

Le richieste inviate non causano mai una cancellazione immediata: serve sempre l'approvazione manuale descritta al punto 7.
