from pathlib import Path
import numpy as np
import pytest

from flakefinder.mote2_optics import (uniaxial_reflectance, load_optical_model,
    score_contrast, make_optical_provider, fit_effective_response_basis,
    conditional_ensemble, score_ensemble, make_pixel_classifier)

CONFIG = Path(__file__).resolve().parents[1] / 'configs/materials/mote2-optics-285nm.json'


def test_interface_normal_and_angular_fresnel_limits():
    wave = np.array([500., 600.]); eps = np.array([[1, 1], [2.25, 2.25]], complex)
    assert np.allclose(uniaxial_reflectance(wave, eps, eps, [0, 0]), .04)
    # Compare filled-pupil quadrature to independent scalar Fresnel equations.
    nodes, weight = np.polynomial.legendre.leggauss(24)
    sine = np.sqrt((nodes + 1) * .4 ** 2 / 2)
    c0 = np.sqrt(1 - sine ** 2); c1 = np.sqrt(1 - (sine / 1.5) ** 2)
    rs = ((c0 - 1.5 * c1) / (c0 + 1.5 * c1)) ** 2
    rp = ((1.5 * c0 - c1) / (1.5 * c0 + c1)) ** 2
    reference = np.sum(weight * (rs + rp) / 4)
    assert np.allclose(uniaxial_reflectance(wave, eps, eps, [0, 0], illumination_na=.4, angle_samples=24), reference, atol=1e-12)


def test_zero_thickness_passive_anisotropic_and_explicit_response():
    model = load_optical_model(CONFIG)
    spectral = model.forward([0., .7, 10.], illumination_na=.4)
    assert 'contrast_rgb' not in spectral
    with pytest.raises(ValueError, match='response'):
        make_optical_provider(spectral)
    curve = model.forward([0., .7, 10.], illumination_na=.4, illustrative=True)
    assert np.allclose(curve['contrast_rgb'][0], 0, atol=1e-12)
    assert np.all((np.array(curve['reflectance']) >= 0) & (np.array(curve['reflectance']) <= 1))
    assert curve['calibration_status'] == 'illustrative_uncalibrated'
    assert not curve['layer_assignment_allowed']
    wave = np.array([450., 550., 650.]); parallel = np.array([[1]*3,[8+2j]*3,[3]*3]); perpendicular=parallel.copy();perpendicular[1]=3+.1j
    a=uniaxial_reflectance(wave,parallel,parallel,[0,30,0]);b=uniaxial_reflectance(wave,parallel,perpendicular,[0,30,0])
    assert np.allclose(a,b)
    a=uniaxial_reflectance(wave,parallel,parallel,[0,30,0],illumination_na=.4);b=uniaxial_reflectance(wave,parallel,perpendicular,[0,30,0],illumination_na=.4)
    assert np.max(np.abs(a-b)) > 1e-4


def test_rgb_integration_sign_gain_cancellation_and_no_extrapolation():
    model=load_optical_model(CONFIG);wave=np.array([450.,550.,650.])
    response={'kind':'measured_effective_response','source':'test fixture, not physical calibration','radiometry_status':'test linear','wavelengths_nm':wave.tolist(),'effective_weight_rgb':np.eye(3).tolist()}
    a=model.forward([0,1,5],wavelengths_nm=wave,response=response)
    expected=1-np.array(a['reflectance'])/np.array(a['substrate_reflectance'])
    assert np.allclose(a['contrast_rgb'],expected)
    response['effective_weight_rgb']=(np.eye(3)*[.2,1,3]).tolist()
    assert np.allclose(model.forward([0,1,5],wavelengths_nm=wave,response=response)['contrast_rgb'],a['contrast_rgb'])
    with pytest.raises(ValueError,match='wavelength'):
        model.forward([0,1],wavelengths_nm=[290,600])
    with pytest.raises(ValueError,match='NA'):
        model.forward([0,1],illumination_na=.5)
    with pytest.raises(ValueError,match='cover'):
        model.forward([0,1],wavelengths_nm=[400,700],response=response)


def test_conditional_noise_intervals_multibranch_and_vectorized_provider():
    curve={'thickness_nm':[0,1,2,3,4], 'contrast_rgb':[[0,0,0],[.1,.2,.3],[.3,.4,.5],[.1,.2,.3],[.5,.6,.7]],'calibration_status':'illustrative_uncalibrated','caveats':[]}
    points=np.array([[[.1,.2,.3],[.3,.4,.5]],[[0,0,0],[.5,.6,.7]]])
    out=make_optical_provider(curve,chunk_size=2)(points,np.eye(3)*1e-5)
    assert out['best_conditional_thickness_nm'].shape==(2,2)
    assert out['ambiguous_thickness'][0,0]
    assert np.allclose(out['conditional_interval_nm'][0,0],[1,3])
    narrow=score_contrast([.3,.4,.5],curve,np.eye(3)*1e-5)
    broad=score_contrast([.3,.4,.5],curve,np.eye(3)*.1)
    assert np.ptp(broad['conditional_interval_nm']) > np.ptp(narrow['conditional_interval_nm'])
    assert not out['layer_assignment_allowed'] and not out['material_identity_confirmed']
    assert np.allclose(out['min_mahalanobis_distance'],0)
    with pytest.raises(ValueError,match='positive definite'):
        score_contrast(points,curve,np.zeros((3,3)))


def test_response_fit_independent_holdout_and_unidentifiable_rejection():
    w=np.array([450.,550.,650.,750.]);sub=np.full(4,.3)
    spectra=np.array([[.2,.3,.4,.5],[.4,.2,.5,.1],[.25,.35,.15,.2],[.1,.2,.25,.45]])
    basis=np.array([[1,0],[1,0],[0,1],[0,1]],float)
    weights=basis@np.array([[.2,.4,.6],[.8,.6,.4]])
    obs=1-np.trapezoid(spectra[:,:,None]*weights[None,:,:],w,axis=1)/np.trapezoid(sub[:,None]*weights,w,axis=0)
    kwargs={'training_indices':[0,1,2],'validation_indices':[3],'reference_records':[{'source':'synthetic independent spectrum'}]*4,'radiometry_status':'synthetic linear'}
    fit=fit_effective_response_basis(w,sub,spectra,obs,basis,**kwargs)
    assert np.max(np.abs(fit['validation_residual_rgb'])) < 1e-6
    assert not fit['calibrated_camera_qe']
    with pytest.raises(ValueError,match='identify'):
        fit_effective_response_basis(w,sub,np.full_like(spectra,.2),obs,basis,**kwargs)


def test_manufacturer_ensemble_kdtree_matches_bruteforce_and_reports_assumptions():
    model=load_optical_model(CONFIG)
    curve=conditional_ensemble(model,[0,.7,1.4,2.1],CONFIG.parent/'data/Axiocam305_vendor_response.json',
        oxide_nm=[285],illumination_na=[0,.4],gamma=[.99,1.])
    expected=np.asarray(curve['contrast_rgb']); points=expected[[1,3],[1,2]]+[.001,-.002,.003]
    covariance=np.array([[.0002,.00005,0],[.00005,.0003,0],[0,0,.0001]])
    out=score_ensemble(points,curve,covariance,chunk_size=1)
    diff=points[:,None,:]-expected.reshape(-1,3)[None,:,:]
    brute=np.einsum('nic,cd,nid->ni',diff,np.linalg.inv(covariance),diff).min(axis=1)
    assert np.allclose(out['min_mahalanobis_distance']**2,brute)
    varied=score_ensemble(points,curve,np.repeat(covariance[None,:,:],2,axis=0),chunk_size=1)
    assert np.allclose(varied['min_mahalanobis_distance'],out['min_mahalanobis_distance'])
    assert np.allclose(varied['conditional_interval_nm'],out['conditional_interval_nm'])
    assert out['calibration_status']=='conditional_manufacturer_response_ensemble'
    assert curve['vendor_trace_uncertainty']['plot_half_stroke_uncertainty_response']>.003
    assert not out['layer_assignment_allowed']
    assert out['nuisance_scenarios_profiled']>1


def test_fast_pixels_match_exact_distance_preserve_unmatched_and_substrate():
    curve={'thickness_nm':[0,1,2],'contrast_rgb':[[[0,0,0],[.1,.2,.3],[.3,.4,.5]],[[0,0,0],[.12,.18,.28],[.32,.38,.48]]],
        'calibration_status':'conditional_test','caveats':[]}
    pixels=np.array([[[0,0,0],[.1,.2,.3]],[[.12,.18,.28],[.9,.9,.9]]],np.float32)
    covariance=np.eye(3)*1e-4;fast=make_pixel_classifier(curve,chunk_size=2)
    result=fast(pixels,covariance,max_mahalanobis=5)
    slow=score_ensemble(pixels,curve,covariance)
    assert np.allclose(result['min_mahalanobis_distance'],slow['min_mahalanobis_distance'],atol=1e-5)
    assert result['conditional_thickness_index'][1,1]==-1
    assert result['out_of_model'][1,1]
    assert result['log_likelihood_improvement_vs_substrate'][0,0]==0
    assert result['log_likelihood_improvement_vs_substrate'][0,1]>1
    assert not result['thickness_uncertainty_assessed']
    assert np.array_equal(fast(pixels,covariance)['best_conditional_thickness_nm'],slow['best_conditional_thickness_nm'])
