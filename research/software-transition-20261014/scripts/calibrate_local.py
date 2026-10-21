from pathlib import Path
import sys,time
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from flakefinder.config import load_config,connect,arm,save_transform
from flakefinder.calibration import calibrate_camera
from flakefinder.model import atomic_json
cfg=load_config('configs/zen-local.json');s=connect(cfg)
try:
    cfg['camera_transform']=None
    atomic_json(Path('configs/zen-local.json'),cfg)
    arm(s,cfg)
    transform,quality=calibrate_camera(s,50,Path('data/calibration-'+time.strftime('%Y%m%d-%H%M%S')))
    save_transform('configs/zen-local.json',cfg,transform,quality)
    print(transform,quality,flush=True)
except BaseException:s.stop();raise
finally:s.close()
