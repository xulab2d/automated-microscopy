"""Fixed optical profiles and exposure optimization without per-tile color drift."""
from dataclasses import asdict
from pathlib import Path
import time
import numpy as np
from .model import SafetyError,AcquisitionError,atomic_json
from .imaging import save_frame
from .autofocus import make_focus_mask
from .vendor_lifecycle import acknowledge_raw,file_hash

def image_quality(frame,white_level=4095):
    if white_level<=0:raise ValueError('Specify the camera ADC white level.')
    mask=make_focus_mask(frame.rgb).astype(bool)
    if mask.sum()<1000:raise AcquisitionError('No usable illuminated substrate for exposure optimization.')
    pixels=frame.rgb[mask].astype(float)
    if np.max(np.percentile(pixels,95,axis=0))<white_level*.02:
        raise AcquisitionError('Signal is at the dark-noise level. Check illumination and optical path before increasing exposure.')
    all_pixels=frame.rgb.reshape(-1,3).astype(float)
    return {'substrate_pixels':int(mask.sum()),'rgb_median':np.median(pixels,axis=0).tolist(),
            'rgb_q95':np.percentile(pixels,95,axis=0).tolist(),
            'rgb_q999':np.percentile(all_pixels,99.9,axis=0).tolist(),
            'saturated_fraction':np.mean(pixels>=white_level,axis=0).tolist(),
            'whole_frame_saturated_fraction':np.mean(all_pixels>=white_level,axis=0).tolist(),'white_level':white_level}

def optimize_exposure(scope,output,initial_ms=200,white_level=4095,target_fraction=.72,max_iterations=5):
    if not scope.armed:raise SafetyError('Arm the microscope before changing acquisition settings.')
    if not .3<=target_fraction<=.85:raise ValueError('Target signal must be 30..85% of ADC range.')
    from .storage import managed_output_path
    output=managed_output_path(output);steps=[];exposure=float(initial_ms)
    for i in range(max_iterations):
        scope.check_cancel();scope.set_exposure_ms(exposure)
        frame=scope.capture()
        save_frame(output,f'exposure_{i:02d}',frame)
        acknowledge_raw(scope,frame,output/f'exposure_{i:02d}.tiff')
        quality=image_quality(frame,white_level)
        steps.append({'exposure_ms':exposure,**quality})
        level=max(quality['rgb_q95']);ratio=target_fraction*white_level/max(level,1)
        # Preserve bright-object color as well as substrate signal. A clipped
        # value cannot reveal its true intensity, so reduce and remeasure.
        if max(quality['whole_frame_saturated_fraction'])>.001:ratio=min(ratio,.8)
        else:ratio=min(ratio,.95*white_level/max(max(quality['rgb_q999']),1))
        if .94<ratio<1.06:break
        exposure=float(np.clip(exposure*np.clip(ratio,.4,2),1,10000))
    # The last measured setting is the fixed profile; do not claim an unmeasured guess.
    chosen=steps[-1]['exposure_ms'];scope.set_exposure_ms(chosen)
    result={'exposure_ms':chosen,'steps':steps,'target_fraction':target_fraction,
            'converged':.94<ratio<1.06,'white_balance':scope.camera_resources.get('ff_white_balance','preserved fixed camera-job values') if hasattr(scope,'camera_resources') else 'preserved fixed camera-job values'}
    atomic_json(output/'exposure-optimization.json',result)
    return result

def save_objective_profile(scope,config,transform,path):
    """Save only a measured camera transform for the currently engaged objective."""
    if not transform.calibrated:raise SafetyError('Measure the camera transform on this objective first.')
    from .storage import resolve_output_path
    path=resolve_output_path(path)
    if path.exists():raise SafetyError('Choose a new objective profile path; an existing profile is immutable.')
    evidence=path.parent/(path.stem+'-evidence')
    if evidence.exists():raise SafetyError('Objective evidence destination already exists; choose a new profile path.')
    sample=config.get('context',{}).get('sample_id')
    if not sample:raise SafetyError('Set a sample ID before creating a revisit profile.')
    controls=scope.hardware_controls();state=controls.objectives()
    frame=scope.capture();transform.validate_frame(frame)
    objective=next(o for o in state['objectives'] if o['position']==state['position'])
    lamp=controls.lamp_percent()
    save_frame(evidence,'profile-frame',frame)
    receipt=acknowledge_raw(scope,frame,evidence/'profile-frame.tiff')
    result={'schema':1,'created_at':time.time(),'objective':objective,'motorization':state['motorization'],
            'camera_transform':asdict(transform),'context':{**config.get('context',{}),'objective':objective['name'],
                 'illumination_percent':lamp,'exposure_ms':scope.camera_resources.get('ff_exposure_ms',config.get('context',{}).get('exposure_ms'))},
            'job_template':scope.job_template,'camera_resources':dict(scope.camera_resources),
            'illumination_percent':lamp,'focus':dict(config['focus']),
            'calibration_position':asdict(frame.position),'source':frame.metadata.get('source'),
            'verification_capture':{'path':str(evidence/'profile-frame.tiff'),
                'sha256':file_hash(evidence/'profile-frame.tiff'),
                'sidecar_sha256':file_hash(evidence/'profile-frame.json'),'vendor_receipt':receipt},
            'parcentric_offset':None,'parcentric_status':'not_measured'}
    atomic_json(path,result);return result
