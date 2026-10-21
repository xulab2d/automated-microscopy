"""Record the measured fixed acquisition profile and its repeat noise."""
from pathlib import Path
import sys,json
import numpy as np
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from flakefinder.config import connect,arm,transform_from_config
from flakefinder.imaging import save_frame
from flakefinder.optics import optimize_exposure,save_objective_profile,image_quality
from flakefinder.model import atomic_json

path=Path('configs/zen-20x-quantitative.json');cfg=json.loads(path.read_text())
cfg['zen']['camera_resources']={'ff_exposure_ms':250,'ff_white_balance':'0.25007564,1,2.61244442'}
out=Path('data/20x-fixed-profile');out.mkdir(parents=True,exist_ok=True)
s=connect(cfg)
try:
 arm(s,cfg)
 result=optimize_exposure(s,out/'exposure',250,max_iterations=7)
 cfg['zen']['camera_resources']=dict(s.camera_resources)
 cfg['context'].update(exposure_ms=result['exposure_ms'],white_balance=s.camera_resources['ff_white_balance'],
   color_profile_kind='fixed acquisition channel balance; not a neutral-reference color calibration',white_level=4095)
 frames=[]
 for i in range(4):
  f=s.capture();save_frame(out,f'repeat_{i}',f);frames.append(f.rgb.astype(np.float32))
 diff=frames[1]-frames[0];median=np.median(frames[0],axis=(0,1))
 noise=np.median(abs(diff-np.median(diff,axis=(0,1))),axis=(0,1))*1.4826/np.sqrt(2)
 quality={**image_quality(f),'repeat_noise_sigma_rgb':noise.tolist(),'relative_repeat_noise_rgb':(noise/median).tolist()}
 atomic_json(out/'quality.json',quality);atomic_json(path,cfg)
 save_objective_profile(s,cfg,transform_from_config(cfg),'configs/objective-20x-quantitative.json')
 print('COMPLETE',quality,flush=True)
finally:s.close()
