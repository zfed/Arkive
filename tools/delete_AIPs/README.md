# Delete AIPs

Script per individuare e richiedere la cancellazione di AIP (Archival Information Package) nello Storage Service di Archivematica, tramite le API REST v2.

| Script | Funzione |
|---|---|
| `aip_client.py` | Client minimale per le API dello Storage Service (libreria condivisa, non eseguibile direttamente) |
| `list_aips.py` | Elenca gli AIP presenti, con filtro per stato ed esportazione CSV |
| `delete_aips.py` | Invia richieste di cancellazione per uno o più AIP |

## ⚠️ Importante: la cancellazione è a due fasi

La cancellazione di un AIP in Archivematica richiede sempre due passaggi:

1. `delete_aips.py` invia la **richiesta** di cancellazione tramite API
2. Un **amministratore deve approvare manualmente** la richiesta dall'interfaccia web dello Storage Service (sezione *Packages*)

Questo secondo passaggio è un controllo di sicurezza intenzionale di Archivematica, per evitare cancellazioni accidentali o non supervisionate: non esiste un endpoint pubblico documentato per automatizzarlo, e questo script non tenta di aggirarlo.

## Installazione

```bash
pip install -r requirements.txt --break-system-packages
```

## Configurazione

Copia `.env.example` in `.env` e compila i valori (oppure esporta le stesse variabili nell'ambiente):

```bash
cp .env.example .env
```

| Variabile | Descrizione |
|---|---|
| `SS_BASE_URL` | URL dello Storage Service (es. `http://localhost:8000`) |
| `SS_USERNAME` | Utente API dello Storage Service |
| `SS_API_KEY` | API key dell'utente (Storage Service → Administration → Users) |
| `SS_PIPELINE_UUID` | UUID della pipeline Archivematica |
| `SS_USER_ID` | ID numerico dell'utente Archivematica (non lo username — vedi nota sotto) |
| `SS_USER_EMAIL` | Email dell'utente Archivematica |

> Per trovare `SS_USER_ID`: accedi al dashboard di Archivematica come amministratore, vai in *Administration → Users*, apri l'utente desiderato e leggi l'ID dalla URL (es. `.../administration/accounts/3/edit/` → `user_id=3`).

## Utilizzo

**1. Elencare gli AIP disponibili**

```bash
python3 list_aips.py
python3 list_aips.py --status UPLOADED
python3 list_aips.py --status DEL_REQ --csv da_rivedere.csv
```

**2. Richiedere la cancellazione**

```bash
# Anteprima senza inviare nulla
python3 delete_aips.py --uuid <uuid1> --reason "Scaduto" --dry-run

# Singolo AIP, con conferma interattiva
python3 delete_aips.py --uuid <uuid1> --reason "Scaduto"

# Più AIP da file (uno per riga, oppure CSV con colonna 'uuid')
python3 delete_aips.py --uuid-file uuids.txt --reason "Retention policy 2026" --yes
```

Ogni richiesta viene registrata in `delete_aips.log` (esito e timestamp).

## Autore e licenza

Autore: Federica Zanardini — Università degli Studi di Milano, Direzione ICT
Sviluppato con il supporto di Claude AI (Anthropic)

Distribuito con licenza [MIT](../../LICENSE).
