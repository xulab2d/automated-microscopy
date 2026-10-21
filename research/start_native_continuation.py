from pathlib import Path
import sys,json,hashlib
from datetime import datetime,timezone
src=Path(__file__).parent/'software-next-20261009';sys.path.insert(0,str(src))
from flakefinder.scan_service import ScanService,process_alive
project=Path(r'G:\Shared drives\Xu Lab - Microscopy and Instruments\Automated Microscopy')
root=project/'research/session-20261011-native-memory';report=json.loads((root/'repair-report.json').read_text())
assert report['state']=='validated_offline'
for n,h in report['source_after_sha256'].items():assert hashlib.sha256((src/n).read_bytes()).hexdigest()==h,n
assert not any(process_alive(p) for p in (29716,25204,17636,19692))
service=ScanService(project);run_id=report['planned_run'];folder=service.folder(run_id)
assert not (folder/'worker-start.json').exists()
request=json.loads((folder/'scan-request.json').read_text());parent=service.folder(request['continuation']['prior_run_id'])
previous=json.loads((parent/'acquisition-authorization.json').read_text())
planned=json.loads((folder/'preflight.json').read_text())
holds=[{k:h[k] for k in ('path','sha256')} for h in planned['hold']['records']]
assert holds==previous['holds'],'Do not release new or changed operator holds'
current=[{k:h[k] for k in ('path','sha256')} for h in service.holds()['records']]
assert current==holds
receipt={'at':datetime.now(timezone.utc).isoformat(),'parent':parent.name,'holds':holds,
 'authorization':'Identical October9 holds already superseded by October11 human placement confirmation and autonomous recovery. No new/changed holds.',
 'memory_policy_authorization':'Human explicitly requested removal of arbitrary RAM reservations; operation capacity policy replaces free-physical-RAM gates.',
 'zen_ready_observation':'Computer Use observed API mode running, hardware initialization CompletedSuccessfully, application ready, API mode control allowed True at21:00-21:01UTC; no mode change.'}
(folder/'supervision').mkdir(exist_ok=True)
(folder/'supervision/unchanged-hold-authorization.json').write_text(json.dumps(receipt,indent=2))
(root/'api-ready-before-continuation.json').write_text(json.dumps(receipt,indent=2))
result=service.start(run_id,release_hold=True)
report.update(state='recovery_starting',run_id=run_id,launch=result['run'])
(root/'repair-report.json').write_text(json.dumps(report,indent=2));(folder/'supervision/repair-status.json').write_text(json.dumps(report,indent=2))
print(json.dumps({'run_id':run_id,'state':result['run']['state'],'pid':result['run'].get('pid')}))
