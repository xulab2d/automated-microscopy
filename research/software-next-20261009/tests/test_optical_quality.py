import numpy as np
import pytest
from types import SimpleNamespace as N
from flakefinder.model import Frame,Position,AcquisitionError,SafetyError
from flakefinder.optics import image_quality,optimize_exposure
from flakefinder.mtb import MtbControls

def test_dark_noise_is_not_optimized_into_a_bright_preview(tmp_path):
    f=Frame(np.full((100,100,3),20,np.uint16),Position(0,0,0),(1,1),0)
    with pytest.raises(AcquisitionError,match='dark-noise'):image_quality(f)

def test_exposure_preserves_small_bright_objects(tmp_path):
    class Camera:
        armed=True
        def check_cancel(self):pass
        def set_exposure_ms(self,value):self.exposure=value
        def capture(self):
            a=np.full((100,100,3),self.exposure*10)
            a[30:40,30:40]=self.exposure*30
            return Frame(np.uint16(np.minimum(a,4095)),Position(0,0,0),(1,1),0)
    s=Camera();r=optimize_exposure(s,tmp_path,200,max_iterations=8)
    assert r['converged']
    assert max(r['steps'][-1]['whole_frame_saturated_fraction'])<.001
    assert s.exposure==r['exposure_ms']

def test_invalid_or_changed_objective_blocks_optical_profile():
    c=object.__new__(MtbControls)
    c.lamp_percent=lambda:90
    c.objectives=lambda:{'valid_position':False,'position':0}
    with pytest.raises(SafetyError,match='between stops'):c.verify_optics(2)
    c.objectives=lambda:{'valid_position':True,'position':1}
    with pytest.raises(SafetyError,match='does not match'):c.verify_optics(2)
    c.objectives=lambda:{'valid_position':True,'position':2}
    c.lamp_percent=lambda:-.0978
    with pytest.raises(AcquisitionError,match='illumination'):c.verify_optics(2)
