import numpy as np
from flakefinder.thin_contrast import thin_contrast_evidence

def test_expected_thin_direction_distinguishes_neutral_haze_and_bright_controls():
    covariance=np.eye(3)*.003**2
    assert thin_contrast_evidence([.04,.07,-.01],covariance)['compatible']
    for contrast in ([.07,.03,.05],[.07,.07,.07],[-.7,-.6,-.8],[.002,.004,0]):
        assert not thin_contrast_evidence(contrast,covariance)['compatible']

def test_rg_covariance_controls_chromatic_direction_uncertainty():
    covariance=np.array([[.02**2,.019**2,0],[.019**2,.02**2,0],[0,0,.01**2]])
    correlated=thin_contrast_evidence([.1,.15,.02],covariance)
    independent=thin_contrast_evidence([.1,.15,.02],np.diag(np.diag(covariance)))
    assert correlated['compatible'] and not independent['compatible']
    assert correlated['layer_number'] is None
