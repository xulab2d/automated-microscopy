"""Temporal noise reduction, unchanged raw values, and stationary-frame guards."""
from copy import deepcopy
import json

import cv2
import numpy as np
import pytest

from flakefinder.acquisition import capture_average
from flakefinder.imaging import load_frame
from flakefinder.model import AcquisitionError, Frame, Position


def signal():
    rng=np.random.default_rng(41)
    image=np.full((180,240,3),2100,np.float32)
    for _ in range(100):
        cv2.circle(image,tuple(map(int,rng.integers([8,8],[232,172]))),int(rng.integers(2,10)),
                   tuple(map(float,rng.integers(1300,3100,3))),-1)
    return np.uint16(np.rint(cv2.GaussianBlur(image,(0,0),.6)))


def frame(rgb,index=0):
    return Frame(rgb,Position(100,200,300),(.175,.175),index,1000+index,
        {'source':'ZEN API raw stream','experiment_id':f'experiment-{index}',
         'white_level':4095,'objective_position':2,'camera_resources':{'ff_exposure_ms':200.,'ff_white_balance':'1,2,3'}})


class Camera:
    def __init__(self,frames):self.frames=iter(frames);self.calls=0
    def capture(self):self.calls+=1;return next(self.frames)


def test_stationary_average_reduces_temporal_noise_and_preserves_originals(tmp_path):
    truth=signal();rng=np.random.default_rng(7)
    frames=[frame(np.uint16(np.rint(truth+rng.normal(0,24,truth.shape))),i) for i in range(8)]
    # Small encoder fluctuations are distinct from resolvable image motion.
    frames[1].position=Position(100.01,200,300.08)
    camera=Camera(frames);result=capture_average(camera,8,tmp_path,'tile_test')
    assert camera.calls==8
    assert np.std(result.rgb.astype(float)-truth)<.42*np.std(frames[0].rgb.astype(float)-truth)
    expected=np.rint(np.mean([f.rgb.astype(np.float64) for f in frames],axis=0)).astype(np.uint16)
    assert np.array_equal(result.rgb,expected)
    assert result.rgb.dtype==np.uint16 and result.rgb.max()<4095
    assert result.pixel_size_um==(.175,.175)
    assert result.metadata['source']=='ZEN API raw stream'
    assert result.metadata['camera_resources']==frames[0].metadata['camera_resources']
    processing=result.metadata['acquisition_processing']
    assert processing['frame_count']==8 and processing['warped'] is False
    assert processing['measured_max_position_delta_um']==pytest.approx([.01,0,.08])
    assert processing['measured_max_translation_px']<.5
    for original,member in zip(frames,processing['members']):
        saved=load_frame(tmp_path/member['image'])
        assert np.array_equal(saved.rgb,original.rgb)
        assert saved.metadata==original.metadata and saved.position==original.position
    assert 'acquisition_processing' not in frames[0].metadata


def test_drifting_images_are_rejected_without_warping_or_discarding_raw_frames(tmp_path):
    first=frame(signal())
    shifted=cv2.warpAffine(first.rgb,np.float32([[1,0,1.25],[0,1,0]]),(240,180),borderMode=cv2.BORDER_REFLECT)
    with pytest.raises(AcquisitionError,match='Image drift'):
        capture_average(Camera([first,frame(shifted,1)]),2,tmp_path,'drift')
    burst=next((tmp_path/'drift-frames').glob('attempt-*/burst.json'))
    data=json.loads(burst.read_text())
    assert data['status']=='failed' and len(data['members'])==2
    assert np.linalg.norm(data['members'][-1]['translation_px'])>.5
    assert len(list(burst.parent.glob('*.tiff')))==2


@pytest.mark.parametrize('change,message',[
    ('camera','context'),('objective','context'),('shape','shape'),('dtype','dtype'),('scale','scale'),('position','XYZ')])
def test_changed_camera_or_position_context_cannot_be_averaged(tmp_path,change,message):
    first=frame(signal());second=frame(first.rgb.copy(),1)
    if change=='camera':second.metadata['camera_resources']['ff_exposure_ms']=100.
    if change=='objective':second.metadata['objective_position']=3
    if change=='shape':second.rgb=second.rgb[:-1]
    if change=='dtype':second.rgb=np.uint8(second.rgb//16)
    if change=='scale':second.pixel_size_um=(.18,.18)
    if change=='position':second.position=Position(100.16,200,300)
    with pytest.raises(AcquisitionError,match=message):capture_average(Camera([first,second]),2,tmp_path,change)
    assert len(list((tmp_path/(change+'-frames')).glob('attempt-*/*.tiff')))==2


def test_single_capture_is_unchanged_and_retry_never_overwrites_burst_members(tmp_path):
    first=frame(signal());camera=Camera([first])
    assert capture_average(camera,1,tmp_path/'single','tile') is first
    assert camera.calls==1 and not (tmp_path/'single').exists()
    for _ in range(2):capture_average(Camera([first,frame(first.rgb.copy(),1)]),2,tmp_path,'retry')
    assert len(list((tmp_path/'retry-frames').glob('attempt-*')))==2
    assert len(list((tmp_path/'retry-frames').glob('attempt-*/*.tiff')))==4


def test_textureless_burst_is_not_averaged_without_verifiable_registration(tmp_path):
    blank=frame(np.full((120,180,3),2000,np.uint16))
    with pytest.raises(AcquisitionError):capture_average(Camera([blank,deepcopy(blank)]),2,tmp_path,'blank')
    data=json.loads(next((tmp_path/'blank-frames').glob('attempt-*/burst.json')).read_text())
    assert data['status']=='failed'
