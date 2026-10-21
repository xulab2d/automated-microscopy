from pathlib import Path
import sys,json,hashlib,shutil
from datetime import datetime,timezone
root=Path(__file__).resolve().parents[1];source=root/'research/software-next-20261009'
sys.path.insert(0,str(source))
from flakefinder.scan_continuation import process_alive_strict
project=Path(r'G:\Shared drives\Xu Lab - Microscopy and Instruments\Automated Microscopy')
read=lambda p:json.loads(p.read_text(encoding='utf-8-sig'))
run=project/'data'/read(project/'scan-control/current.json')['id']
assert run.name=='scan-20261011-140151-bdf8664e'
status=read(run/'scan-status.json');assert status['hardware_closed']
pids={status['pid'],read(run/'launch.json')['pid'],read(run/'memory-guard/status.json').get('guard_pid'),status['memory_cleanup_launch']['pid']}
assert all(not process_alive_strict(p) for p in pids if p)
report=project/'research/session-20261011-geometry-continuity';report.mkdir(exist_ok=True)
before=report/'before';before.mkdir(exist_ok=True)
hashes={}
for p in (source/'flakefinder').glob('*.py'):
 hashes[p.name]=hashlib.sha256(p.read_bytes()).hexdigest()
 if not (before/p.name).exists():shutil.copy2(p,before/p.name)
(report/'source-before.json').write_text(json.dumps({'at':datetime.now(timezone.utc).isoformat(),'pids_verified_absent':sorted(p for p in pids if p),'status':status,'sha256':hashes},indent=2))
from PIL import Image,ImageDraw
import numpy as np
from flakefinder.imaging import load_frame
for candidate in ['candidate002','candidate003']:
 folder=run/'discovery'/candidate;d=read(folder/'discovery.json');records=d.get('imported_records',[])+d['records']
 rows=[];sheet=Image.new('RGB',(4*360,((len(records)+3)//4)*320),'white');draw=ImageDraw.Draw(sheet)
 for i,r in enumerate(records):
  f=load_frame(folder/r['image']);a=f.rgb.astype(np.float32);a=np.clip(a/(float(f.metadata.get('white_level',4095)))*255,0,255).astype(np.uint8)
  im=Image.fromarray(a);im.thumbnail((350,260));x=(i%4)*360;y=(i//4)*320;sheet.paste(im,(x,y))
  name=r['image'];xy=r['position_um'];edge=r.get('edge')
  draw.text((x,y+263),f'{name}\n{r["phase"]}  {r["classification"]}\nXY {xy[0]:.1f}, {xy[1]:.1f} Z{xy[2]:.1f}',fill='black')
  rows.append({'image':name,'phase':r['phase'],'position':xy,'classification':r['classification'],'fractions':r['fractions'],'edge_normal':edge['normal'] if edge else None,'edge_reason':r['edge_validation']['reason']})
 sheet.save(report/(candidate+'-contact.jpg'))
 (report/(candidate+'-records.json')).write_text(json.dumps({'seed':d['seed_xy_um'],'transform':d['transform'],'field':d['field_rectangle_um'],'records':rows},indent=2))
 print(json.dumps({'candidate':candidate,'seed':d['seed_xy_um'],'transform':d['transform'],'field':d['field_rectangle_um'],'rows':rows}))
