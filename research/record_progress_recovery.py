from pathlib import Path
import json,hashlib
from datetime import datetime,timezone
project=Path(r'G:\Shared drives\Xu Lab - Microscopy and Instruments\Automated Microscopy')
src=Path(__file__).parent/'software-next-20261009'
root=project/'research/session-20261011-progress-continuation'
report=json.loads((root/'repair-report.json').read_text())
run=project/'data'/json.loads((project/'scan-control/current.json').read_text())['id']
assert run.name=='scan-20261011-133357-4a1271ad'
snapshot=json.loads((run/'supervision/low-memory-snapshot.json').read_text());state=snapshot['status']
for name,sha in report['source_after_sha256'].items():assert hashlib.sha256((src/name).read_bytes()).hexdigest()==sha,name
live={'at':datetime.now(timezone.utc).isoformat(),'run_id':run.name,'worker_pid':state['pid'],'phase':state['phase'],
 'state':'recovery_running' if state['state']=='running' else state['state'],
 'progress_reused':snapshot.get('progress_reused'),'initial_focus_reused':snapshot.get('focus_reused'),
 'discoveries':snapshot['discoveries'],'available_bytes':snapshot['available_bytes_now'],
 'completed_chip_outlines':len((snapshot.get('batch') or {}).get('chip_states') or {}),
 'raster_tiles':sum(m['tiles'] for m in snapshot['manifests']),'interval_minutes':1,
 'remaining_issue':'Candidate001 geometric/focus disagreement remains unresolved; evaluate new candidate002 outcome, do not count edge observations as chip coverage.'}
report['live_verification']=live;report['state']=live['state']
(root/'repair-report.json').write_text(json.dumps(report,indent=2))
(run/'supervision/repair-status.json').write_text(json.dumps(live,indent=2))
(run/'supervision/cadence.json').write_text(json.dumps({'minutes':1,'previous_minutes':1,'reason':'Recovery requires verified geometry/focus reuse and sustained capture headroom.','at':live['at']},indent=2))
print(json.dumps(live))
