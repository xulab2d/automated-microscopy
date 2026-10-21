from pathlib import Path
import json
from datetime import datetime,timezone
project=Path(r'G:\Shared drives\Xu Lab - Microscopy and Instruments\Automated Microscopy')
run=project/'data'/json.loads((project/'scan-control/current.json').read_text())['id']
assert run.name=='scan-20261011-140151-bdf8664e'
rows=[]
for path in sorted((run/'discovery/candidate002/frames').glob('probe_*.json')):
 d=json.loads(path.read_text());g=d.get('metadata',{}).get('capture_memory_guard')
 if g:rows.append({'path':path.name,'working_bytes':g['working_allowance_bytes'],'commit_available':g['commit_available_bytes'],
   'physical_available':g['physical_available_bytes'],'physical_floor':g['physical_free_floor_bytes'],'wait_seconds':g['memory_wait_seconds']})
assert rows and all(x['physical_floor']==0 and x['wait_seconds']==0 for x in rows)
(run/'supervision/live-capacity-verification.json').write_text(json.dumps({'at':datetime.now(timezone.utc).isoformat(),'captures':rows},indent=2))
print(json.dumps(rows))
