from pathlib import Path
import sys,json,hashlib,shutil
from datetime import datetime,timezone
src=Path(__file__).parent/'software-next-20261009';sys.path.insert(0,str(src))
from flakefinder.scan_continuation import build_contract,verify_contract
from flakefinder.pending_focus import find_pending
from flakefinder.imaging import load_frame
from flakefinder.memory_capacity import snapshot,assess,capture_working_bytes,STARTUP_WORKING_BYTES
project=Path(r'G:\Shared drives\Xu Lab - Microscopy and Instruments\Automated Microscopy')
old=project/'data/scan-20261011-133357-4a1271ad';root=project/'research/session-20261011-native-memory'
c=build_contract(project,old.name,-25000.,z_max_um=25000.,focus_travel_policy='controller');verify_contract(project,c)
(root/'validated-contract.json').write_text(json.dumps(c,indent=2))
new=project/'data'/json.loads((old/'supervision/adaptive-focus-continuation.json').read_text())['new_run']
request=json.loads((new/'scan-request.json').read_text());assert request['continuation']==c
assert not (new/'worker-start.json').exists()
ref=load_frame(old/'calibration/substrate.tiff')
pending=find_pending(new/'discovery/candidate002',{'prior_run_id':old.name},[3497.503,-21485.085],ref)
assert pending and len(pending['samples'])==5 and abs(pending['fitted']['z_um']-15676.776593407369)<.001
assert c['preserve_pre_raster_progress'] and c['deadline_at']==1791825855.8789244
discovery=json.loads((old/'discovery/candidate002/discovery.json').read_text())
assert len(discovery['imported_records'])==7 and sum(bool(r.get('edge')) for r in discovery['imported_records'])==3
names=['flakefinder/memory_capacity.py','flakefinder/acquisition_resources.py','flakefinder/scan_service.py','flakefinder/scan_worker.py','flakefinder/zen.py','flakefinder/pending_focus.py','flakefinder/scan_continuation.py','flakefinder/sparse_acquisition.py','flakefinder/memory_cleanup.py','scripts/watch_scan_memory.py','scripts/native_memory_actions.py']
memory=snapshot();report=json.loads((root/'repair-report.json').read_text())
report.update(state='validated_offline',planned_run=new.name,at=datetime.now(timezone.utc).isoformat(),tests={'integrated_python_passed':179,'targeted_followup_passed':86,'note':'Overlapping suites; not a count of unique tests.'},
 source_after_sha256={n:hashlib.sha256((src/n).read_bytes()).hexdigest() for n in names},
 memory_policy={'fixed_free_physical_reserve_bytes':0,'camera':assess(memory,capture_working_bytes(int(ref.rgb.nbytes)),purpose='camera capture'),
 'startup':assess(memory,STARTUP_WORKING_BYTES,purpose='startup'),'helper':'native ctypes only; no PowerShell/WMI subprocess',
 'source':'https://learn.microsoft.com/windows/win32/api/sysinfoapi/ns-sysinfoapi-memorystatusex'},
 replay={'five_point_fit_z_um':pending['fitted']['z_um'],'requires_one_fresh_independent_confirmation':True,
 'preserved_geometric_probes':7,'accepted_edge_observations':3,'chip_outlines':0,'raster_tiles':0,'hardware_access':False,
 'frame':c['prior_coordinate_frame_id'],'deadline_at':c['deadline_at']})
report.pop('capture_ram_floor_bytes',None)
(root/'repair-report.json').write_text(json.dumps(report,indent=2));(old/'supervision/repair-status.json').write_text(json.dumps(report,indent=2))
print(json.dumps(report))
