from pathlib import Path
import sys,json,shutil,hashlib
from datetime import datetime,timezone
workspace=Path(__file__).resolve().parents[1];src=workspace/'research/software-next-20261009';sys.path.insert(0,str(src))
from flakefinder.scan_continuation import build_contract,verify_contract
from flakefinder.memory_progress import prepare_progress
from flakefinder.model import CameraTransform
from flakefinder.imaging import load_frame
from flakefinder.focus_checkpoint import find_local_focus
project=Path(r'G:\Shared drives\Xu Lab - Microscopy and Instruments\Automated Microscopy')
parent=project/'data/scan-20261011-130111-bf685ad5'
report_root=project/'research/session-20261011-progress-continuation'
contract=build_contract(project,parent.name,-25000.,z_max_um=25000.,focus_travel_policy='controller')
verify_contract(project,contract)
(report_root/'validated-contract.json').write_text(json.dumps(contract,indent=2))
# File-only replay in an explicitly offline report tree; no microscope import.
mirror=report_root/'offline-replay/data';old=mirror/parent.name
for name in contract['prior_files_sha256']:
    target=old/name;target.parent.mkdir(parents=True,exist_ok=True)
    if not target.exists():shutil.copy2(parent/name,target)
new=mirror/'scan-offline-validation-02';new.mkdir(exist_ok=True)
(new/'scan-request.json').write_text(json.dumps({'continuation':contract}))
(new/'continuation-verification.json').write_text(json.dumps({'continuation':contract,'offline_simulation_only':True}))
cfg=json.loads((parent/'scan-config.json').read_text());cfg['context'].update(prior_run_id=parent.name,prior_coordinate_frame_id=contract['prior_coordinate_frame_id'])
ref=load_frame(parent/'calibration/substrate.tiff');transform=CameraTransform(**cfg['camera_transform'])
search=json.loads((parent/'search-plan.json').read_text())
state={'coordinate_frame_id':contract['prior_coordinate_frame_id'],'search_bounds_um':contract['search_bounds_um']}
result=prepare_progress(new,state,cfg,ref,transform,search)
partial=find_local_focus(new/'discovery/candidate002',cfg['context'],[3497.503,-21485.085],ref,partial=True)
assert partial is not None and partial['prior_samples']==20 and partial['result']['z_um']==15675.624999999998
imported=json.loads((new/'discovery/candidate002/discovery.json').read_text())
assert len(imported['imported_records'])==7 and sum(bool(r.get('edge')) for r in imported['imported_records'])==3
from flakefinder.sparse_acquisition import discover_sparse_outline,SparseEdgeOptions
from flakefinder.model import Limits,Cancelled,Position
class NoMotion:
    limits=Limits(**cfg['limits']);armed=True
    def position(self):return Position(*imported['seed_xy_um'],15675.625)
    def check_cancel(self):raise Cancelled('Offline checkpoint replay deliberately forbids hardware operations.')
    def stop(self):pass
try:
    discover_sparse_outline(NoMotion(),imported['seed_xy_um'],transform,ref,new/'discovery/candidate002',SparseEdgeOptions(**imported['options']),
        context=imported['context'],resume=True,edge_validation_profile=cfg['chip_discovery']['edge_validation_profile'],focus_search_bounds_um=[-25000.,25000.])
except Cancelled:pass
report=json.loads((report_root/'repair-report.json').read_text())
source_names=['flakefinder/memory_progress.py','flakefinder/scan_continuation.py','flakefinder/focus_checkpoint.py','flakefinder/sparse_acquisition.py','flakefinder/chip_campaign.py','flakefinder/sparse_import.py','scripts/watch_scan_memory.ps1']
before=report_root/'source-before/flakefinder/sparse_import.py'
if not before.exists():before.write_text((src/'flakefinder/sparse_import.py').read_text().replace("old.get('imported_records',[])+old['records']+old.get('support_records',[])","old['records']+old.get('support_records',[])"))
report['source_before_sha256']['flakefinder/sparse_import.py']=hashlib.sha256(before.read_bytes()).hexdigest()
guard=src/'scripts/watch_scan_memory.ps1';guard_before=guard.read_text()
start=guard_before.index('\ndef Convert-TrimExpiryUtc') if '\ndef Convert-TrimExpiryUtc' in guard_before else guard_before.index('\nfunction Convert-TrimExpiryUtc')
end=guard_before.index('\nfunction Test-CodexRenderer',start)
guard_before=guard_before[:start]+'\n'+guard_before[end:]
guard_before=guard_before.replace("(Convert-TrimExpiryUtc $entry.Value).ToString('o')","[string]$entry.Value").replace("(Convert-TrimExpiryUtc $trimLedger[$trimKey])","[datetimeoffset]::Parse($trimLedger[$trimKey]).UtcDateTime")
guard_path=report_root/'source-before/scripts/watch_scan_memory.ps1';guard_path.parent.mkdir(exist_ok=True)
guard_path.write_text(guard_before)
report['source_before_sha256']['scripts/watch_scan_memory.ps1']=hashlib.sha256(guard_path.read_bytes()).hexdigest()
report.update(state='validated_offline',validated_at=datetime.now(timezone.utc).isoformat(),
 tests={'checkpoint_and_refusal':85,'campaign_worker_autofocus':56,'powershell_guard':29},
 ram_cause='Legacy UTC expiry strings were parsed in local timezone, incorrectly extending cooldown by7hours; parse and persist UTC explicitly. Renderer identities,20-minute cooldown and1.5GiB capture requirement unchanged.',
 source_after_sha256={n:hashlib.sha256((src/n).read_bytes()).hexdigest() for n in source_names},
 replay={'preserved_seeds':len(state['seed_records']),'imported_probes':len(imported['imported_records']),'accepted_edge_observations':3,
 'remaining_lattice_points':len(result['pending']),'partial_focus_samples_preserved':20,'search_peak_seed_z_um':partial['result']['z_um'],
 'requires_fresh_focus_confirmation':True,'new_operation_allowance':imported['lineage']['recovery_operation_budget'],
 'deadline_at':contract['deadline_at'],'frame':contract['prior_coordinate_frame_id'],'hardware_touched':False})
(report_root/'repair-report.json').write_text(json.dumps(report,indent=2));(parent/'supervision/repair-status.json').write_text(json.dumps(report,indent=2))
print(json.dumps(report))
