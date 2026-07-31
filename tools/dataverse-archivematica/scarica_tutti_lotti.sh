#!/usr/bin/env bash
# Download di tutti i lotti della ri-acquisizione.
# Non tocca Archivematica: scrive solo file su disco.

cd "$(dirname "$0")" || exit 1
TS=/mnt/e/DROPBOX/Dropbox/_ARKIVE/TRANSFER_SOURCE
mkdir -p logs

echo "avvio: $(date '+%F %T')"
echo "destinazione: $TS"
echo

for f in lotti/dois_lotto_*.txt; do
    n=$(basename "$f" .txt | sed 's/dois_lotto_//')
    echo "===== LOTTO_$n  ($(date '+%F %T')) ====="

    python3 scarica_dataverse.py --dois "$f" \
        --output "$TS/LOTTO_$n" --dcat > "logs/lotto${n}_dl.log" 2>&1
    rc=$?

    err=$(grep -cE "ERRORE|\[FALLBACK\]" "logs/lotto${n}_dl.log")
    tab=$(find "$TS/LOTTO_$n" -name "*.tab" 2>/dev/null | wc -l)
    fil=$(find "$TS/LOTTO_$n" -path "*/objects/*" -type f \
            -not -path "*/metadata/*" 2>/dev/null | wc -l)

    echo "LOTTO_$n: exit=$rc  errori/fallback=$err  file_tab=$tab  file_scaricati=$fil"
    [ "$rc" -ne 0 ] || [ "$err" -ne 0 ] || [ "$tab" -ne 0 ] \
        && echo "  >>> DA CONTROLLARE: logs/lotto${n}_dl.log"
    echo
done

echo "fine: $(date '+%F %T')"
