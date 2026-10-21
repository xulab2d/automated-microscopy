from __future__ import annotations
from dataclasses import dataclass, field, asdict
import json, math, time
from pathlib import Path
import numpy as np

class SafetyError(RuntimeError): pass
class AcquisitionError(RuntimeError): pass
class FocusSupportError(AcquisitionError):
    """Observed chip focus is unresolved; motion and optical checks succeeded."""
    pass
class Cancelled(RuntimeError): pass

@dataclass(frozen=True)
class Position:
    x: float
    y: float
    z: float

    def __post_init__(self):
        if not all(math.isfinite(v) for v in (self.x,self.y,self.z)):
            raise SafetyError('XYZ must be finite micrometre values.')

@dataclass
class Frame:
    rgb: np.ndarray
    position: Position
    pixel_size_um: tuple[float, float]
    sequence: int
    acquired_at: float = field(default_factory=time.time)
    metadata: dict = field(default_factory=dict)

    def __post_init__(self):
        if self.rgb.ndim != 3 or self.rgb.shape[2] != 3 or min(self.rgb.shape[:2])<2 or not np.isfinite(self.rgb).all():
            raise AcquisitionError('Expected an RGB image.')
        if len(self.pixel_size_um)!=2 or not all(math.isfinite(v) and v > 0 for v in self.pixel_size_um):
            raise AcquisitionError('Missing or invalid image calibration.')

@dataclass
class Limits:
    x_min: float
    x_max: float
    y_min: float
    y_max: float
    z_min: float
    z_max: float
    max_xy_step_um: float = 1200
    max_z_step_um: float = 25

    def __post_init__(self):
        if not all(math.isfinite(v) for v in asdict(self).values()):
            raise SafetyError('Motion limits must be finite.')
        if not (self.x_min < self.x_max and self.y_min < self.y_max and self.z_min < self.z_max):
            raise SafetyError('Each lower motion limit must be below its upper limit.')
        if self.max_xy_step_um <= 0 or self.max_z_step_um <= 0:
            raise SafetyError('Maximum motion increments must be positive.')

    def contains(self, p: Position):
        return self.x_min <= p.x <= self.x_max and self.y_min <= p.y <= self.y_max and self.z_min <= p.z <= self.z_max

    def check(self, p: Position):
        if not self.contains(p):
            raise SafetyError(f'Position {p} lies outside the configured travel envelope.')

    def check_move(self, start: Position, target: Position):
        self.check(start)
        self.check(target)
        if math.hypot(target.x-start.x,target.y-start.y) > self.max_xy_step_um + 1e-6:
            raise SafetyError('XY increment exceeds the configured limit.')
        if abs(target.z-start.z) > self.max_z_step_um + 1e-6:
            raise SafetyError('Z increment exceeds the configured limit.')
        # Encoder readback can fluctuate below the settling tolerance without a
        # commanded move. Only one axis group may change by a resolvable amount.
        if math.hypot(target.x-start.x,target.y-start.y)>0.3 and abs(target.z-start.z)>0.3:
            raise SafetyError('XY and Z must move separately.')

def atomic_json(path: Path, data):
    import tempfile,os
    path=Path(path)
    path.parent.mkdir(parents=True,exist_ok=True)
    serialized=json.dumps(data,indent=2,allow_nan=False)
    # Windows indexers/antivirus may briefly hold the previous checkpoint open.
    # Retry only the atomic replace; never delete the last complete checkpoint.
    with tempfile.NamedTemporaryFile(mode='w',encoding='utf-8',dir=path.parent,prefix='.ff-',suffix='.tmp',delete=False) as file:
        file.write(serialized);file.flush();os.fsync(file.fileno());temp=Path(file.name)
    try:
        for attempt in range(7):
            try:temp.replace(path);break
            except PermissionError:
                if attempt==6:raise
                time.sleep(min(.05*2**attempt,.5))
    finally:
        if temp.exists():temp.unlink()

@dataclass
class CameraTransform:
    """Maps camera pixel offsets from centre to sample stage coordinates (um)."""
    matrix: list[list[float]]
    pixel_size_um: tuple[float,float]
    calibrated: bool = False

    def __post_init__(self):
        a=np.asarray(self.matrix,dtype=float)
        if a.shape!=(2,2) or not np.isfinite(a).all() or abs(np.linalg.det(a))<1e-12:
            raise ValueError('Camera transform must be a finite, invertible 2x2 matrix.')
        self.pixel_size_um=tuple(self.pixel_size_um)
        if len(self.pixel_size_um)!=2 or not all(math.isfinite(v) and v>0 for v in self.pixel_size_um):
            raise ValueError('Camera pixel sizes must be finite and positive.')

    def validate_frame(self, f: Frame):
        if not np.allclose(f.pixel_size_um,self.pixel_size_um,rtol=0.01,atol=0):
            raise SafetyError('Pixel scale changed: objective or camera mode changed. Recalibrate.')

    def stage_xy(self, pixel, frame: Frame):
        self.validate_frame(frame)
        centre=np.array([(frame.rgb.shape[1]-1)/2,(frame.rgb.shape[0]-1)/2])
        return np.array([frame.position.x,frame.position.y]) + np.asarray(self.matrix) @ (np.asarray(pixel)-centre)
