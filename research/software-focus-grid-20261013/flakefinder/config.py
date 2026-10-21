"""Configuration is data only. Never evaluate configuration strings as code."""
from dataclasses import asdict
from pathlib import Path
import json
from .model import Limits, CameraTransform, SafetyError, atomic_json

def load_config(path):
    data=json.loads(Path(path).read_text(encoding='utf-8-sig'))
    if data.get('schema')!=1: raise ValueError('Expected configuration schema 1.')
    if data.get('backend') not in ('simulator','zen'): raise ValueError('Unknown microscope backend.')
    reference=data.get('analysis',{}).get('material_profile')
    if isinstance(reference,str):
        reference_path=Path(reference)
        if not reference_path.is_absolute():reference_path=Path(path).resolve().parent/reference_path
        data['analysis']['material_profile']=str(reference_path.resolve())
    return data

def connect(config):
    if config['backend']=='simulator':
        from .simulator import SimulatedMicroscope
        return SimulatedMicroscope()
    from .zen import ZenMicroscope
    return ZenMicroscope(**config.get('zen',{}))

def arm(scope,config):
    if not config.get('limits'):
        raise SafetyError('Set the specimen-specific XYZ travel envelope in the configuration first.')
    scope.arm(Limits(**config['limits']))
    scope.expected_scale=tuple(config['camera_transform']['pixel_size_um']) if config.get('camera_transform') else None

def transform_from_config(config):
    if not config.get('camera_transform'): raise SafetyError('Calibrate the camera-to-stage transform first.')
    return CameraTransform(**config['camera_transform'])

def save_transform(path,config,transform,quality):
    config['camera_transform']=asdict(transform)
    config['calibration_quality']=quality
    atomic_json(Path(path),config)

def demo_config():
    return {'schema':1,'backend':'simulator','limits':asdict(Limits(-100,2400,-100,2400,60,150,1200,25)),
        'survey':{'bounds_um':[0,2300,0,2300],'autofocus_every':0,'mode':'survey'},
        'grid':{'expected_count':15,'grid_shape':[4,4],'polarity':'bright'},
        'focus':{'half_range_um':12,'coarse_step_um':3,'fine_step_um':0.5},
        'initial_focus':{'low':65,'high':135,'coarse_step_um':5,'fine_step_um':0.5},
        'detail':{'autofocus_every':2,'focus_half_range_um':12,'focus_step_um':3,'edge_margin_um':12},
        'context':{'material':'MoTe2','substrate':'SiO2/Si','oxide_nm':285,'oxide_process':'wet',
                   'objective':'SIMULATION','illumination':'SIMULATION'},'material_model':None}
