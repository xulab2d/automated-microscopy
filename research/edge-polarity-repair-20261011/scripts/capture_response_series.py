"""Acquire stationary exposure pairs with fixed illumination and channel balance."""
from pathlib import Path
from dataclasses import asdict
import argparse,json,sys,time
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from flakefinder.config import connect,load_config
from flakefinder.imaging import save_frame
from flakefinder.model import atomic_json,AcquisitionError


def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--config',required=True);p.add_argument('--output',type=Path,required=True)
    p.add_argument('--exposures-ms',nargs='+',type=float,default=[100,200,400])
    a=p.parse_args()
    if len(set(a.exposures_ms))<3 or any(not 0<t<=2000 for t in a.exposures_ms):
        raise ValueError('Use three or more distinct positive exposures up to 2000 ms.')
    if a.output.exists() and any(a.output.iterdir()):raise ValueError('Choose an empty series folder.')
    a.output.mkdir(parents=True,exist_ok=True)
    cfg=load_config(a.config);scope=connect(cfg);started=time.monotonic()
    state={'state':'running','frames':[],'white_level':cfg['zen'].get('camera_white_level',4095),
           'dark_reference':'not acquired; this is an offset-unverified exposure response check'}
    try:
        origin=scope.position();state['position']=asdict(origin)
        for exposure in sorted(a.exposures_ms):
            scope.camera_resources['ff_exposure_ms']=exposure
            for repeat in range(2):
                frame=scope.capture()
                if max(abs(getattr(frame.position,k)-getattr(origin,k)) for k in ('x','y','z'))>.15:
                    raise AcquisitionError('Stage moved during the stationary exposure series.')
                name=f'exposure_{exposure:g}_{repeat}'
                save_frame(a.output,name,frame)
                state['frames'].append({'path':name+'.tiff','exposure_ms':exposure})
                state['elapsed_s']=time.monotonic()-started
                atomic_json(a.output/'series.json',state)
                print(json.dumps({'frame':name,'elapsed_s':state['elapsed_s']}),flush=True)
        state['state']='complete'
    except BaseException as exc:
        state.update(state='failed',error=str(exc));raise
    finally:
        atomic_json(a.output/'series.json',state);scope.close()


if __name__=='__main__':main()
