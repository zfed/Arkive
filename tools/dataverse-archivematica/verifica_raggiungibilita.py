#!/usr/bin/env python3
# =============================================================================
# verifica_raggiungibilita.py  —  strumento di sola lettura (Fase 0 migrazione)
#
# Progetto Arkive — Università degli Studi di Milano, Direzione ICT
#
# SCOPO
#   Prima di azzerare gli AIP sperimentali e ri-acquisire tutto con la pipeline
#   corretta (download degli originali), verifica per ogni DOI dello scope che:
#     - il dataset risponda ancora su Dataverse (non rimosso/deaccessionato)
#     - sia pubblicato/accessibile (segnala bozze, accesso negato, file ristretti)
#     - quanti file contiene e QUALE VOLUME avra' la ri-acquisizione con gli
#       ORIGINALI (usa originalFileSize per gli ingeriti, fileSize altrimenti).
#       Il conteggio copre TUTTE LE VERSIONI (endpoint /versions), perche' la
#       pipeline scarica ogni versione in una cartella separata: il volume su
#       disco e' la somma delle versioni, non quello dell'ultima.
#
#   Non cancella e non scrive nulla su Dataverse: esegue solo GET. L'unico file
#   scritto in locale e' il report JSON (e i DOI non raggiungibili in un .txt).
#   La decisione sui DOI non raggiungibili resta MANUALE (potrebbero essere
#   l'unico esemplare rimasto nel vecchio AIP).
#
# USO
#   cd tools/dataverse-archivematica
#   # I parametri vengono letti dal file .env della cartella corrente (stesso
#   # .env della pipeline: DATAVERSE_URL, DATAVERSE_API_KEY, SSL_VERIFY). Le
#   # variabili esportate nella shell hanno comunque la precedenza sul .env.
#   python3 verifica_raggiungibilita.py [lista_doi.txt] [--sleep 0.1]
#
#   Default lista: dois_scope.txt
#   Report:        reacquisizione_report.json   (resumibile: skip dei DOI gia' fatti)
#   Non raggiung.: doi_non_raggiungibili.txt
#
# Dipendenza: requests (gia' usata dalla pipeline).
# =============================================================================

import argparse
import json
import os
import sys
import time

import requests
import urllib3


def _load_dotenv(env_file: str = ".env") -> None:
    """
    Carica variabili da un file .env senza dipendenze esterne (stesso
    comportamento di scarica_dataverse.py). Formato: KEY=value, ignora righe
    vuote e commenti (#). Le variabili gia' presenti nell'ambiente NON vengono
    sovrascritte (la shell ha la precedenza sul .env).
    """
    if not os.path.isfile(env_file):
        return
    with open(env_file, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            key   = key.strip()
            value = value.strip().strip('"').strip("'")
            if key and key not in os.environ:
                os.environ[key] = value


# IMPORTANTE: caricare il .env PRIMA di leggere le costanti che ne dipendono.
_load_dotenv()

BASE_URL   = os.environ.get("DATAVERSE_URL", "https://dataverse.unimi.it").rstrip("/")
API_KEY    = os.environ.get("DATAVERSE_API_KEY", "")
SSL_VERIFY = os.environ.get("SSL_VERIFY", "true").lower() not in ("false", "0", "no")

REPORT_PATH   = "reacquisizione_report.json"
UNREACH_PATH  = "doi_non_raggiungibili.txt"
RETRIES       = 4
BACKOFF_BASE  = 3      # secondi: attesa = BACKOFF_BASE * tentativo


def _headers() -> dict:
    return {"X-Dataverse-key": API_KEY} if API_KEY else {}


def _e_404_autentico(r):
    """
    Distingue un 404 EMESSO DA DATAVERSE (dataset realmente inesistente) da un
    404 spurio prodotto da proxy/WAF/rate-limiting.

    Restituisce (autentico: bool, messaggio: str|None).

    Il discriminante affidabile e' la presenza di un CORPO JSON di Dataverse
    (`{"status": "ERROR", "message": ...}`): un blocco intermedio restituisce
    HTML, corpo vuoto o non-JSON, mai il JSON dell'applicazione.

    NB: NON si puo' filtrare sul testo del messaggio. Verificato su
    dataverse.unimi.it: per un DOI inesistente questa installazione risponde
        {"status":"ERROR","message":"When accessing a dataset based on
         Persistent ID, a persistentId query parameter must be present."}
    cioe' un messaggio fuorviante che non contiene "not found" (difetto noto
    di alcune versioni Dataverse nella gestione dell'eccezione). Filtrare su
    "not found" faceva sfuggire le rimozioni reali.

    Nel dubbio si considera NON autentico -> si ritenta -> DA_RIVERIFICARE.
    """
    ctype = (r.headers.get("Content-Type") or "").lower()
    if "json" not in ctype:
        return False, None
    try:
        body = r.json()
    except ValueError:
        return False, None
    if not isinstance(body, dict) or body.get("status") != "ERROR":
        return False, None
    return True, body.get("message")


def _get_dataset(doi: str):
    """
    GET del dataset via native API.

    Restituisce (esito, payload) dove esito e':
      200          -> payload = json del dataset
      404          -> 404 AUTENTICO di Dataverse (dataset inesistente)
      401 / 403    -> accesso negato (definitivo)
      "TRANSITORIO"-> 404 spurio, 429, 5xx o errore di rete dopo tutti i retry
                      (NON e' una prova di rimozione: va riverificato)
    """
    if not SSL_VERIFY:
        urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
    url = f"{BASE_URL}/api/datasets/:persistentId/versions"
    dettaglio = None
    for tentativo in range(1, RETRIES + 1):
        try:
            r = requests.get(url, params={"persistentId": doi}, headers=_headers(),
                             timeout=60, verify=SSL_VERIFY)

            if r.status_code == 200:
                try:
                    return 200, r.json()
                except ValueError:
                    dettaglio = "200 con corpo non-JSON (probabile pagina proxy)"

            elif r.status_code == 404:
                autentico, messaggio = _e_404_autentico(r)
                if autentico:
                    return 404, messaggio     # rimozione reale: definitivo
                dettaglio = "404 senza corpo JSON di Dataverse (proxy/rate-limit?)"

            elif r.status_code in (401, 403):
                return r.status_code, None    # definitivo

            elif r.status_code == 429 or r.status_code >= 500:
                dettaglio = f"HTTP {r.status_code} (throttling o errore server)"

            else:
                dettaglio = f"HTTP {r.status_code}"

        except requests.exceptions.RequestException as e:
            dettaglio = f"errore di rete: {e}"

        # Caso transitorio: attesa progressiva e nuovo tentativo
        if tentativo < RETRIES:
            time.sleep(BACKOFF_BASE * tentativo)

    return "TRANSITORIO", dettaglio


def _size_acquisizione(df: dict) -> int:
    """Dimensione che avra' il file ri-acquisito: originale se ingerito."""
    if df.get("originalFileName") or df.get("originalFileFormat"):
        return int(df.get("originalFileSize") or df.get("fileSize")
                   or df.get("filesize") or 0)
    return int(df.get("fileSize") or df.get("filesize") or 0)


def _size_tab(df: dict) -> int:
    return int(df.get("fileSize") or df.get("filesize") or 0)


def analizza(doi: str) -> dict:
    status, payload = _get_dataset(doi)

    if status == 200:
        # L'endpoint /versions restituisce la LISTA di tutte le versioni.
        # La pipeline scarica ogni versione in una cartella separata
        # (objects/<versione>/objects/), quindi i file si ripetono a ogni
        # versione: il volume su disco e' la SOMMA su tutte le versioni, non
        # quello dell'ultima.
        versioni = payload.get("data", [])
        if not isinstance(versioni, list):
            versioni = []

        n_file_tot = n_ing = n_restr = 0
        size_orig = size_tab = 0
        for ver in versioni:
            for f in ver.get("files", []):
                df = f.get("dataFile", {})
                n_file_tot += 1
                if df.get("originalFileName") or df.get("originalFileFormat"):
                    n_ing += 1
                if f.get("restricted"):
                    n_restr += 1
                size_orig += _size_acquisizione(df)
                size_tab  += _size_tab(df)

        # Vuoto = nessun file in NESSUNA versione (stesso criterio di
        # _dataset_has_files() in scarica_dataverse.py).
        stato = "OK" if n_file_tot else "VUOTO"

        # Dati dell'ultima versione, utili come riferimento rapido.
        ultima      = versioni[0] if versioni else {}
        n_file_ult  = len(ultima.get("files", []))

        return {
            "doi": doi, "stato": stato,
            "versionState": ultima.get("versionState"),
            "n_versioni": len(versioni),
            "n_file_ultima": n_file_ult,
            "n_file": n_file_tot,          # totale su tutte le versioni
            "n_ingeriti": n_ing, "n_ristretti": n_restr,
            "size_acquisizione": size_orig, "size_tab": size_tab,
        }

    if status == 404:
        # 404 autentico di Dataverse: il dataset non esiste piu'.
        return {"doi": doi, "stato": "NON_RAGGIUNGIBILE", "http": 404,
                "messaggio_server": payload}
    if status in (401, 403):
        return {"doi": doi, "stato": "ACCESSO_NEGATO", "http": status}
    # Esito transitorio (404 spurio, throttling, rete): NON e' una prova di
    # rimozione. Va riverificato e non entra mai in doi_non_raggiungibili.txt.
    return {"doi": doi, "stato": "DA_RIVERIFICARE", "dettaglio": str(payload)}


def _fmt_gb(n_byte: int) -> str:
    return f"{n_byte / (1024**3):.2f} GB"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("lista", nargs="?", default="dois_scope.txt")
    ap.add_argument("--sleep", type=float, default=0.3,
                    help="pausa in secondi tra un DOI e l'altro (default 0.3, "
                         "riduce il rischio di rate-limiting su liste lunghe)")
    ap.add_argument("--force", action="store_true",
                    help="ri-controlla anche i DOI gia' conclusi nel report")
    args = ap.parse_args()

    if not os.path.isfile(args.lista):
        print(f"ERRORE: lista non trovata: {args.lista}", file=sys.stderr)
        return 1

    dois = []
    with open(args.lista, encoding="utf-8") as fh:
        for riga in fh:
            riga = riga.strip()
            if not riga or riga.startswith("#"):
                continue
            dois.append(riga if riga.startswith("doi:") else f"doi:{riga}")
    # dedup preservando l'ordine
    visti = set()
    dois = [d for d in dois if not (d in visti or visti.add(d))]

    # Resume: carica risultati precedenti. Gli stati NON definitivi
    # (DA_RIVERIFICARE) vengono comunque ritentati al run successivo.
    STATI_DEFINITIVI = ("OK", "VUOTO", "NON_RAGGIUNGIBILE", "ACCESSO_NEGATO")
    risultati: dict = {}
    if os.path.isfile(REPORT_PATH) and not args.force:
        try:
            for r in json.load(open(REPORT_PATH, encoding="utf-8")).get("dettaglio", []):
                risultati[r["doi"]] = r
        except Exception:
            risultati = {}

    da_ritentare = sum(1 for r in risultati.values()
                       if r.get("stato") not in STATI_DEFINITIVI)

    print(f"Istanza    : {BASE_URL}")
    print(f"API key    : {'presente' if API_KEY else 'assente'}")
    print(f"DOI totali : {len(dois)}   (gia' conclusi: "
          f"{len(risultati) - da_ritentare}, da ritentare: {da_ritentare})")
    print("-" * 60)

    for i, doi in enumerate(dois, 1):
        pregresso = risultati.get(doi)
        if (pregresso and not args.force
                and pregresso.get("stato") in STATI_DEFINITIVI):
            continue
        res = analizza(doi)
        risultati[doi] = res
        nota = res.get("stato")
        if res.get("stato") in ("OK", "VUOTO"):
            nota += (f"  ver={res.get('n_versioni', '?')}"
                     f" file={res['n_file']} ingeriti={res['n_ingeriti']}")
            if res.get("n_ristretti"):
                nota += f" RISTRETTI={res['n_ristretti']}"
            nota += f"  ~{_fmt_gb(res['size_acquisizione'])}"
        print(f"[{i}/{len(dois)}] {doi.split('/')[-1]:10s} -> {nota}")

        # salvataggio incrementale (resumibilita')
        if i % 20 == 0:
            _salva(risultati)
        if args.sleep:
            time.sleep(args.sleep)

    _salva(risultati)
    _riepilogo(risultati)
    return 0


def _salva(risultati: dict) -> None:
    ordinati = list(risultati.values())
    with open(REPORT_PATH, "w", encoding="utf-8") as fh:
        json.dump({"dettaglio": ordinati}, fh, indent=2, ensure_ascii=False)
    non_ragg = [r["doi"] for r in ordinati
                if r["stato"] in ("NON_RAGGIUNGIBILE", "ACCESSO_NEGATO")]
    if non_ragg:
        with open(UNREACH_PATH, "w", encoding="utf-8") as fh:
            fh.write("\n".join(non_ragg) + "\n")


def _riepilogo(risultati: dict) -> None:
    vals = list(risultati.values())
    def conta(s): return sum(1 for r in vals if r["stato"] == s)
    ok       = conta("OK")
    vuoti    = conta("VUOTO")
    nonragg  = conta("NON_RAGGIUNGIBILE")
    negato   = conta("ACCESSO_NEGATO")
    riverif  = conta("DA_RIVERIFICARE")
    tot_size = sum(r.get("size_acquisizione", 0) for r in vals)
    tot_tab  = sum(r.get("size_tab", 0) for r in vals)
    ristretti = sum(1 for r in vals if r.get("n_ristretti"))

    print("\n" + "=" * 60)
    print("RIEPILOGO")
    print(f"  DOI totali analizzati        : {len(vals)}")
    print(f"  OK (con file)                : {ok}")
    print(f"  VUOTI (bozza/embargo/nofile) : {vuoti}")
    print(f"  NON RAGGIUNGIBILI (404 reale): {nonragg}   -> vedi {UNREACH_PATH}")
    print(f"  ACCESSO NEGATO               : {negato}")
    print(f"  DA RIVERIFICARE (transitori) : {riverif}")
    print(f"  DOI con file RISTRETTI       : {ristretti}")
    print(f"  Versioni totali da scaricare : {sum(r.get('n_versioni', 0) for r in vals)}")
    print(f"  File totali da scaricare     : {sum(r.get('n_file', 0) for r in vals)}")
    print(f"  Volume ri-acquisizione (orig): {_fmt_gb(tot_size)}  (tutte le versioni)")
    print(f"  Volume equivalente in .tab   : {_fmt_gb(tot_tab)}  (tutte le versioni)")

    if riverif:
        print(f"\n  {riverif} DOI hanno dato esiti TRANSITORI (404 senza corpo")
        print("  Dataverse, throttling, rete). NON sono prove di rimozione e non")
        print("  entrano in " + UNREACH_PATH + ". Rilancia lo strumento per")
        print("  ritentarli (il resume li riprende in automatico); se restano")
        print("  molti, aumenta la pausa: --sleep 1")
    if nonragg or negato:
        print("\n  ATTENZIONE: i DOI non raggiungibili / accesso negato vanno")
        print("  valutati a MANO prima di azzerare i rispettivi AIP: potrebbero")
        print("  essere l'unico esemplare rimasto.")
    if riverif == 0 and (nonragg or negato):
        print("\n  Nessun esito transitorio pendente: il quadro e' stabile.")


if __name__ == "__main__":
    sys.exit(main())
