"""Test local focus and camera direction within the configured commissioning envelope."""
import sys,time
from pathlib import Path
from dataclasses import asdict
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from flakefinder.config import load_config,connect,arm,save_transform
from flakefinder.autofocus import autofocus_range
from flakefinder.calibration import calibrate_camera
from flakefinder.imaging import save_frame
from flakefinder.model import atomic_json

config_path=Path('configs/zen-local.json');cfg=load_config(config_path)
out=Path('data/commission-'+time.strftime('%Y%m%d-%H%M%S'));out.mkdir(parents=True)
s=connect(cfg)
try:
    arm(s,cfg);atomic_json(out/'origin.json',asdict(s.position()))
    def sample(frame,info):
        name=f"{info['phase']}-{info['z_um']:.3f}"
        save_frame(out/'focus-frames',name,frame)
        print(info['phase'],round(info['z_um'],3),round(info['score'],7),flush=True)
    focus=autofocus_range(s,**cfg['initial_focus'],output=out/'focus',callback=sample)
    if not focus.success:raise RuntimeError(focus.reason)
    save_frame(out,'focused',s.capture())
    print('Focus passed',focus.z_um,flush=True)
    transform,quality=calibrate_camera(s,20)
    save_transform(config_path,cfg,transform,quality)
    atomic_json(out/'result.json',{'focus':asdict(focus),'transform':asdict(transform),'quality':quality})
    print('Calibration passed',asdict(transform),quality,flush=True)
except BaseException:
    s.stop();raise
finally:s.close()
