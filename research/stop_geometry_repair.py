"""File-only cooperative checkpoint for the repeated, saved geometry failure."""
from pathlib import Path
import json,time,hashlib
from datetime import datetime,timezone
project=Path(r'G:\Shared drives\Xu Lab - Microscopy and Instruments\Automated Microscopy')
read=lambda p:json.loads(p.read_text(encoding='utf-8-sig'))
run=project/'data'/read(project/'scan-control/current.json')['id']
assert run.name=='scan-20261011-140151-bdf8664e'
status=read(run/'scan-status.json')
assert status['pid']==28352 and status['state']=='running'
assert hashlib.sha256((run/'scan-request.json').read_bytes()).hexdigest()==status['request_sha256']
assert not (run/'STOP').exists()
outlines={c:read(run/'discovery'/c/'estimated-outline.json') for c in ('candidate002','candidate003')}
assert all(o['reason']=='registered_substrate_contradicts_halfplanes' for o in outlines.values())
assert not read(run/'batch.json').get('chip_states') and not list(run.glob('chip*/manifest.json'))
at=datetime.now(timezone.utc).isoformat()
record={'at':time.time(),'utc':at,'issued_by':'assistant_supervisor','hardware_fault':False,
 'clear_stop_automatically':False,'reason':'Supervisor cooperative stop: repeated geometric contradiction after two completed boundary searches; preserve all captures for offline diagnosis and source-bound continuation.',
 'run_id':run.name,'request_sha256':status['request_sha256']}
with (run/'STOP').open('x',encoding='utf-8') as f:json.dump(record,f,indent=2)
repair={'at':at,'state':'cooperative_closure_pending','reason':record['reason'],'ram_repair':'verified; no allocation waits',
 'completed_outlines':0,'raster_tiles':0,'failed_candidates':list(outlines),'source_edit_permitted':False}
(run/'supervision/repair-status.json').write_text(json.dumps(repair,indent=2),encoding='utf-8')
(run/'supervision/cadence.json').write_text(json.dumps({'at':at,'interval_minutes':1,'previous_interval_minutes':5,'reason':record['reason'],'previous_observation':'candidate002 and candidate003 each terminated unresolved; RAM is not blocking capture'},indent=2),encoding='utf-8')
print(json.dumps(record))
