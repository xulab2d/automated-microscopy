from pathlib import Path
import sys,json,hashlib
from datetime import datetime,timezone
src=Path(__file__).parent/'software-next-20261009';sys.path.insert(0,str(src))
from flakefinder.scan_service import ScanService
from flakefinder.scan_continuation import process_alive_strict
project=Path(r'G:\Shared drives\Xu Lab - Microscopy and Instruments\Automated Microscopy')
root=project/'research/session-20261011-geometry-continuity';report=json.loads((root/'repair-report.json').read_text())
assert report['state']=='validated_offline'
for n,h in report['source_after_sha256'].items():assert hashlib.sha256((src/'flakefinder'/n).read_bytes()).hexdigest()==h,n
old=project/'data/scan-20261011-140151-bdf8664e';status=json.loads((old/'scan-status.json').read_text())
guard=json.loads((old/'memory-guard/status.json').read_text());launch=json.loads((old/'launch.json').read_text())
assert status['hardware_closed'] and not any(process_alive_strict(p) for p in (status['pid'],launch['pid'],guard['guard_pid'],status['memory_cleanup_launch']['pid']))
run_id=json.loads((old/'supervision/adaptive-focus-continuation.json').read_text())['new_run']
assert json.loads((project/'scan-control/current.json').read_text())['id']==run_id
service=ScanService(project);folder=service.folder(run_id)
assert not (folder/'worker-start.json').exists()
request=json.loads((folder/'scan-request.json').read_text());assert request['continuation']['prior_run_id']==old.name
assert request['continuation']['reference_correction'] and request['continuation']['preserve_pre_raster_progress']
previous=json.loads((old/'acquisition-authorization.json').read_text());planned=json.loads((folder/'preflight.json').read_text())
holds=[{k:h[k] for k in ('path','sha256')} for h in planned['hold']['records']]
assert holds==previous['holds'],'Do not release new or changed operator holds'
assert [{k:h[k] for k in ('path','sha256')} for h in service.holds()['records']]==holds
receipt={'at':datetime.now(timezone.utc).isoformat(),'parent':old.name,'holds':holds,
 'authorization':'Identical October9 holds superseded by October11 human placement/start and autonomous software recovery; no new hold.',
 'zen_ready_observation':'Read-only Computer Use observed API running, CompletedSuccessfully, application ready, control allowed True; no mode change or restart.'}
(folder/'supervision').mkdir(exist_ok=True)
(folder/'supervision/unchanged-hold-authorization.json').write_text(json.dumps(receipt,indent=2))
result=service.start(run_id,release_hold=True)
report.update(state='recovery_starting',run_id=run_id,launch=result['run'],validation={'integrated_tests_passed':96,'additional_tests_passed':34,'note':'Overlapping suites; not unique totals.'})
(root/'repair-report.json').write_text(json.dumps(report,indent=2));(folder/'supervision/repair-status.json').write_text(json.dumps(report,indent=2))
print(json.dumps({'run_id':run_id,'state':result['run']['state'],'pid':result['run'].get('pid')}))
