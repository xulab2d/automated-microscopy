import json
from copy import deepcopy

import cv2
import numpy as np
import pytest

from flakefinder.model import Frame,Position
from flakefinder.radiometry import assess_exposure_series


def series(gamma=1.,levels=(25.,50.,100.),metadata=False):
    rng=np.random.default_rng(31)
    field=cv2.GaussianBlur(rng.normal(size=(128,128)).astype(np.float32),(0,0),2)
    field=1+.12*field/max(float(np.std(field)),1e-6)
    frames=[];times=[]
    for t in levels:
        signal=field[:,:,None]*np.array([1500.,1800.,1200.])*t/100
        base=40+4095*np.maximum(signal/4095,0)**gamma
        for _ in range(2):
            # Correlated temporal noise must survive the full RGB covariance.
            noise=rng.normal(0,3,(128,128,1))+rng.normal(0,.7,(128,128,3))
            frames.append(np.clip(base+noise,0,4095));times.append(t)
    darks=[40+rng.normal(0,.7,(128,128,3)) for _ in range(2)]
    if metadata:
        def wrap(a,t,dark=False):
            m={'camera_job_template':'test','camera_resources':{'ff_exposure_ms':t,'ff_white_balance':'1,1,1'},
               'objective_position':2,'white_level':4095,'illumination_percent':0 if dark else 93.}
            return Frame(a,Position(0,0,0),(.1725,.1725),0,metadata=m)
        frames=[wrap(a,t) for a,t in zip(frames,times)];darks=[wrap(a,100.,True) for a in darks]
    return frames,times,darks


@pytest.mark.parametrize('gamma,expected',[(1.,'consistent_with_linear_response'),(.5,'nonlinear_or_insufficient_response')])
def test_detects_response_gamma_and_rgb_correlated_noise(gamma,expected):
    frames,times,darks=series(gamma)
    report=assess_exposure_series(frames,times,darks)
    assert report['status']==expected
    assert all(abs(f['gamma']-gamma)<.025 for f in report['response_rgb'])
    assert report['preferred_setting']['exposure_ms']==100.
    row=report['levels'][-1];cov=np.asarray(row['relative_covariance_rgb'])
    assert cov[0,1]>0
    assert row['rg_relative_noise_sigma']<.6*np.sqrt(cov[0,0]+cov[1,1])
    assert not report['linearity_confirmed'] and not report['emva_certified']
    json.dumps(report,allow_nan=False)


def test_clipped_high_exposure_is_excluded_and_missing_dark_stays_unverified():
    frames,times,darks=series(levels=(25.,50.,100.,400.))
    report=assess_exposure_series(frames,times,darks)
    assert report['preferred_setting']['exposure_ms']==100.
    assert not report['levels'][-1]['eligible_for_preference']
    assert all(f['levels_used']==3 for f in report['response_rgb'])
    assert assess_exposure_series(frames,times)['status']=='indeterminate_without_dark_reference'


def test_profile_exposure_shape_and_translation_changes_are_rejected():
    frames,times,darks=series(metadata=True)
    assert assess_exposure_series(frames,times,darks)['profile_metadata_complete']
    changed=deepcopy(frames);changed[-1].metadata['camera_resources']['ff_white_balance']='2,1,1'
    with pytest.raises(ValueError,match='profile mismatch'):
        assess_exposure_series(changed,times,darks)
    with pytest.raises(ValueError,match='Exposure metadata mismatch'):
        assess_exposure_series(frames,[t*2 for t in times],darks)
    changed=deepcopy(frames);changed[-1].rgb=changed[-1].rgb[:-1]
    with pytest.raises(ValueError,match='shape/range mismatch'):
        assess_exposure_series(changed,times,darks)
    changed=deepcopy(frames);changed[-1].rgb=np.roll(changed[-1].rgb,12,axis=1)
    with pytest.raises(ValueError,match='shift >2 pixels'):
        assess_exposure_series(changed,times,darks)


def test_requires_three_levels_and_complete_pairs():
    frames,times,_=series()
    with pytest.raises(ValueError,match='three distinct'):
        assess_exposure_series(frames[:-1],times[:-1])
