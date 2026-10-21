"""Create a same-placement continuation referencing the complete original chip."""
from pathlib import Path
import json,sys,time
HERE=Path(__file__).resolve().parent;ROOT=HERE.parents[1]
sys.path.insert(0,str(ROOT/'research/software-next-20261009'))
from flakefinder.rectangle_handoff import build_contract
from flakefinder.scan_service import ScanService
if (HERE/'handoff-plan.json').exists():raise RuntimeError('Plan already exists; inspect before retry')
project=Path(json.loads((ROOT/'configs/storage.json').read_text())['project_root'])
preview=HERE/'contract-preview.json'
contract=json.loads(preview.read_text()) if preview.exists() else build_contract(project,'scan-20261013-221727-131d9ced',reference_completed=True)
if 'chip001' not in contract['completed_chip_sources']:raise RuntimeError('Completed chip001 was not retained')
if any(name.startswith('chip001/') for name in contract['prior_files_sha256']):raise RuntimeError('Completed chip would be copied')
print('Completed chip retained by reference; preparing continuation',flush=True)
result=ScanService(project).plan(contract['settings'],rectangle_handoff=contract)
proof={'run_id':result['run']['id'],'donor':contract['prior_run_id'],'deadline_at':contract['deadline_at'],
       'planned_at':time.time(),'pinned_files_to_consider':len(contract['prior_files_sha256']),
       'completed_chip_sources':contract['completed_chip_sources'],
       'preflight_ok':result['preflight']['ok'],'blockers':result['preflight']['blockers']}
with (HERE/'handoff-plan.json').open('x',encoding='utf-8') as f:json.dump(proof,f,indent=2)
print(json.dumps({k:v for k,v in proof.items() if k!='completed_chip_sources'}),flush=True)
