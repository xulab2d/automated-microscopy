"""Explicit user-requested start retaining the October 15 hBN chip evidence."""
from pathlib import Path
import json,sys,time

here=Path(__file__).resolve().parent
workspace=here.parents[1]
sys.path.insert(0,str(workspace/'research/software-next-20261009'))
from flakefinder.rectangle_handoff import build_contract
from flakefinder.scan_service import ScanService

project=Path(json.loads((workspace/'configs/storage.json').read_text(encoding='utf-8-sig'))['project_root'])
suffix='-retry' if '--retry' in sys.argv else ''
receipt=here/f'storage-continuation{suffix}-start.json'
if receipt.exists():raise RuntimeError('Start already recorded; inspect the current worker.')
service=ScanService(project)
plan_record=here/f'storage-continuation{suffix}-plan.json'
if plan_record.exists():
    identifier=json.loads(plan_record.read_text())['run_id']
else:
    print('Verifying saved hBN reference and chip evidence.',flush=True)
    contract=build_contract(project,'scan-20261015-215627-bb0a2529')
    if contract['settings']['material']!='hBN':raise RuntimeError('Unexpected material')
    planned=service.plan(contract['settings'],rectangle_handoff=contract)
    identifier=planned['run']['id']
    with plan_record.open('x') as stream:
        json.dump({'run_id':identifier,'prior_run_id':contract['prior_run_id'],
            'deadline_at':contract['deadline_at'],'preflight':planned['preflight']},stream,indent=2)
    if not planned['preflight']['ok']:raise RuntimeError(str(planned['preflight']['blockers']))
print('Starting saved-chip continuation '+identifier,flush=True)
result=service.start(identifier,release_hold=True)
summary={k:result['run'].get(k) for k in ('id','state','phase','pid','process_alive')}
summary['operator_instruction']='ok make the run start'
with receipt.open('x') as stream:json.dump(summary,stream,indent=2)
print(json.dumps(summary),flush=True)
