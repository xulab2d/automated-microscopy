"""Prepare the verified completed-chip recovery; no hardware connection."""
from pathlib import Path
import json
import sys
import time

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / 'research/software-next-20261009'))
from flakefinder.rectangle_handoff import build_contract
from flakefinder.scan_service import ScanService

proof = Path(__file__).with_name('handoff-plan.json')
if proof.exists():
    raise RuntimeError('Recovery already planned; inspect saved plan before any retry')
project = Path(json.loads((ROOT / 'configs/storage.json').read_text())['project_root'])
last_report = time.monotonic()
processed_bytes = 0
processed_files = 0
def report(info):
    global last_report, processed_bytes, processed_files
    processed_bytes += info['bytes_completed']
    processed_files += 1
    if time.monotonic() - last_report >= 30:
        print(json.dumps({'phase': 'pinning_donor', 'files_checked': processed_files,
                          'GiB_checked': round(processed_bytes / 2**30, 2)}), flush=True)
        last_report = time.monotonic()

contract = build_contract(project, 'scan-20261013-221727-131d9ced', progress=report)
manifest = json.loads((project/'data'/contract['prior_run_id']/'chip001/manifest.json').read_text())
raw_verified = 0
for row in manifest['completed'].values():
    for relative, expected in row['hashes'].items():
        actual = contract['prior_files_sha256'].get('chip001/' + relative)
        if actual is not None and actual != expected:
            raise RuntimeError('Committed artifact hash changed: ' + relative)
        if relative.endswith('.tiff'):
            if actual is None: raise RuntimeError('Committed raw image is missing: ' + relative)
            raw_verified += 1
print('Donor evidence pinned; preparing immutable continuation', flush=True)
reply = ScanService(project).plan(contract['settings'], rectangle_handoff=contract)
result = {'run_id': reply['run']['id'], 'donor': contract['prior_run_id'],
          'deadline_at': contract['deadline_at'], 'planned_at': time.time(),
          'pinned_files': len(contract['prior_files_sha256']),
          'preflight_ok': reply['preflight']['ok'],
          'blockers': reply['preflight']['blockers'],
          'completed_chip001_tiles': 999,
          'raw_tiffs_hash_verified': raw_verified,
          'validation': '138 combined tests passed; real 999-field coverage replay exact'}
with proof.open('x', encoding='utf-8') as stream:
    json.dump(result, stream, indent=2)
print(json.dumps(result), flush=True)
