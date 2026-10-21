"""Measure fixed channel response choices, preserving acquisition and comparison data."""
from pathlib import Path
import sys,json
import numpy as np
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from flakefinder.config import connect,arm,transform_from_config
from flakefinder.imaging import save_frame
from flakefinder.optics import image_quality,optimize_exposure,save_objective_profile
from flakefinder.model import atomic_json

cfg=json.loads(Path('configs/zen-20x-quantitative.json').read_text())
out=Path('data/20x-channel-response');out.mkdir(parents=True,exist_ok=True)
vectors={'old_normalized':'0.25007564,1,1.30622221','red_boost':'0.12503782,1,1.30622221',
         'blue_reduced':'0.25007564,1,2.61244442','balanced':'0.12503782,1,2.61244442'}
s=connect(cfg);records={}
try:
 arm(s,cfg)
 for name,value in vectors.items():
  s.camera_resources={'ff_exposure_ms':100,'ff_white_balance':value}
  f=s.capture();save_frame(out,name,f);records[name]={'white_balance':value,**image_quality(f)}
  print(name,records[name]['rgb_median'],records[name]['rgb_q95'],flush=True)
 atomic_json(out/'response.json',records)
finally:s.close()
