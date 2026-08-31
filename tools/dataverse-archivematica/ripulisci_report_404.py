import json, shutil, os
p='archivi_report.json'
if not os.path.isfile(p):
    print("archivi_report.json non trovato: eseguire dalla cartella tools/dataverse-archivematica")
    raise SystemExit(1)
shutil.copy(p, p+'.bak')
d=json.load(open(p))['dettaglio']
# i 6 accertati inesistenti (verificati in isolamento con doppio tentativo)
CONFERMATI={'QA4BRO','BX8AIN','YF2NZV','WKMYTV','YIAURF','OYDM83'}
tenuti, rimossi = [], []
for r in d:
    sigla=r['doi'].split('/')[-1]
    if r.get('stato')!='OK' and sigla not in CONFERMATI:
        rimossi.append(r['doi'])
    else:
        tenuti.append(r)
json.dump({'dettaglio':tenuti}, open(p,'w'), indent=2, ensure_ascii=False)
print(f"voci rimosse (da ricontrollare): {len(rimossi)}")
for x in rimossi: print("  ", x)
print(f"voci tenute: {len(tenuti)}")
