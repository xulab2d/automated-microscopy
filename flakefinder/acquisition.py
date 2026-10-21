"""Stationary, auditable temporal averaging for final acquisition images.

Autofocus should continue to use individual camera frames. This helper neither
moves the microscope nor aligns/resamples images: excessive drift rejects the
burst, preserving its original camera frames for diagnosis.
"""
from copy import deepcopy
from dataclasses import asdict
from pathlib import Path
import hashlib
import json
import operator
import uuid

import numpy as np

from .calibration import register_translation
from .imaging import save_frame
from .model import AcquisitionError, Frame, atomic_json


MAX_TRANSLATION_PX = 0.5
MAX_POSITION_DELTA_UM = 0.15
# ZEN gives each camera-only job a new experiment ID; all other metadata must
# remain unchanged, including objective, exposure, balance, lamp, and ADC range.
TRANSIENT_METADATA_KEYS = frozenset({'experiment_id'})


def _camera_context(metadata):
    return json.dumps({k:v for k,v in metadata.items() if k not in TRANSIENT_METADATA_KEYS},
                      sort_keys=True,allow_nan=False)


def capture_average(scope, count, output, name):
    """Return one frame or the rounded mean of a verified stationary burst.

    ``count=1`` is an ordinary, unchanged ``scope.capture()``. For larger counts,
    original uint8/uint16 camera TIFFs and sidecars are saved beneath
    ``output/name-frames/attempt-<uuid>/``. Every retry uses its own directory.
    The returned Frame is not saved here; the caller saves its final tile.

    Readback fluctuations up to 0.15 um per axis are allowed, but registered
    image displacement must also remain within 0.5 pixel of the first frame.
    No coordinate warping, white balance, gamma, or intensity normalization is
    applied. Unreliable registration also rejects the burst.
    """
    if isinstance(count,bool):raise ValueError('Average count must be a positive integer.')
    try:count=operator.index(count)
    except TypeError as exc:raise ValueError('Average count must be a positive integer.') from exc
    if count<1:raise ValueError('Average count must be a positive integer.')
    if count==1:return scope.capture()
    if not isinstance(name,str) or not name or name in ('.','..') or any(c in name for c in '/\\:\0'):
        raise ValueError('Burst name must be one filename without a directory.')
    root=Path(output).resolve();attempt=root/(name+'-frames')/('attempt-'+uuid.uuid4().hex)
    attempt.mkdir(parents=True,exist_ok=False)
    record={'schema':1,'processing':'temporal_mean','requested_count':count,'status':'capturing',
            'members':[],'max_translation_px':MAX_TRANSLATION_PX,
            'max_position_delta_um':MAX_POSITION_DELTA_UM,'warped':False}
    atomic_json(attempt/'burst.json',record)
    first=None;total=None;reference_context=None
    try:
        for index in range(count):
            check_cancel=getattr(scope,'check_cancel',None)
            if callable(check_cancel):check_cancel()
            observed=scope.capture()
            if observed.rgb.dtype not in (np.dtype('uint8'),np.dtype('uint16')):
                raise AcquisitionError('Temporal averaging requires losslessly supported uint8 or uint16 camera TIFF data.')
            if observed.metadata.get('acquisition_processing'):
                raise AcquisitionError('Temporal averaging requires original camera frames, not previously processed acquisitions.')
            # Snapshot buffers and metadata: some camera interfaces reuse them.
            frame=Frame(observed.rgb.copy(),observed.position,tuple(observed.pixel_size_um),
                        observed.sequence,observed.acquired_at,deepcopy(observed.metadata))
            member_name=f'frame_{index:04d}'
            save_frame(attempt,member_name,frame)
            member={'image':(attempt/(member_name+'.tiff')).relative_to(root).as_posix(),
                    'sidecar':(attempt/(member_name+'.json')).relative_to(root).as_posix(),
                    'sequence':frame.sequence,'acquired_at':frame.acquired_at,
                    'position':asdict(frame.position),'rgb_sha256':hashlib.sha256(frame.rgb.tobytes()).hexdigest()}
            record['members'].append(member)
            if first is None:
                first=frame;total=frame.rgb.astype(np.float64)
                reference_context=_camera_context(first.metadata)
                member.update(position_delta_um=[0.,0.,0.],translation_px=[0.,0.],registration_response=None)
            else:
                if frame.rgb.shape!=first.rgb.shape:raise AcquisitionError('Camera shape changed during the averaging burst.')
                if frame.rgb.dtype!=first.rgb.dtype:raise AcquisitionError('Camera pixel dtype changed during the averaging burst.')
                if frame.pixel_size_um!=first.pixel_size_um:raise AcquisitionError('Camera pixel scale changed during the averaging burst.')
                if _camera_context(frame.metadata)!=reference_context:
                    raise AcquisitionError('Camera context or objective changed during the averaging burst.')
                delta=np.array([frame.position.x-first.position.x,frame.position.y-first.position.y,frame.position.z-first.position.z])
                member['position_delta_um']=delta.tolist()
                if np.max(np.abs(delta))>MAX_POSITION_DELTA_UM+1e-9:
                    raise AcquisitionError('XYZ changed beyond the 0.15 um readback tolerance during the averaging burst.')
                shift,response=register_translation(first.rgb,frame.rgb)
                shift=np.asarray(shift,dtype=float)
                if shift.shape!=(2,) or not np.isfinite(shift).all() or not np.isfinite(response) or response<.3:
                    raise AcquisitionError('Image registration is unreliable during the averaging burst.')
                member.update(translation_px=shift.tolist(),registration_response=float(response))
                if float(np.linalg.norm(shift))>MAX_TRANSLATION_PX:
                    raise AcquisitionError(f'Image drift is {np.linalg.norm(shift):.3f} pixels, exceeding the 0.5 pixel averaging limit.')
                total+=frame.rgb
            atomic_json(attempt/'burst.json',record)
        mean=np.rint(total/count).astype(first.rgb.dtype)
        record.update(status='complete',frame_count=count,
                      measured_max_translation_px=max(float(np.linalg.norm(m['translation_px'])) for m in record['members']),
                      measured_max_position_delta_um=np.max(np.abs([m['position_delta_um'] for m in record['members']]),axis=0).tolist(),
                      started_at=record['members'][0]['acquired_at'],completed_at=record['members'][-1]['acquired_at'],
                      accumulator_dtype='float64',output_dtype=str(first.rgb.dtype),rounding='nearest, ties to even')
        atomic_json(attempt/'burst.json',record)
        metadata=deepcopy(first.metadata)
        metadata['acquisition_processing']={**record,'manifest':(attempt/'burst.json').relative_to(root).as_posix()}
        return Frame(mean,first.position,first.pixel_size_um,frame.sequence,frame.acquired_at,metadata)
    except BaseException as exc:
        record.update(status='failed',error_type=type(exc).__name__,error=str(exc))
        atomic_json(attempt/'burst.json',record)
        raise
