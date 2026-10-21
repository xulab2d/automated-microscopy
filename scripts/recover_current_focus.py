"""Run a recorded focus search at a freshly measured specimen position."""
from pathlib import Path
from dataclasses import asdict
import argparse,json,sys
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from flakefinder.config import load_config,connect,arm
from flakefinder.autofocus import autofocus_range
from flakefinder.imaging import save_frame
from flakefinder.model import atomic_json,Cancelled

def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--config',required=True);p.add_argument('--output',type=Path,required=True)
    p.add_argument('--xy',nargs=2,type=float,required=True)
    p.add_argument('--range',dest='z_range',nargs=2,type=float,required=True)
    p.add_argument('--coarse-step',type=float,default=10)
    p.add_argument('--fine-step',type=float,default=1)
    a=p.parse_args()
    if a.output.exists() and any(a.output.iterdir()):raise ValueError('Choose an empty focus folder.')
    a.output.mkdir(parents=True,exist_ok=True)
    cfg=load_config(a.config);scope=connect(cfg);best=-1.;count=0
    try:
        arm(scope,cfg)
        origin=scope.position()
        atomic_json(a.output/'origin.json',asdict(origin))
        old_check=scope.check_cancel
        def check():
            old_check()
            if (a.output/'STOP').exists() or (a.output.parent.parent/'STOP').exists():raise Cancelled('Focus STOP requested.')
        scope.check_cancel=check
        scope.travel(*a.xy)
        def record(frame,sample):
            nonlocal best,count
            count+=1
            if sample['score']>best:
                best=sample['score'];save_frame(a.output,'best-observed',frame)
            print(json.dumps(sample),flush=True)
        result=autofocus_range(scope,*a.z_range,a.coarse_step,a.fine_step,a.output,record)
        if result.success:save_frame(a.output,'focused',scope.capture())
        print(json.dumps(asdict(result)),flush=True)
    finally:scope.close()

if __name__=='__main__':main()
