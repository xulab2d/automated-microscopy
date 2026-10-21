import cv2
import numpy as np
import pytest

from flakefinder import layout
from flakefinder.model import Frame,Position


@pytest.mark.parametrize('dtype',('uint16','uint8','float32'))
def test_channelwise_resize_preserves_float_interpolation(dtype):
    rng=np.random.default_rng(67)
    rgb=rng.integers(0,255 if dtype=='uint8' else 4095,(617,739,3)).astype(dtype)
    old=cv2.resize(rgb.astype(np.float32),(384,321),interpolation=cv2.INTER_AREA)
    new=layout.resize_rgb_float32(rgb,(384,321))
    # Same area weights, allowing only floating-point accumulation order.
    np.testing.assert_allclose(new,old,rtol=5e-7,atol=.001)
    assert new.dtype==np.float32


def test_reference_fit_and_probe_labels_unchanged(monkeypatch):
    rng=np.random.default_rng(12)
    rgb=np.uint16(np.clip([2300,3000,2200]+rng.normal(0,12,(823,987,3)),0,4095))
    reference=Frame(rgb,Position(0,0,0),(.1725,.1725),0,metadata={'white_level':4095})
    probe_rgb=rgb.copy();probe_rgb[:,600:]=[950,720,850];probe_rgb[:65]=0;probe_rgb[-65:]=4095
    probe=Frame(probe_rgb,reference.position,reference.pixel_size_um,1,metadata=reference.metadata)
    resized=[];actual_resize=layout.cv2.resize
    def inspected_resize(value,*args,**kwargs):
        resized.append(value.shape)
        return actual_resize(value,*args,**kwargs)
    monkeypatch.setattr(layout.cv2,'resize',inspected_resize)
    new=layout.SubstrateReference.from_frame(reference,max_dimension=384)
    new_labels=new.classify(probe)
    assert all(len(shape)==2 for shape in resized)  # No source-sized RGB float argument.
    def original(frame,max_dimension):
        h,w=frame.rgb.shape[:2];scale=min(1.,max_dimension/max(h,w))
        return actual_resize(frame.rgb.astype(np.float32),(max(2,round(w*scale)),max(2,round(h*scale))),interpolation=cv2.INTER_AREA)
    monkeypatch.setattr(layout,'_working_rgb',original)
    old=layout.SubstrateReference.from_frame(reference,max_dimension=384)
    np.testing.assert_allclose(new.centre,old.centre,atol=1e-7,rtol=1e-6)
    np.testing.assert_allclose(new.covariance,old.covariance,atol=1e-10,rtol=1e-6)
    np.testing.assert_array_equal(new_labels,old.classify(probe))
