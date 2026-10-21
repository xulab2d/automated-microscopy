"""Sparse layout reconnaissance. Unsampled gaps are never treated as chip edges."""
from pathlib import Path
import sys,json,time
from dataclasses import asdict
import numpy as np
from PIL import Image,ImageDraw
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from flakefinder.config import connect,arm,load_config
from flakefinder.imaging import save_frame
from flakefinder.model import atomic_json

cfg=load_config('configs/zen-20x-quantitative.json')
cfg['limits'].update(x_min=45500,x_max=54500,y_min=25500,y_max=34500,max_xy_step_um=500)
out=Path('data/chip-layout-probes-01');out.mkdir(parents=True,exist_ok=True)
xs=np.linspace(46000,54000,5);ys=np.linspace(26000,34000,5)
state={'schema':1,'kind':'sparse_layout_probes','status':'running','bounds_um':[46000,54000,26000,34000],
       'sample_pitch_um':2000,'unsampled_gaps':'unknown; not chip boundaries','configuration':cfg,'records':[]}
atomic_json(out/'probes.json',state)
s=connect(cfg)
try:
 arm(s,cfg);state['origin']=asdict(s.position())
 for row,y in enumerate(ys):
  for x in xs if row%2==0 else xs[::-1]:
   s.travel(float(x),float(y));f=s.capture();name=f'probe_{len(state["records"]):03d}'
   save_frame(out/'frames',name,f)
   h,w=f.rgb.shape[:2];pixels=f.rgb[h//4:3*h//4,w//4:3*w//4].reshape(-1,3)
   med=np.median(pixels,axis=0)
   record={'name':name,'position':asdict(f.position),'median_rgb':med.tolist(),
           'rgb_ratios_to_green':(med/max(med[1],1)).tolist(),'q10_rgb':np.percentile(pixels,10,axis=0).tolist(),
           'q90_rgb':np.percentile(pixels,90,axis=0).tolist()}
   state['records'].append(record);atomic_json(out/'probes.json',state)
   print('PROBE',len(state['records']),record,flush=True)
 state['status']='complete'
except BaseException as exc:state.update(status='failed',error=str(exc));s.stop();raise
finally:
 atomic_json(out/'probes.json',state);s.close()
 # Contact sheet preserves gaps as unmeasured background.
 canvas=Image.new('RGB',(1250,1100),'#171d24');draw=ImageDraw.Draw(canvas)
 for r in state['records']:
  x=int(round((r['position']['x']-46000)/2000));y=int(round((r['position']['y']-26000)/2000))
  im=Image.open(out/'frames'/(r['name']+'.jpg'));im.thumbnail((235,196));canvas.paste(im,(x*250,y*220))
  draw.text((x*250+3,y*220+196),f"{r['name']} X{r['position']['x']:.0f} Y{r['position']['y']:.0f}",fill='white')
 canvas.save(out/'contact-sheet.jpg')
