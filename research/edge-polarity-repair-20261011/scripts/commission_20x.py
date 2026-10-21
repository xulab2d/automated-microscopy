"""Commission the engaged 20x objective and preserve every focus/calibration frame."""
from pathlib import Path
import sys,json
from dataclasses import asdict
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from flakefinder.config import connect,arm,save_transform
from flakefinder.autofocus import autofocus
from flakefinder.calibration import calibrate_camera
from flakefinder.optics import optimize_exposure,save_objective_profile
from flakefinder.imaging import save_frame
from flakefinder.model import atomic_json

cfg=json.loads(Path('configs/zen-profile-local.json').read_text())
cfg['camera_transform']=None;cfg.pop('calibration_quality',None)
cfg['zen'].update(expected_objective_position=2,camera_white_level=4095)
cfg['context']['objective']='EC Epiplan 20x/0.4 HD M27'
cfg['focus']={'half_range_um':12,'coarse_step_um':2,'fine_step_um':.5}
cfg['initial_focus']={'low':15595,'high':15635,'coarse_step_um':2,'fine_step_um':.5}
cfg['calibration_step_um']=30
cfg['notes']='20x objective; measured stage transform and fixed illumination/exposure. Detection requires separate validation.'
out=Path('data/20x-commissioning')
s=connect(cfg)
try:
 s.hardware_controls().verify_optics(2);arm(s,cfg)
 atomic_json(out/'origin.json',asdict(s.position()))
 exposure=optimize_exposure(s,out/'exposure',200)
 cfg['zen']['camera_resources']['ff_exposure_ms']=exposure['exposure_ms']
 cfg['context']['exposure_ms']=exposure['exposure_ms']
 def sample(frame,info):
  save_frame(out/'focus-frames',f"{info['phase']}-{info['z_um']:.3f}",frame)
  print('FOCUS',info,flush=True)
 result=autofocus(s,**cfg['focus'],output=out/'focus',callback=sample)
 if not result.success:raise RuntimeError(result.reason)
 transform,quality=calibrate_camera(s,30,out/'calibration')
 save_transform('configs/zen-20x-local.json',cfg,transform,quality)
 save_objective_profile(s,cfg,transform,'configs/objective-20x-measured.json')
 save_frame(out,'focused',s.capture())
 atomic_json(out/'result.json',{'focus':asdict(result),'transform':asdict(transform),'quality':quality,'exposure':exposure})
 print('COMPLETE',quality,flush=True)
except BaseException:s.stop();raise
finally:s.close()
