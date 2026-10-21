"""The preview fast path must preserve every displayed camera level exactly."""
from io import BytesIO

import numpy as np
import pytest
from PIL import Image

from flakefinder.imaging import preview


def original_preview(rgb,white_level=None):
    if white_level is None:
        white_level=max(float(np.percentile(rgb,99.7))*1.05,1)
    return np.uint8(np.clip(rgb.astype(np.float32)/white_level,0,1)**0.6*255)


@pytest.mark.parametrize('white_level',[1,255,4095,4299.75,65535,
    np.float32(4095.2),np.float64(4095.2)])
@pytest.mark.parametrize('dtype',[np.uint8,np.uint16])
def test_every_unsigned_camera_level_is_identical(dtype,white_level):
    rgb=np.arange(np.iinfo(dtype).max+1,dtype=dtype)
    before=rgb.copy()
    assert np.array_equal(preview(rgb,white_level),original_preview(rgb,white_level))
    assert np.array_equal(rgb,before)


def test_noncontiguous_rgb_automatic_scaling_and_jpeg_are_unchanged():
    rgb=np.random.default_rng(42).integers(0,4096,(127,181,3),dtype=np.uint16)[:,:,::-1]
    actual=preview(rgb)
    expected=original_preview(rgb)
    assert np.array_equal(actual,expected)
    old_jpeg,new_jpeg=BytesIO(),BytesIO()
    Image.fromarray(expected).save(old_jpeg,format='JPEG',quality=90)
    Image.fromarray(actual).save(new_jpeg,format='JPEG',quality=90)
    assert new_jpeg.getvalue()==old_jpeg.getvalue()


@pytest.mark.parametrize('dtype',[np.float32,np.float64,np.int16,np.uint32])
def test_non_camera_dtypes_keep_original_scaling(dtype):
    rgb=np.array([0,1,17,128,255,1024,4095,8192],dtype=dtype)
    assert np.array_equal(preview(rgb,4095),original_preview(rgb,4095))


def test_array_white_levels_keep_original_broadcast_scaling():
    rgb=np.array([[[0,700,1500],[4095,2000,1000]]],dtype=np.uint16)
    white=np.array([4095,3000,2000],dtype=np.float32)
    assert np.array_equal(preview(rgb,white),original_preview(rgb,white))
