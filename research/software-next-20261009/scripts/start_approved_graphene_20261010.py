"""One explicit launch after the diagnosed, clean RAM stop. No hardware imports."""
from datetime import datetime, timezone
from pathlib import Path
import hashlib
import json
import math
import sys
import time

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from flakefinder.scan_service import ScanService, memory_status, process_alive, read_json, write_json

project = Path(r'G:\Shared drives\Xu Lab - Microscopy and Instruments\Automated Microscopy')
service = ScanService(project)
previous = 'scan-20261010-024348-16270a07'
status = read_json(service.folder(previous)/'scan-status.json')
assert status['hardware_closed'] is True and not process_alive(status['pid'])
assert status['state'] == 'stopped' and status['error'] == 'MemoryError: Available RAM is below the 2 GiB acquisition floor.'
assert (service.folder(previous)/'STOP').read_text().strip() == status['error']
available, total = memory_status()
if available < 2.8*1024**3:
    raise RuntimeError(f'Insufficient launch margin after resource stop: {available/1024**3:.2f} GiB')
settings = dict(service.request(previous)['settings'])
cutoff = datetime(2026, 10, 10, 16, 0, tzinfo=timezone.utc).timestamp()
settings['hours'] = math.floor((cutoff-time.time()-120)/60)/60
assert settings['hours'] > .05
assert [settings[k] for k in ('width_mm','height_mm','z_below_um','z_above_um')] == [56,56,25,25]
preflight = service.preflight(settings)
if not preflight['ok']:
    raise RuntimeError('; '.join(preflight['blockers']))
planned = service.plan(settings)
run_id = planned['run']['id']
source = Path(__file__).resolve().parents[1]/'flakefinder'
write_json(service.folder(run_id)/'launch-review.json', {
    'user_confirmation':'explicit_exact_travel_clearance',
    'user_answer':'Yes—I explicitly approve those exact limits; start the scan',
    'limits':{'width_mm':56,'height_mm':56,'z_below_um':25,'z_above_um':25},
    'replaces_clean_resource_stop':previous,
    'prior_stop_preserved':True,
    'available_ram_before_launch_bytes':available,
    'morning_cutoff_utc':'2026-10-10T16:00:00Z',
    'acquisition_reserve_unchanged_bytes':2*1024**3,
    'memory_patch_source_sha256':{name:hashlib.sha256((source/name).read_bytes()).hexdigest()
        for name in ('scan_worker.py','layout.py','chip_discovery.py','photometric_edges.py')}
}, exclusive=True)
result = service.start(run_id, release_hold=True)
write_json(service.folder(previous)/'replacement-run.json', {'id':run_id,'reason':'clean memory stop; redundant camera buffer allocations removed','at':time.time()}, exclusive=True)
print(json.dumps(result, allow_nan=False))
