"""Measured stage-side color support from an independently verified raw edge.

This is background compatibility, not a material classifier. Multiplicative
stage brightness may vary; substrate-like chromaticity is explicitly excluded
even below the normal exposure gate. No all-dark-pixels shortcut is used.
"""
import hashlib
import json
import math
from pathlib import Path
import numpy as np

from .model import AcquisitionError, SafetyError

KEY = 'measured_stage_background'


def pixel_hash(frame):
    view = memoryview(np.ascontiguousarray(frame.rgb)).cast('B')
    result = hashlib.sha256()
    for start in range(0, len(view), 1024*1024):
        result.update(view[start:start+1024*1024])
    return result.hexdigest()


def _checksum(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(',',':'), allow_nan=False).encode()).hexdigest()


def build_model(reference, edge_frame, evidence, transform, controls, *, provenance):
    from .layout import _context
    if evidence.get('raw_validation',{}).get('accepted') is not True or controls.get('accepted') is not True:
        raise SafetyError('Stage background needs independently accepted raw edge polarity and bright-side correspondence.')
    if _context(reference) != _context(edge_frame) or not np.allclose(reference.pixel_size_um, edge_frame.pixel_size_um, rtol=0, atol=1e-9):
        raise SafetyError('Background control and substrate reference differ in optical context or pixel scale.')
    for key in ('edge_tiff_sha256','edge_metadata_sha256'):
        if not isinstance(provenance.get(key),str) or len(provenance[key]) != 64:
            raise SafetyError('Background source receipt is missing: '+key)
    h,w = edge_frame.rgb.shape[:2]
    stride = max(1, math.ceil(max(h,w)/512))
    yy,xx = np.mgrid[0:h:stride,0:w:stride]
    normal = np.asarray(evidence['edge']['normal'],float)
    pixel_normal = np.asarray(transform.matrix).T@normal
    distance = ((xx-(w-1)/2)*pixel_normal[0]+(yy-(h-1)/2)*pixel_normal[1]
        +np.array([edge_frame.position.x,edge_frame.position.y])@normal-evidence['edge']['offset_um'])
    outside = distance > 20.
    white = float(reference.metadata['white_level'])
    sampled = edge_frame.rgb[::stride,::stride]
    usable = outside & (sampled.min(axis=2)>4.) & (sampled.max(axis=2)<.98*white)
    pixels = sampled[usable].astype(np.float64)  # bounded: at most 512x512 RGB
    if len(pixels)<100 or np.mean(outside)<.05:
        raise AcquisitionError('Too few measured stage-side pixels away from the raw transition.')
    bright = np.asarray(controls['bright_control_rgb'],float)
    dark = np.median(pixels,axis=0)
    delta = bright-dark
    if not np.isfinite(delta).all() or np.any(delta<=.1*np.maximum(bright,1.)):
        raise AcquisitionError('Measured background does not separate from the bright-side control.')
    features = np.log(pixels[:,[0,2]]/pixels[:,1:2])
    centre = np.median(features,axis=0)
    spread = np.maximum(1.4826*np.median(abs(features-centre),axis=0), [.035,.035])
    core = features[(abs(features-centre)<3*spread).all(axis=1)]
    if len(core)<100 or len(core)<.5*len(features):
        raise AcquisitionError('Measured stage-side color is not a robust dominant component.')
    centre = np.median(core,axis=0)
    covariance = np.cov(core,rowvar=False)
    covariance += np.diag(np.maximum(0.,np.array([.035,.035])**2-np.diag(covariance)))+np.eye(2)*1e-10
    value = dict(schema=1,kind='independent_raw_edge_stage_background_v1',
        context=_context(reference),pixel_size_um=list(reference.pixel_size_um),
        reference_pixels_sha256=pixel_hash(reference),reference_shape=list(reference.rgb.shape),reference_dtype=str(reference.rgb.dtype),
        edge_pixels_sha256=pixel_hash(edge_frame),provenance=dict(provenance),edge=evidence['edge'],
        raw_edge_validation=evidence['raw_validation'],reference_controls=controls,
        sampling={'stride_px':stride,'minimum_outside_distance_um':20.,'sample_count':len(pixels),'core_count':len(core)},
        centre=centre.tolist(),covariance=covariance.tolist(),dark_control_rgb=dark.tolist(),bright_control_rgb=bright.tolist(),
        rgb_upper=(dark+.35*delta).tolist(),rgb_lower_exclusive=4.,mahalanobis_max=3.5,
        substrate_chromatic_exclusion_max=3.5,white_level=white,
        qualification='Measured stage-side chromatic compatibility below 35% of the independently measured bright-dark separation; brightness may vary. Substrate-colored shadows excluded. Not chemical identity or a confidence bound.')
    value['model_sha256'] = _checksum(value)
    return value


def validate_model(frame):
    from .layout import _context
    value = frame.metadata.get(KEY)
    if value is None:
        return None
    if not isinstance(value,dict) or value.get('schema')!=1 or value.get('kind')!='independent_raw_edge_stage_background_v1':
        raise ValueError('Invalid measured stage-background model.')
    payload = {k:v for k,v in value.items() if k!='model_sha256'}
    if value.get('model_sha256') != _checksum(payload):
        raise ValueError('Measured stage-background parameters/provenance changed.')
    if (value['context']!=_context(frame) or value['pixel_size_um']!=list(frame.pixel_size_um)
            or value['reference_shape']!=list(frame.rgb.shape) or value['reference_dtype']!=str(frame.rgb.dtype)
            or value['reference_pixels_sha256']!=pixel_hash(frame)):
        raise ValueError('Measured stage-background model belongs to a different reference/context/pixel scale.')
    if value['raw_edge_validation'].get('accepted') is not True or value['reference_controls'].get('accepted') is not True:
        raise ValueError('Measured stage-background evidence is not accepted.')
    return value


def compatible_pixels(rgb, model, substrate_distance):
    features = np.log(np.maximum(rgb[:,:,[0,2]],1.)/np.maximum(rgb[:,:,1:2],1.))
    delta = features-np.asarray(model['centre'])
    d2 = np.einsum('...i,ij,...j->...',delta,np.linalg.inv(model['covariance']),delta)
    return ((rgb.min(axis=2)>model['rgb_lower_exclusive'])
        & (rgb<np.asarray(model['rgb_upper'])).all(axis=2)
        & (rgb.max(axis=2)<.98*model['white_level'])
        & (d2<=model['mahalanobis_max']**2)
        & (substrate_distance>model['substrate_chromatic_exclusion_max']))
