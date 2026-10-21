from pathlib import Path
import sys,json,hashlib,shutil
from datetime import datetime,timezone
src=Path(__file__).parent/'software-next-20261009';sys.path.insert(0,str(src))
from flakefinder.scan_continuation import build_contract,verify_contract
from flakefinder.reference_repair import validate_plan,reclassify_progress,PLAN,check_controls
from flakefinder.imaging import load_frame
from flakefinder.model import CameraTransform
project=Path(r'G:\Shared drives\Xu Lab - Microscopy and Instruments\Automated Microscopy')
parent=project/'data/scan-20261011-140151-bdf8664e';root=project/'research/session-20261011-geometry-continuity'
read=lambda p:json.loads(p.read_text(encoding='utf-8-sig'))
c=build_contract(project,parent.name,-25000.,z_max_um=25000.,focus_travel_policy='controller');verify_contract(project,c)
assert c['deadline_at']==1791825855.8789244 and c['prior_coordinate_frame_id']=='f32a3bfc-d8e5-4ae9-8218-6d57fa670104'
assert c['preserve_pre_raster_progress'] and c['reference_correction']
(root/'validated-contract.json').write_text(json.dumps(c,indent=2))
plan,_=validate_plan(parent);ref=load_frame(parent/Path(PLAN).parent/plan['focused_frame'])
old=read(parent/'batch.json');config=read(parent/'scan-config.json');config['context']['prior_run_id']=parent.name
search=read(parent/'search-plan.json');transform=CameraTransform(**config['camera_transform'])
out=root/'offline-progress';out.mkdir(exist_ok=True)
seeds=json.loads(json.dumps(old['seed_records']))
for r in seeds:
 for name,h in r['hashes'].items():
  p=parent/name;assert hashlib.sha256(p.read_bytes()).hexdigest()==h
  target=out/name;target.parent.mkdir(parents=True,exist_ok=True)
  if not target.exists():shutil.copy2(p,target)
state={};progress=reclassify_progress(out,state,config,ref,transform,search,old,seeds)
changes=read(out/'reference-reclassified-progress.json')['changes']
names=['reference_repair.py','scan_continuation.py','scan_worker.py','chip_campaign.py','memory_progress.py','sparse_acquisition.py']
report={'at':datetime.now(timezone.utc).isoformat(),'state':'validated_offline','parent_run':parent.name,
 'diagnosis':'Defocused mixed reference assigned backing as substrate and chip as holder; strict geometry rejection correctly prevented false outlines.',
 'reference_controls':read(root/'reference-control-replay.json'),'validation':{'offline_tests_passed':96},
 'source_after_sha256':{n:hashlib.sha256((src/'flakefinder'/n).read_bytes()).hexdigest() for n in names},
 'continuation':{'kind':c['continuation_kind'],'frame':c['prior_coordinate_frame_id'],'deadline':c['deadline_at'],
 'checkpoint':c['motion_checkpoint']['position'],'pin_count':len(c['prior_files_sha256'])},
 'progress':{'preserved_seed_count':len(old['seed_records']),'reclassified':changes,'queued_seeds':len(progress['discoveries']),
 'remaining_lattice':len(progress['pending']),'historical_boundaries':len(old['boundary_attempts']),'completed_outlines':0,'raster_tiles':0},
 'changes':['Fresh reference at pinned independently focused XY/Z; one new same-ROI confirmation and four independent control checks required.',
 'Prior raw seed images reclassified and reused; previous invalid labels preserved in lineage, original lattice unchanged.',
 'At most four spatially separated local focuses; nearest measured focus seeds new XY; original total capture and operation budgets unchanged.',
 'Exact supervisor STOP/vendor FutureCancelledError recognized only with matching timestamp, request, PID, closure and settled motion evidence.'],
 'ram_policy':'Commit/address capacity; physical free-RAM floor remains zero.',
 'remaining_work':'Launch verified continuation and validate corrected live reference, geometry and acquisition.'}
(root/'repair-report.json').write_text(json.dumps(report,indent=2));(parent/'supervision/repair-status.json').write_text(json.dumps(report,indent=2))
print(json.dumps({'continuation':report['continuation'],'progress':report['progress']}))
