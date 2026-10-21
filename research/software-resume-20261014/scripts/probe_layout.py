"""Capture explicit sparse layout probes, preserving unknown space between them."""
from pathlib import Path
from dataclasses import asdict
import argparse,json,sys
import numpy as np
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from flakefinder.config import connect,arm,load_config
from flakefinder.imaging import save_frame
from flakefinder.model import atomic_json,Position

def main():
 p=argparse.ArgumentParser();p.add_argument('--config',required=True);p.add_argument('--points',required=True);p.add_argument('--output',required=True)
 a=p.parse_args();cfg=load_config(a.config);points=json.loads(Path(a.points).read_text());out=Path(a.output)
 if out.exists() and any(out.iterdir()):raise ValueError('Use a new probe output directory.')
 out.mkdir(parents=True,exist_ok=True)
 state={'schema':1,'kind':'sparse_layout_probes','status':'running','unsampled_gaps':'unknown; not chip boundaries','configuration':cfg,'requested_points_um':points,'records':[]}
 s=connect(cfg)
 try:
  arm(s,cfg);origin=s.position();state['origin']=asdict(origin)
  for x,y in points:s.limits.check(Position(x,y,origin.z))
  for x,y in points:
   s.travel(float(x),float(y));f=s.capture();name=f'probe_{len(state["records"]):03d}';save_frame(out/'frames',name,f)
   h,w=f.rgb.shape[:2];pixels=f.rgb[h//4:3*h//4,w//4:3*w//4].reshape(-1,3);med=np.median(pixels,axis=0)
   record={'name':name,'position':asdict(f.position),'median_rgb':med.tolist(),'rgb_ratios_to_green':(med/max(med[1],1)).tolist(),
           'q10_rgb':np.percentile(pixels,10,axis=0).tolist(),'q90_rgb':np.percentile(pixels,90,axis=0).tolist()}
   state['records'].append(record);atomic_json(out/'probes.json',state);print('PROBE',len(state['records']),record,flush=True)
  state['status']='complete'
 except BaseException as exc:state.update(status='failed',error=str(exc));s.stop();raise
 finally:atomic_json(out/'probes.json',state);s.close()

if __name__=='__main__':main()
