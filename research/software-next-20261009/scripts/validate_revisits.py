"""Three-field physical revisit test using the currently engaged 10x objective."""
from pathlib import Path
import json,sys
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from flakefinder.config import load_config,connect,arm,transform_from_config
from flakefinder.optics import save_objective_profile,optimize_exposure
from flakefinder.revisit import run_revisits
from flakefinder.model import atomic_json

config=load_config('configs/zen-validation.json')
config['zen'].update(job_template='FlakeFinder Profile Camera',camera_resources={'ff_exposure_ms':200})
config['context']['exposure_ms']=200
scope=connect(config)
try:
    arm(scope,config)
    if scope.hardware_controls().objectives()['position']!=1:raise RuntimeError('Test requires the 10x objective used for this calibration.')
    # The range stays within the already measured focus envelope.
    config['focus']={'half_range_um':9,'coarse_step_um':3,'fine_step_um':.5}
    profile=save_objective_profile(scope,config,transform_from_config(config),'configs/objective-10x-measured.json')
    config['context']['illumination_percent']=profile['illumination_percent']
    atomic_json('configs/zen-profile-local.json',config)
    result=run_revisits(scope,'data/live-validation-20261008-c/scan/analysis',profile,
        'data/revisit-validation-10x',max_candidates=3,
        callback=lambda event,frame,info:print(event,json.dumps(info),flush=True))
    print(json.dumps({'state':result['state'],'verified':result['verified_count']},indent=2),flush=True)
finally:scope.close()
