"""Compare fixed vendor white balance on one stationary field; preserve raw pairs."""
from pathlib import Path
import sys,json
import numpy as np
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from flakefinder.config import connect,arm,transform_from_config
from flakefinder.optics import image_quality,optimize_exposure,save_objective_profile
from flakefinder.imaging import save_frame
from flakefinder.model import atomic_json

cfg=json.loads(Path('configs/zen-20x-local.json').read_text())
cfg['zen']['job_template']='FlakeFinder Quantitative Camera'
out=Path('data/20x-color-comparison');out.mkdir(parents=True,exist_ok=True)
profiles={'previous':'0.027312621062124,0.10921695976961,0.14266182058968',
          'vendor5500':'0.45050752,1,0.571064'}
s=connect(cfg);records={}
try:
 arm(s,cfg);s.hardware_controls().verify_optics(2)
 for name,wb in profiles.items():
  s.camera_resources={'ff_exposure_ms':100,'ff_white_balance':wb}
  frames=[]
  for i in range(2):
   f=s.capture();save_frame(out,f'{name}_{i}',f);frames.append(f.rgb.astype(np.float32))
  diff=frames[1]-frames[0];med=np.median(frames[0],axis=(0,1))
  noise=np.median(abs(diff-np.median(diff,axis=(0,1))),axis=(0,1))*1.4826/np.sqrt(2)
  records[name]={'white_balance':wb,'exposure_ms':100,**image_quality(f),
                 'repeat_noise_sigma_rgb':noise.tolist(),'relative_repeat_noise_rgb':(noise/np.maximum(med,1)).tolist()}
  print(name,records[name],flush=True)
 atomic_json(out/'comparison.json',records)
 # Select after measurement: this script records both profiles and leaves the
 # vendor setting for an independently inspectable exposure optimization.
 optimized=optimize_exposure(s,out/'vendor-exposure',100,max_iterations=7)
 cfg['zen']['camera_resources']=dict(s.camera_resources)
 cfg['context'].update(exposure_ms=optimized['exposure_ms'],white_balance=profiles['vendor5500'],
                       illumination_percent=s.hardware_controls().lamp_percent(),white_level=4095)
 atomic_json('configs/zen-20x-quantitative.json',cfg)
 save_objective_profile(s,cfg,transform_from_config(cfg),'configs/objective-20x-quantitative.json')
 save_frame(out,'vendor_optimized',s.capture())
 print('COMPLETE',optimized,flush=True)
except BaseException:s.stop();raise
finally:s.close()
