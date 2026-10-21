"""Stationary camera/exposure and reflected-light test; restores original lamp."""
from pathlib import Path
import sys,json
import numpy as np
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from flakefinder.config import load_config,connect,arm
from flakefinder.imaging import save_frame
from flakefinder.model import atomic_json

cfg=load_config('configs/zen-validation.json')
cfg['zen']['job_template']='FlakeFinder Profile Camera'
scope=connect(cfg);controls=None;original=None
folder=Path('data/optical-control-test');folder.mkdir(exist_ok=True)
result={'frames':[]}
try:
    arm(scope,cfg);controls=scope.hardware_controls()
    result['objectives']=controls.objectives();original=controls.lamp_percent()
    result['original_lamp_percent']=original
    for exposure,lamp in [(200,original),(100,original),(200,80)]:
        actual=controls.set_lamp_percent(lamp)
        scope.set_exposure_ms(exposure);frame=scope.capture()
        name=f'exposure-{exposure}-lamp-{lamp:.2f}'
        save_frame(folder,name,frame)
        data={'name':name,'exposure_ms':exposure,'lamp_percent':actual,
              'rgb_median':np.median(frame.rgb,axis=(0,1)).tolist(),
              'rgb_q99':np.percentile(frame.rgb,99,axis=(0,1)).tolist(),
              'saturated_fraction':np.mean(frame.rgb>=4095,axis=(0,1)).tolist(),
              'metadata':frame.metadata}
        result['frames'].append(data);atomic_json(folder/'result.json',result)
        print(json.dumps(data),flush=True)
finally:
    try:
        if controls and original is not None:
            result['restored_lamp_percent']=controls.set_lamp_percent(original)
            atomic_json(folder/'result.json',result)
    finally:scope.close()
