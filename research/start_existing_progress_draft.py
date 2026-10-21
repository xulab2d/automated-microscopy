from pathlib import Path
import sys,json
from datetime import datetime,timezone
src=Path(__file__).parent/'software-next-20261009';sys.path.insert(0,str(src))
from flakefinder.scan_service import ScanService
project=Path(r'G:\Shared drives\Xu Lab - Microscopy and Instruments\Automated Microscopy')
service=ScanService(project);run_id='scan-20261011-133357-4a1271ad';folder=service.folder(run_id)
request=json.loads((folder/'scan-request.json').read_text());parent=service.folder(request['continuation']['prior_run_id'])
previous=json.loads((parent/'acquisition-authorization.json').read_text())
planned=json.loads((folder/'preflight.json').read_text())
holds=[{k:h[k] for k in ('path','sha256')} for h in planned['hold']['records']]
assert holds==previous['holds'],'Do not release new or changed operator holds'
receipt={'at':datetime.now(timezone.utc).isoformat(),'parent':parent.name,'holds':holds,
 'authorization':'Identical prior-session holds were already authorized by the October11 human confirmation that chips were returned, fresh scan and autonomous recovery. No new/changed hold; hold files remain unchanged.'}
(folder/'supervision').mkdir(exist_ok=True)
(folder/'supervision/unchanged-hold-authorization.json').write_text(json.dumps(receipt,indent=2))
result=service.start(run_id,release_hold=True)
print(json.dumps({'run_id':run_id,'state':result['run']['state'],'pid':result['run'].get('pid')}))
