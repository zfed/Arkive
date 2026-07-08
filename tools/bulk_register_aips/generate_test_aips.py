#!/usr/bin/env python3
"""
generate_test_aips.py

Genera AIP sintetici ma strutturalmente realistici (con METS interno
valido) per testare bulk_register_aips.py, secondo il piano di test in
docs/PIANO_TEST.md.

Copre:
  - Test 2: cartella "ereditata" da un'altra istanza Archivematica, già
    organizzata a quadranti (pairtree).
  - Test 3: cartella piatta stile a3m/DART3, con tre sotto-casi:
      a) nome file standard <nome>-<uuid>.<ext>, UUID coerente col METS
         interno (nessun conflitto atteso)
      b) nome file standard ma con UUID DIVERSO da quello nel METS
         interno (conflitto atteso)
      c) nome file non standard, senza UUID riconoscibile (richiede il
         fallback via METS)

Non copre il Test 1: quello richiede un AIP realmente già registrato
sulla tua Storage Service, che non si può simulare qui - vedi le
istruzioni date in chat.

Uso:
    python3 generate_test_aips.py --out-dir /tmp/test_bulk_register --scenario all
    python3 generate_test_aips.py --out-dir /tmp/test_bulk_register --scenario 2 --count 4
    python3 generate_test_aips.py --out-dir /tmp/test_bulk_register --scenario 3
"""

import argparse
import io
import os
import shutil
import tarfile
import uuid as uuid_lib
import zipfile

METS_TEMPLATE = """<?xml version="1.0" encoding="UTF-8"?>
<mets:mets xmlns:mets="http://www.loc.gov/METS/"
           xmlns:xlink="http://www.w3.org/1999/xlink"
           OBJID="urn:uuid:{uuid}">
  <mets:metsHdr CREATEDATE="2024-01-01T00:00:00">
    <mets:agent ROLE="CREATOR">
      <mets:name>generate_test_aips.py (dati di test sintetici)</mets:name>
    </mets:agent>
  </mets:metsHdr>
  <mets:dmdSec ID="dmdSec_1">
    <mets:mdWrap MDTYPE="DC">
      <mets:xmlData>
        <dc:title xmlns:dc="http://purl.org/dc/elements/1.1/">AIP di test sintetico</dc:title>
      </mets:xmlData>
    </mets:mdWrap>
  </mets:dmdSec>
  <mets:fileSec>
    <mets:fileGrp USE="original">
      <mets:file ID="file-{file_uuid}" GROUPID="Group-{file_uuid}">
        <mets:FLocat xlink:href="objects/oggetto_test.bin"
                     LOCTYPE="OTHER" OTHERLOCTYPE="SYSTEM"/>
      </mets:file>
    </mets:fileGrp>
  </mets:fileSec>
  <mets:structMap TYPE="physical" LABEL="Archivematica default">
    <mets:div TYPE="Directory" LABEL="aip-{uuid}">
      <mets:div TYPE="Directory" LABEL="objects">
        <mets:div TYPE="Item" LABEL="oggetto_test.bin">
          <mets:fptr FILEID="file-{file_uuid}"/>
        </mets:div>
      </mets:div>
    </mets:div>
  </mets:structMap>
</mets:mets>
"""


def make_mets_bytes(mets_uuid):
    """Il METS include un fileSec/structMap minimale con UN file 'original':
    senza almeno un file indicizzabile, l'AIP non comparirebbe MAI nella
    ricerca Archival storage del Dashboard (la cui query parte dall'indice
    'aipfiles' e aggrega gli UUID degli AIP da li'; un AIP con zero file
    indicizzati e' presente nell'indice 'aips' ma invisibile nella UI).
    L'UUID del file e' incluso nell'attributo ID del mets:file, da cui il
    parser di indicizzazione lo estrae senza bisogno di un amdSec."""
    file_uuid = str(uuid_lib.uuid4())
    return METS_TEMPLATE.format(uuid=mets_uuid, file_uuid=file_uuid).encode("utf-8")


def new_uuid():
    return str(uuid_lib.uuid4())


def create_archive(output_path, internal_root, mets_uuid, archive_format="tar.gz"):
    """Crea un archivio con la struttura tipica di un AIP Archivematica:

        <internal_root>/
        |-- bagit.txt
        |-- bag-info.txt
        |-- data/
            |-- METS.<mets_uuid>.xml
            |-- objects/
                |-- oggetto_test.bin

    IMPORTANTE: le voci di tipo DIRECTORY vengono aggiunte esplicitamente.
    La Storage Service (get_base_directory) determina la cartella radice del
    pacchetto elencando le directory dell'archivio con lsar: un archivio con
    sole voci-file (come quelli creati ingenuamente con tarfile/zipfile)
    non ha nessuna voce directory e manda in errore l'estrazione (HTTP 500
    su extract_file, e quindi anche l'indicizzazione nel Dashboard).
    """
    dummy_object = b"contenuto fittizio di un oggetto digitale di test"
    mets_bytes = make_mets_bytes(mets_uuid)
    bagit_txt = b"BagIt-Version: 0.97\nTag-File-Character-Encoding: UTF-8\n"
    bag_info = (b"Bagging-Date: 2024-01-01\n"
                b"Payload-Oxum: %d.2\n" % (len(dummy_object) + len(mets_bytes)))

    directories = [
        f"{internal_root}",
        f"{internal_root}/data",
        f"{internal_root}/data/objects",
    ]
    files = [
        (f"{internal_root}/bagit.txt", bagit_txt),
        (f"{internal_root}/bag-info.txt", bag_info),
        (f"{internal_root}/data/METS.{mets_uuid}.xml", mets_bytes),
        (f"{internal_root}/data/objects/oggetto_test.bin", dummy_object),
    ]

    if archive_format == "zip":
        with zipfile.ZipFile(output_path, "w") as z:
            for d in directories:
                z.writestr(d + "/", "")  # voce directory esplicita
            for name, data in files:
                z.writestr(name, data)
    elif archive_format == "tar.gz":
        with tarfile.open(output_path, "w:gz") as t:
            for d in directories:
                info = tarfile.TarInfo(name=d)
                info.type = tarfile.DIRTYPE
                info.mode = 0o755
                t.addfile(info)
            for name, data in files:
                _add_bytes_to_tar(t, name, data)
    else:
        raise ValueError(f"formato non supportato in questo generatore: {archive_format}")


def _add_bytes_to_tar(tarf, arcname, data):
    info = tarfile.TarInfo(name=arcname)
    info.size = len(data)
    tarf.addfile(info, io.BytesIO(data))


def compute_quadrant_dir(root, aip_uuid):
    """8 blocchi da 4 caratteri = UUID intero (32 hex); il file va
    direttamente nell'ultimo quadrante, senza cartella UUID finale.
    Schema verificato sul current_path reale della Storage Service UNIMI."""
    hex_str = aip_uuid.replace("-", "")
    quadrants = [hex_str[i:i + 4] for i in range(0, 32, 4)]
    return os.path.join(root, *quadrants)


# ---------------------------------------------------------------------------
# Test 2: store ereditato, gia' a quadranti
# ---------------------------------------------------------------------------

def generate_scenario_2(out_dir, count):
    scenario_dir = os.path.join(out_dir, "test2_ereditati")
    os.makedirs(scenario_dir, exist_ok=True)
    created = []
    for i in range(count):
        aip_uuid = new_uuid()
        name = f"dataset-ereditato-{i+1:02d}"
        filename = f"{name}-{aip_uuid}.tar.gz"
        qdir = compute_quadrant_dir(scenario_dir, aip_uuid)
        os.makedirs(qdir, exist_ok=True)
        full_path = os.path.join(qdir, filename)
        create_archive(full_path, f"{name}-{aip_uuid}", aip_uuid, archive_format="tar.gz")
        created.append((aip_uuid, full_path))
    print(f"[Test 2] Creati {count} AIP già a quadranti in: {scenario_dir}")
    for u, p in created:
        print(f"    {u}  ->  {os.path.relpath(p, scenario_dir)}")
    return scenario_dir


# ---------------------------------------------------------------------------
# Test 3: a3m/DART3, cartella piatta, 3 sotto-casi
# ---------------------------------------------------------------------------

def generate_scenario_3(out_dir):
    scenario_dir = os.path.join(out_dir, "test3_a3m_dart3")
    os.makedirs(scenario_dir, exist_ok=True)

    report_lines = []

    # a) nome standard, UUID coerente col METS -> nessun conflitto
    for i in range(2):
        aip_uuid = new_uuid()
        name = f"a3m-dataset-{i+1:02d}"
        filename = f"{name}-{aip_uuid}.zip"
        full_path = os.path.join(scenario_dir, filename)
        create_archive(full_path, f"{name}-{aip_uuid}", aip_uuid, archive_format="zip")
        report_lines.append(f"  [a - coerente]      {filename}  (uuid atteso: {aip_uuid})")

    # b) nome standard, UUID diverso dal METS -> conflitto atteso
    filename_uuid = new_uuid()
    mets_uuid = new_uuid()
    name = "a3m-dataset-conflitto"
    filename = f"{name}-{filename_uuid}.zip"
    full_path = os.path.join(scenario_dir, filename)
    create_archive(full_path, f"{name}-{mets_uuid}", mets_uuid, archive_format="zip")
    report_lines.append(f"  [b - CONFLITTO]     {filename}")
    report_lines.append(f"                        uuid nel nome file: {filename_uuid}")
    report_lines.append(f"                        uuid nel METS:      {mets_uuid}")

    # c) nome non standard, senza UUID -> richiede fallback via METS
    aip_uuid = new_uuid()
    filename = "esportazione_dart3_pacchetto_007.tar.gz"
    full_path = os.path.join(scenario_dir, filename)
    create_archive(full_path, f"pacchetto_dart3_{aip_uuid}", aip_uuid, archive_format="tar.gz")
    report_lines.append(f"  [c - fallback METS] {filename}  (uuid solo nel METS: {aip_uuid})")

    print(f"[Test 3] Creati AIP di test in: {scenario_dir}")
    for line in report_lines:
        print(line)
    return scenario_dir


def main():
    parser = argparse.ArgumentParser(
        description="Genera AIP sintetici per testare bulk_register_aips.py"
    )
    parser.add_argument("--out-dir", required=True,
                         help="Cartella base dove creare le sottocartelle di test")
    parser.add_argument("--scenario", choices=["2", "3", "all"], default="all",
                         help="Quale scenario generare (default: all)")
    parser.add_argument("--count", type=int, default=3,
                         help="Numero di AIP da generare per il Test 2 (default: 3)")
    parser.add_argument("--clean", action="store_true",
                         help="Cancella prima le sottocartelle di test già esistenti in --out-dir")
    args = parser.parse_args()

    os.makedirs(args.out_dir, exist_ok=True)

    if args.clean:
        for sub in ("test2_ereditati", "test3_a3m_dart3"):
            path = os.path.join(args.out_dir, sub)
            if os.path.exists(path):
                shutil.rmtree(path)
                print(f"Rimossa cartella precedente: {path}")

    if args.scenario in ("2", "all"):
        generate_scenario_2(args.out_dir, args.count)
    if args.scenario in ("3", "all"):
        generate_scenario_3(args.out_dir)

    print("\nFatto. Ricorda: questi sono dati FITTIZI, generati solo per testare "
          "la logica dello script (non sono AIP validi da un punto di vista "
          "archivistico/di conformità Archivematica).")


if __name__ == "__main__":
    main()
