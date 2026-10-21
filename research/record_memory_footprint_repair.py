from pathlib import Path
from datetime import datetime,timezone
import hashlib,json,xml.etree.ElementTree as ET
project=Path(r'G:\Shared drives\Xu Lab - Microscopy and Instruments\Automated Microscopy')
source=Path(__file__).parent/'software-next-20261009'
report=project/'research/session-20261011-memory-footprint'
def digest(p):return hashlib.sha256(p.read_bytes()).hexdigest()
files=['flakefinder/memory_cleanup.py','flakefinder/scan_continuation.py','flakefinder/focus_checkpoint.py',
       'flakefinder/sparse_acquisition.py','scripts/watch_scan_memory.ps1','scripts/watch_scan_memory.py']
hashes={name:{'before':digest(report/'source-before'/name) if (report/'source-before'/name).is_file() else None,
              'after':digest(source/name)} for name in files}
suites=ET.parse(report/'validation.xml').getroot().findall('testsuite')
assert sum(int(s.attrib.get('failures',0))+int(s.attrib.get('errors',0)) for s in suites)==0
run=project/'data/scan-20261011-130111-bf685ad5';status=json.loads((run/'scan-status.json').read_text())
note={'at':datetime.now(timezone.utc).isoformat(),'state':'recovery_starting','run_id':run.name,
      'prior_run':'scan-20261011-124430-9a1108f1','worker_pid':status.get('pid'),'phase':status.get('phase'),
      'summary':'Second RAM exhaustion was closed cleanly. Replaced ~200MB persistent PowerShell helper with stdlib watcher; existing checked cleanup runs once per60s at most and exits. Supervisor snapshot measured20MB. Initial focus and independently verified15743.202um local focus are pinned for reuse with fresh local confirmation.',
      'completed_chip_outlines':0,'raster_tiles':0,'interval_minutes':1}
payload={'schema':1,**note,'source_hashes':hashes,'python_tests_passed':sum(int(s.attrib['tests']) for s in suites),
    'powershell_guard_checks_passed':25,'camera_reserve_bytes':1073741824,'capture_working_allowance_bytes':536870912,
    'deadline_at':1791825855.8789244,'coordinate_frame_id':'f32a3bfc-d8e5-4ae9-8218-6d57fa670104',
    'prior_verified_local_focus_z_um':15743.202,'local_focus_reuse_policy':'Three source files hash-pinned into immutable continuation; matching physical XY/optics/ROI; one independent new confirmation, otherwise fresh adaptive focus.'}
(report/'repair-report.json').write_text(json.dumps(payload,indent=2),encoding='utf-8')
folder=run/'supervision';folder.mkdir(exist_ok=True)
(folder/'repair-status.json').write_text(json.dumps(note,indent=2),encoding='utf-8')
(folder/'cadence.json').write_text(json.dumps({'minutes':1,'previous_minutes':1,'reason':'RAM failure recovery startup; require actual focus reuse, capture and boundary progress before backing off.','at':note['at']},indent=2),encoding='utf-8')
print(json.dumps({'report':str(report/'repair-report.json'),'run':run.name,'worker_pid':status.get('pid'),'phase':status.get('phase')}))
