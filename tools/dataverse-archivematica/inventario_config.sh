#!/usr/bin/env bash
# =============================================================================
# inventario_config.sh  —  fotografa la configurazione prima dello svuotamento
# Progetto Arkive — Fase 2.  Sola lettura.
# =============================================================================

set -o pipefail
cd "$(dirname "$0")" || exit 1
set -a; source .env 2>/dev/null; set +a

OUT="$HOME/arkive_inventario_$(date +%Y%m%d_%H%M).txt"
mask() { sed -E 's/(API_KEY|PASSWORD|SECRET|TOKEN)[^=]*=.*/\1=***MASCHERATO***/I'; }

VERIFY_OPT=""
[ "${SSL_VERIFY,,}" = "false" ] && VERIFY_OPT="-k"

# --- parser Python salvati su file (evita problemi di quoting inline) ---
cat > /tmp/_inv_loc.py << 'PYEOF'
import json, sys
try:
    d = json.load(sys.stdin)
except Exception as e:
    print("  [errore lettura API]:", e); sys.exit()
for o in d.get("objects", []):
    print("  UUID    :", o.get("uuid"))
    print("    purpose      :", o.get("purpose"))
    print("    enabled      :", o.get("enabled"))
    print("    relative_path:", o.get("relative_path"))
    print("    description  :", o.get("description"))
    print("    space        :", o.get("space"))
    print("    pipeline     :", o.get("pipeline"))
    print()
PYEOF

cat > /tmp/_inv_space.py << 'PYEOF'
import json, sys
try: d = json.load(sys.stdin)
except Exception as e: print("  [errore]:", e); sys.exit()
for o in d.get("objects", []):
    print("  UUID=%s  access_protocol=%s  path=%s  staging_path=%s" % (
        o.get("uuid"), o.get("access_protocol"), o.get("path"), o.get("staging_path")))
PYEOF

cat > /tmp/_inv_pipe.py << 'PYEOF'
import json, sys
try: d = json.load(sys.stdin)
except Exception as e: print("  [errore]:", e); sys.exit()
for o in d.get("objects", []):
    print("  UUID=%s  description=%s  remote_name=%s" % (
        o.get("uuid"), o.get("description"), o.get("remote_name")))
PYEOF

{
echo "================================================================"
echo "  INVENTARIO CONFIGURAZIONE ARCHIVEMATICA — Progetto Arkive"
echo "  data: $(date '+%F %T')   host: $(hostname)"
echo "================================================================"

echo; echo "===== 1. .env (segreti mascherati) ====="
grep -vE '^\s*#|^\s*$' .env | mask

echo; echo "===== 2. Endpoint e riferimenti ====="
echo "AM_URL  = ${AM_URL}"
echo "AM_USER = ${AM_USER}"
echo "SS_URL  = ${SS_URL}"
echo "SS_USER = ${SS_USER}"
echo "AM_TRANSFER_SOURCE_UUID = ${AM_TRANSFER_SOURCE_UUID}"
echo "AM_PROCESSING_CONFIG    = ${AM_PROCESSING_CONFIG}"
echo "AM_PROCESSING_MCP_PATH  = ${AM_PROCESSING_MCP_PATH}"

echo; echo "===== 3. Location nello Storage Service ====="
echo "(purpose: TS=Transfer Source, AS=AIP Storage, DS=DIP, BL=Backlog, SS=interno)"
curl -s $VERIFY_OPT -H "Authorization: ApiKey ${SS_USER}:${SS_API_KEY}" \
     "${SS_URL}/api/v2/location/?limit=100" | python3 /tmp/_inv_loc.py

echo; echo "===== 4. Spaces nello Storage Service ====="
curl -s $VERIFY_OPT -H "Authorization: ApiKey ${SS_USER}:${SS_API_KEY}" \
     "${SS_URL}/api/v2/space/?limit=100" | python3 /tmp/_inv_space.py

echo; echo "===== 5. Pipeline registrate ====="
curl -s $VERIFY_OPT -H "Authorization: ApiKey ${SS_USER}:${SS_API_KEY}" \
     "${SS_URL}/api/v2/pipeline/?limit=100" | python3 /tmp/_inv_pipe.py

echo; echo "===== 6. Percorsi su disco ====="
for p in "$OUTPUT_DIR" "$AM_PROCESSING_MCP_PATH" /mnt/e/ARKIVE/AIPsStore "$HOME/arkive/config"; do
    [ -e "$p" ] && echo "  OK    $p" || echo "  MANCA $p"
done
echo "  Spazio disco:"
df -h /mnt/e /var/archivematica 2>/dev/null | sed 's/^/    /'

echo; echo "===== 7. FPR — PROMEMORIA (riapplicare a mano dopo il wipe) ====="
cat << 'FPR'
  Format version GZip (x-fmt/266, UUID 3a19a758-481f-4fdc-9bb4-4052eb150d62)
      -> Enabled = YES   (conserva il PUID su .rds/.fastq.gz)
  Regola ESTRAZIONE GZip (fprule extract, x-fmt/266)
      -> Enabled = NO    (niente unpacking dei gzip a file singolo)
  Regole estrazione ZIP / 7z / TAR / RAR
      -> Enabled = YES   (invariate)
  Verifica: test .rds (HADWPV) -> nessun unpacking, .rds = GZip x-fmt/266.
  Lotto .tgz: riabilitare la SOLA regola estrazione GZip, poi ridisabilitarla.
FPR

echo; echo "===== 8. Processing configuration ====="
echo "  XML: ${AM_PROCESSING_MCP_PATH}"
if [ -f "${AM_PROCESSING_MCP_PATH}" ]; then
    echo "  presente ($(wc -l < "${AM_PROCESSING_MCP_PATH}") righe) — si reimporta dal Dashboard"
    grep -oE 'appliesTo="[^"]*"' "${AM_PROCESSING_MCP_PATH}" | sort -u | sed 's/^/    /'
else
    echo "  ATTENZIONE: file non trovato"
fi

echo; echo "================================================================"
echo "  FINE INVENTARIO"
echo "================================================================"
} 2>&1 | tee "$OUT"

rm -f /tmp/_inv_loc.py /tmp/_inv_space.py /tmp/_inv_pipe.py
echo; echo ">>> Inventario salvato in: $OUT"
echo ">>> Conservarne una copia FUORI dall'area di lavoro prima del wipe."
