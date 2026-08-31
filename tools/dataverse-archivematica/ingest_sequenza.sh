#!/usr/bin/env bash
# =============================================================================
# ingest_sequenza.sh  —  ingest di piu' lotti in sequenza, con verifica
#
# Progetto Arkive — Fase ri-acquisizione
#
# Processa i lotti uno dopo l'altro. Per ciascuno registra il conteggio
# "ingested" prima e dopo, e si FERMA se un lotto non produce l'incremento
# atteso (cosi' non accumula problemi su un errore non visto).
#
# ESCLUSIONE DEI 7 DATASET CON ARCHIVI VERI:
# Poiche' archivematica_ingest.py processa la cartella --source INTERA (non
# accetta una lista di DOI), i DOI da escludere vengono SPOSTATI in una
# cartella di parcheggio prima dell'ingest. Restano li', al sicuro, per la
# passata dedicata con la regola di estrazione GZip riattivata.
#
# USO
#   1. Verifica la lista LOTTI e la lista ESCLUDI qui sotto.
#   2. bash ingest_sequenza.sh
#
# Il file di stato e' UNICO e condiviso: rilanciare e' sicuro (riprende).
# =============================================================================

set -o pipefail
cd "$(dirname "$0")" || exit 1

# --- Guardia anti-doppio-lancio ---------------------------------------------
# Due sequenze in parallelo processano gli stessi lotti insieme, generando un
# AIP doppio per ogni DOI (successo reale: NZRX1C ingerito due volte). Questo
# lock impedisce a una seconda istanza di partire mentre la prima e' viva.
LOCK="/tmp/arkive_ingest_sequenza.lock"
if [ -e "$LOCK" ]; then
    pid=$(cat "$LOCK" 2>/dev/null)
    if [ -n "$pid" ] && kill -0 "$pid" 2>/dev/null; then
        echo "ERRORE: un'altra sequenza e' gia' in esecuzione (PID $pid)."
        echo "Se sei certo che non sia cosi', rimuovi $LOCK e rilancia."
        exit 1
    fi
    echo "[lock] trovato lock orfano (PID $pid non attivo), lo rimuovo."
    rm -f "$LOCK"
fi
echo $$ > "$LOCK"
trap 'rm -f "$LOCK"' EXIT
# ----------------------------------------------------------------------------
set -a; source .env 2>/dev/null; set +a

TS="${OUTPUT_DIR%/}"
STATE="stato_riacquisizione.json"
LOGDIR="logs"
PARCHEGGIO="$TS/_ARCHIVI_RINVIATI"
mkdir -p "$LOGDIR"

# ---- LOTTI DA PROCESSARE ORA (tutti tranne il lotto .tgz dedicato) ----
# Lotti leggeri rimasti (23 gia' fatto, +31). PLHGBP(08) e LJ6Z8V(22) a parte.
LOTTI=(24 25 26)

# ---- DOI DA ESCLUDERE ORA (i 7 con archivi veri) ----
# Estrazione archivi DISABILITATA nel FPR: gli archivi si conservano as-is,
# quindi non serve piu' escludere/parcheggiare i dataset con archivi.
ESCLUDI=()
# -----------------------------------------------------------------------

conta_ingested() {
    python3 - "$STATE" << 'PY'
import json, sys, os
p = sys.argv[1]
if not os.path.exists(p):
    print(0); raise SystemExit
d = json.load(open(p))
print(sum(1 for v in d.values() if v.get("status") == "ingested"))
PY
}

doi_attesi() {
    local f="lotti/dois_lotto_${1}.txt"
    [ -f "$f" ] && grep -cE '[^[:space:]]' "$f" || echo 0
}

echo "================================================================"
echo "  INGEST IN SEQUENZA — $(date '+%F %T')"
echo "  Transfer Source : $TS"
echo "  Lotti           : ${LOTTI[*]}"
echo "  DOI esclusi ora : ${ESCLUDI[*]}"
echo "================================================================"

# --- FASE 0: sposta i DOI esclusi nel parcheggio ---
echo
echo ">>> Sposto i ${#ESCLUDI[@]} dataset con archivi nel parcheggio..."
mkdir -p "$PARCHEGGIO"
spostati=0
for sigla in "${ESCLUDI[@]}"; do
    # cerca la cartella del DOI in qualunque lotto
    trovata=$(find "$TS"/LOTTO_* -maxdepth 1 -type d -name "*${sigla}" 2>/dev/null | head -1)
    if [ -n "$trovata" ]; then
        mv "$trovata" "$PARCHEGGIO/"
        echo "    $sigla  <- $(basename "$(dirname "$trovata")")  spostato"
        spostati=$((spostati+1))
    else
        # forse gia' nel parcheggio da un run precedente
        gia=$(find "$PARCHEGGIO" -maxdepth 1 -type d -name "*${sigla}" 2>/dev/null | head -1)
        if [ -n "$gia" ]; then
            echo "    $sigla  gia' nel parcheggio"
        else
            echo "    $sigla  NON TROVATO (ne' nei lotti ne' nel parcheggio)"
        fi
    fi
done
echo "    Spostati ora: $spostati   Parcheggio: $PARCHEGGIO"

# --- FASE 1: ingest dei lotti ---
TOTALE_OK=0
for n in "${LOTTI[@]}"; do
    SRC="$TS/LOTTO_$n"
    LOG="$LOGDIR/lotto_${n}_$(date +%Y%m%d_%H%M).log"

    [ ! -d "$SRC" ] && { echo ">>> LOTTO_$n: cartella assente — SALTO"; continue; }

    # DOI attesi, meno quelli esclusi presenti in questo lotto
    attesi=$(doi_attesi "$n")
    escl_qui=0
    for sigla in "${ESCLUDI[@]}"; do
        grep -q "$sigla" "lotti/dois_lotto_${n}.txt" 2>/dev/null && escl_qui=$((escl_qui+1))
    done
    attesi_eff=$(( attesi - escl_qui ))

    prima=$(conta_ingested)
    echo
    echo ">>> LOTTO_$n  (attesi $attesi_eff DOI, $escl_qui esclusi)  —  $(date '+%H:%M:%S')"

    python3 archivematica_ingest.py --source "$SRC" --auto-approve \
        --api-timeout "${AM_API_TIMEOUT:-600}" \
        --state-file "$STATE" > "$LOG" 2>&1
    rc=$?

    dopo=$(conta_ingested)
    delta=$(( dopo - prima ))
    # quanti DOI erano gia' presenti (SKIP) e quanti errori veri, dal log.
    # Uso wc -l su grep per avere sempre un intero singolo e pulito.
    gia_presenti=$(grep -F "[SKIP]" "$LOG" 2>/dev/null | wc -l | tr -d ' ')
    errori=$(grep -oE "Errori[[:space:]]+:[[:space:]]+[0-9]+" "$LOG" 2>/dev/null | grep -oE "[0-9]+$" | head -1)
    [ -z "$gia_presenti" ] && gia_presenti=0
    [ -z "$errori" ] && errori=0
    echo "    ingested: $prima -> $dopo  (+$delta)   gia_presenti=$gia_presenti  errori=$errori  exit=$rc   log=$LOG"

    # Ci si ferma solo se: exit!=0 con errori veri, OPPURE mancano DOI non
    # spiegati da SKIP. delta + gia_presenti deve coprire gli attesi.
    coperti=$(( delta + gia_presenti ))
    if [ "$rc" -ne 0 ] && [ "$errori" -gt 0 ]; then
        echo
        echo "!!! LOTTO_$n: exit=$rc con $errori errori. MI FERMO."
        echo "!!!   tail -40 $LOG"
        exit 2
    fi
    if [ "$coperti" -lt "$attesi_eff" ]; then
        echo
        echo "!!! LOTTO_$n: coperti $coperti/$attesi_eff DOI (ingeriti $delta + skip $gia_presenti). MI FERMO."
        echo "!!!   tail -40 $LOG"
        echo "!!! Rilancia dopo aver risolto: riprende da dove serve."
        exit 2
    fi
    echo "    LOTTO_$n OK"
    TOTALE_OK=$((TOTALE_OK+1))
done

echo
echo "================================================================"
echo "  SEQUENZA COMPLETATA — $TOTALE_OK lotti"
echo "  ingested totali: $(conta_ingested)   (atteso 664 = 671 - 7 rinviati)"
echo "================================================================"
echo
echo "PROSSIMI PASSI:"
echo "  1. I 7 dataset con archivi sono in: $PARCHEGGIO"
echo "     Vanno ingeriti a parte DOPO aver riabilitato la regola estrazione GZip."
echo "  2. Reindicizzazione Elasticsearch finale: una passata con --delete-all."
echo "  3. Verifica finale: 671 AIP totali su disco/DB/stato."
