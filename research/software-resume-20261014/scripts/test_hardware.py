"""Explicit bounded on-instrument tests. All results are stored locally."""
from pathlib import Path
import sys, argparse, json, time
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from dataclasses import asdict
import numpy as np
from PIL import Image
from flakefinder.zen import ZenMicroscope
from flakefinder.model import Limits, Position, atomic_json
from flakefinder.autofocus import autofocus

p=argparse.ArgumentParser()
p.add_argument('action',choices=['motion','focus','frame'])
p.add_argument('--range',type=float,default=25)
p.add_argument('--step',type=float,default=5)
p.add_argument('--job-template',default='FlakeFinder Camera')
a=p.parse_args()
out=Path('data')/(time.strftime('%Y%m%d-%H%M%S')+'-'+a.action)
out.mkdir(parents=True)
s=ZenMicroscope(timeout_s=25,discard_frames=2,job_template=a.job_template)
origin=s.position()
atomic_json(out/'origin.json',asdict(origin))
print('Origin',origin,flush=True)
s.arm(Limits(origin.x-200,origin.x+200,origin.y-200,origin.y+200,origin.z-a.range-1,origin.z+a.range+1,100,25))
try:
    if a.action=='motion':
        path=[Position(origin.x+10,origin.y,origin.z),origin,Position(origin.x,origin.y+10,origin.z),origin,Position(origin.x,origin.y,origin.z+2),origin]
        records=[]
        for t in path:
            r=s.move(t)
            records.append({'target':asdict(t),'actual':asdict(r)})
            print(records[-1],flush=True)
        atomic_json(out/'motion.json',records)
    elif a.action=='focus':
        def callback(f,m):
            print(json.dumps(m),flush=True)
            # Raw 16-bit RGB is preserved as .npy; previews share a fixed nominal 12-bit scale.
            np.save(out/(f"z_{m['z_um']:.3f}.npy"),f.rgb)
            preview=np.uint8(np.clip(f.rgb.astype(float)/4095*255,0,255))
            Image.fromarray(preview).save(out/(f"z_{m['z_um']:.3f}.png"))
        r=autofocus(s,a.range,a.step,max(0.5,a.step/5),out,callback)
        print(json.dumps(asdict(r)),flush=True)
    else:
        f=s.capture()
        np.save(out/'frame.npy',f.rgb)
        atomic_json(out/'frame.json',{'position':asdict(f.position),'pixel_size_um':f.pixel_size_um,'sequence':f.sequence,'metadata':f.metadata})
        Image.fromarray(np.uint8(np.clip(f.rgb.astype(float)/4095*255,0,255))).save(out/'frame.png')
        print('Frame',f.rgb.shape,f.rgb.dtype,f.pixel_size_um,flush=True)
except BaseException:
    print('Stop result',s.stop(),flush=True)
    raise
finally:
    s.close()
    print('Saved',out,flush=True)
