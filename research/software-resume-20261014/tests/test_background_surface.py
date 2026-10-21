"""Quadratic recovery, robust exclusions, and substrate-mask behavior."""
import numpy as np
import pytest

from flakefinder.imaging import background_surface
from flakefinder.model import AcquisitionError


def polynomial(shape=(321,457)):
    y,x=np.mgrid[-1:1:complex(shape[0]),-1:1:complex(shape[1])]
    channels=[]
    for base,linear_x,linear_y,xx,xy,yy in [
        (2100,120,-85,55,29,-17),
        (2800,-145,120,73,-43,21),
        (1700,89,65,-37,61,92),
    ]:
        channels.append(base+linear_x*x+linear_y*y+xx*x*x+xy*x*y+yy*y*y)
    return np.stack(channels,axis=-1).astype(np.float32)


def test_quadratic_recovery_with_robust_outlier_rejection():
    expected=polynomial()
    rgb=expected.copy()
    rgb[45:92,62:141]*=1.8
    rgb[210:260,340:414]*=.35
    actual=background_surface(rgb)
    assert actual.shape==rgb.shape and actual.dtype==np.float32
    assert np.max(abs(actual-expected))<.001
    assert np.max(abs((actual-expected)/expected))<5e-7


def test_substrate_mask_excludes_large_non_substrate_region():
    expected=polynomial((111,169))
    mask=np.zeros(expected.shape[:2],bool)
    mask[8:105,75:160]=True
    rgb=expected.copy();rgb[~mask]=200
    actual=background_surface(rgb,mask)
    assert np.max(abs(actual-expected))<.001


def test_integer_camera_data_and_insufficient_substrate():
    rgb=np.empty((51,73,3),np.uint16);rgb[:]=[1000,2400,1800]
    assert np.max(abs(background_surface(rgb)-rgb))<.001
    mask=np.zeros(rgb.shape[:2],bool);mask.ravel()[:99]=True
    with pytest.raises(AcquisitionError,match='Not enough exposed substrate'):
        background_surface(rgb,mask)
