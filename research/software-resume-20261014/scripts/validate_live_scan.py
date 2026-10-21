"""Run a repeatable raster test using the selected configuration and save evidence."""
from pathlib import Path
from dataclasses import asdict
import argparse, json, sys
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from flakefinder.config import load_config, connect, arm, transform_from_config
from flakefinder.autofocus import autofocus_range
from flakefinder.scan import ScanPlan, run_scan
from flakefinder.imaging import save_frame, load_frame
from flakefinder.calibration import register_translation
from flakefinder.workflow import move_z_bounded
from flakefinder.model import atomic_json
import numpy as np

def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--config', required=True); p.add_argument('--output', required=True)
    p.add_argument('--resume',action='store_true')
    args=p.parse_args(); cfg=load_config(args.config); out=Path(args.output)
    out.mkdir(parents=True, exist_ok=args.resume)
    if args.resume and json.loads((out/'configuration.json').read_text())!=cfg:raise ValueError('Resume configuration changed')
    atomic_json(out/'configuration.json', cfg)
    scope=connect(cfg)
    try:
        arm(scope,cfg); t=transform_from_config(cfg)
        print('Initial position',asdict(scope.position()),flush=True)
        if not args.resume:
            result=autofocus_range(scope,**cfg['initial_focus'],output=out/'initial-focus')
            print('Initial autofocus',result.success,result.z_um,result.reason,flush=True)
            if not result.success: raise RuntimeError(result.reason)
        manifest=run_scan(scope,ScanPlan(**cfg['survey']),t,out/'scan',context=cfg.get('context'),
            callback=lambda event,f,info: print(event,info,flush=True) if event!='focus' else None,resume=args.resume)
        first=load_frame(out/'scan/tiles/tile_000000.tiff')
        scope.travel(first.position.x,first.position.y);move_z_bounded(scope,first.position.z)
        returned=scope.capture();save_frame(out/'repeat','first-tile-revisited',returned)
        shift,response=register_translation(first.rgb,returned.rgb)
        error=float(np.linalg.norm(t.matrix@shift))
        audit={'state':'complete','tiles':manifest['tile_count'],
               'first_tile_return_error_um':error,'registration_response':response,
               'passed_repeatability':error<2,'final_position':asdict(scope.position())}
        atomic_json(out/'live-test.json',audit);print(json.dumps(audit),flush=True)
    except BaseException:
        scope.stop();raise
    finally:scope.close()

if __name__=='__main__':main()
