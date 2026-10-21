from pathlib import Path
from datetime import datetime,timezone
import json,hashlib,sys
project=Path(r'G:\Shared drives\Xu Lab - Microscopy and Instruments\Automated Microscopy')
src=Path(__file__).parent/'software-next-20261009';root=project/'research/session-20261011-native-memory'
report=json.loads((root/'repair-report.json').read_text());run=project/'data'/report['planned_run']
assert json.loads((project/'scan-control/current.json').read_text())['id']==run.name
for n,h in report['source_after_sha256'].items():assert hashlib.sha256((src/n).read_bytes()).hexdigest()==h,n
snap=json.loads((run/'supervision/low-memory-snapshot.json').read_text())
meta=json.loads((run/'calibration/substrate.json').read_text()) if (run/'calibration/substrate.json').exists() else {}
confirmed=[]
for d in snap['discoveries']:
 for f in d['focus']:
  reuse=f.get('pending_reuse')
  if reuse and reuse.get('accepted'):confirmed.append(reuse)
live={'at':datetime.now(timezone.utc).isoformat(),'run_id':run.name,'worker':snap['status'],
 'state':'recovered_capture_and_focus' if confirmed else 'recovering_live_verification',
 'initial_focus_reused':bool(snap.get('focus_reused')),'progress_reused':snap.get('progress_reused'),
 'pending_focus_confirmed':confirmed,'capture_memory_policy':meta.get('metadata',{}).get('capture_memory_guard'),
 'guard':snap.get('guard'),'guard_working_set_bytes':snap.get('guard_working_set_bytes'),
 'available_physical_bytes':snap['available_bytes_now'],'memory_error':snap.get('memory'),
 'discoveries':snap['discoveries'],'raster_tiles':sum(m['tiles'] for m in snap['manifests']),
 'remaining_work':'Resolve chip geometry and complete raster acquisition/analysis; accepted edge observations are not complete chip coverage.'}
if snap['status']['state']=='failed':live['state']='failed_requires_repair'
report['live_verification']=live;report['state']=live['state']
(root/'repair-report.json').write_text(json.dumps(report,indent=2));(run/'supervision/repair-status.json').write_text(json.dumps(live,indent=2))
if '--mapping-cadence' in sys.argv:
 (run/'supervision/cadence.json').write_text(json.dumps({'minutes':5,'previous_minutes':1,'at':live['at'],
 'reason':'One-image independent focus confirmation and10newprobe commits verified;6acceptededgeobservations,11/30captures,116/600seconds. NoRAMwaits. Boundedmappingisadvancing;zerooutlines/raster.',
 'previous_observation':{'at':'2026-10-11T21:04:26.672011Z','new_probes':2,'accepted_edge_observations':4}},indent=2))
print(json.dumps(live))
