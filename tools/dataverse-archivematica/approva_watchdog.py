#!/usr/bin/env python3
"""
approva_watchdog.py — Approva in continuo i transfer in attesa.

Gira in parallelo alla sequenza di ingest: ogni INTERVALLO secondi interroga
/api/transfer/unapproved/ e approva TUTTO cio' che trova. Cosi' nessun transfer
resta bloccato in attesa se l'auto-approve del singolo ingest fallisce.

Uso:
    python3 approva_watchdog.py            # gira all'infinito (Ctrl-C per fermare)
    python3 approva_watchdog.py --once     # un solo giro e termina

Legge le credenziali da .env nella cartella corrente.
"""
import json
import ssl
import sys
import time
import urllib.request
import urllib.parse
from pathlib import Path

INTERVALLO = 30  # secondi tra un giro e l'altro


def carica_env(path=".env"):
    env = {}
    for line in Path(path).read_text().splitlines():
        line = line.strip()
        if "=" in line and not line.startswith("#"):
            k, v = line.split("=", 1)
            env[k] = v.strip().strip('"').strip("'")
    return env


def ctx_ssl(verify: bool):
    if verify:
        return None
    c = ssl.create_default_context()
    c.check_hostname = False
    c.verify_mode = ssl.CERT_NONE
    return c


def get_unapproved(env, ctx):
    url = env["AM_URL"] + "/api/transfer/unapproved/"
    h = {"Authorization": f"ApiKey {env['AM_USER']}:{env['AM_API_KEY']}"}
    req = urllib.request.Request(url, headers=h)
    with urllib.request.urlopen(req, context=ctx, timeout=30) as r:
        return json.load(r).get("results", [])


def approva(env, ctx, directory, ttype):
    url = env["AM_URL"] + "/api/transfer/approve/"
    h = {"Authorization": f"ApiKey {env['AM_USER']}:{env['AM_API_KEY']}"}
    body = urllib.parse.urlencode({"directory": directory, "type": ttype}).encode()
    req = urllib.request.Request(url, data=body, headers=h)
    with urllib.request.urlopen(req, context=ctx, timeout=30) as r:
        return json.load(r)


def un_giro(env, ctx):
    try:
        pendenti = get_unapproved(env, ctx)
    except Exception as e:
        print(f"[watchdog] errore lettura unapproved: {e}", flush=True)
        return 0
    if not pendenti:
        return 0
    approvati = 0
    for t in pendenti:
        d = t.get("directory", "")
        tp = t.get("type", "standard")
        try:
            resp = approva(env, ctx, d, tp)
            print(f"[watchdog] approvato {d[:55]}  ({resp.get('message','ok')})",
                  flush=True)
            approvati += 1
        except Exception as e:
            print(f"[watchdog] ERRORE approve {d[:55]}: {e}", flush=True)
    return approvati


def main():
    once = "--once" in sys.argv
    env = carica_env()
    verify = env.get("SSL_VERIFY", "true").lower() != "false"
    ctx = ctx_ssl(verify)
    print(f"[watchdog] avviato (intervallo {INTERVALLO}s, once={once})", flush=True)
    if once:
        n = un_giro(env, ctx)
        print(f"[watchdog] approvati {n} transfer.", flush=True)
        return 0
    while True:
        n = un_giro(env, ctx)
        time.sleep(INTERVALLO)


if __name__ == "__main__":
    sys.exit(main() or 0)
