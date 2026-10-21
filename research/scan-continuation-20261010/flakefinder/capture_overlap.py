"""Offline qualification schema for CPU processing during a ZEN job's tail.

The streaming response and RunJob future are independent installed contracts.
Image arrival does not establish successful job completion. The ZEN adapter
therefore returns no frame/result until its usual completion and unload fence.
"""
import hashlib
import json
from pathlib import Path
from .model import SafetyError

BACKEND='zen_camera_job_processor_overlap_v1'
def profile(job,resources,objective,white_level):
    return {'job_template':job,'camera_resources':resources,'objective_position':objective,'white_level':float(white_level)}
def source_identity():
    root=Path(__file__).parent
    return {name:hashlib.sha256((root/name).read_bytes()).hexdigest() for name in ('zen.py','capture_overlap.py')}
def validate_acceptance(path,job,resources,objective,white_level):
    if path is None:raise SafetyError('Processing overlap requires a stationary acceptance record.')
    data=json.loads(Path(path).read_text(encoding='utf-8'))
    if (data.get('schema')!=1 or data.get('backend')!=BACKEND or data.get('stationary_verified') is not True
            or data.get('source_sha256')!=source_identity() or data.get('profile')!=profile(job,resources,objective,white_level)
            or data.get('raw_profile_equivalent') is not True or data.get('late_error_tests_passed') is not True
            or data.get('completed_pairs',0)<3):
        raise SafetyError('Processing-overlap stationary acceptance does not match this source and camera profile.')
    return {'record':str(Path(path).resolve()),'sha256':hashlib.sha256(Path(path).read_bytes()).hexdigest(),
            'backend':BACKEND,'completed_pairs':data['completed_pairs']}

def capture_metric(scope,metric):
    """Use a qualified pure-CPU overlap capability, otherwise identical fallback."""
    if hasattr(scope,'capture_with_processing'):return scope.capture_with_processing(metric)
    frame=scope.capture();return frame,metric(frame)
