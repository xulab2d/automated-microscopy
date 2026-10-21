"""Conditional MoTe2 thin-film optics, independent of acquisition and segmentation.

The shipped dielectric tensor was measured on thick 2H crystals. It does NOT
calibrate monolayer optical constants, camera spectra, or material identity.
RGB curves require supplied spectral weights or an explicitly illustrative mode.
Lengths are nm, epsilon is dimensionless, and contrast is (substrate-flake)/substrate.
"""
from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
from pathlib import Path

import numpy as np

_IMPLEMENTATION_SHA256 = hashlib.sha256(Path(__file__).read_bytes()).hexdigest()


def _finite(value, name):
    a = np.asarray(value, dtype=float)
    if not np.all(np.isfinite(a)):
        raise ValueError(f'{name} must be finite')
    return a


def _passive_sqrt(z):
    q = np.sqrt(np.asarray(z, dtype=complex))
    return np.where((q.imag < 0) | ((q.imag == 0) & (q.real < 0)), -q, q)


def uniaxial_reflectance(wavelengths_nm, epsilon_parallel, epsilon_perpendicular,
                        thickness_nm, *, illumination_na=0., angle_samples=12):
    """Air-incident, incoherent pupil average of coherent stratified-film s/p R.

    epsilon arrays: (layers,wavelengths), optic axis normal to interfaces.
    thickness: (layers,) or (stacks,layers); first/last media semi-infinite.
    A uniformly filled circular illumination pupil is assumed for nonzero NA.
    This is a local plane-wave intensity model, not a coherent image/edge model.
    """
    wave = _finite(wavelengths_nm, 'wavelengths_nm')
    ep = np.asarray(epsilon_parallel, complex)
    ez = np.asarray(epsilon_perpendicular, complex)
    d = _finite(thickness_nm, 'thickness_nm')
    if wave.ndim != 1 or len(wave) < 2 or np.any(wave <= 0) or np.any(np.diff(wave) <= 0):
        raise ValueError('wavelengths must be positive and strictly increasing')
    if ep.shape != ez.shape or ep.ndim != 2 or ep.shape[1] != len(wave) or ep.shape[0] < 2:
        raise ValueError('epsilon arrays require matching (layers,wavelengths) shapes')
    if not np.all(np.isfinite(ep)) or not np.all(np.isfinite(ez)) or np.any(ep.imag < -1e-12) or np.any(ez.imag < -1e-12):
        raise ValueError('finite passive epsilon required with exp(-i omega t) convention')
    if not np.allclose(ep[0], 1) or not np.allclose(ez[0], 1):
        raise ValueError('incident medium must be air')
    if d.ndim == 1:
        d = d[None, :]
    if d.ndim != 2 or d.shape[1] != ep.shape[0] or np.any(d < 0):
        raise ValueError('nonnegative thickness shape must match layer count')
    if not np.isfinite(illumination_na) or not 0 <= illumination_na < 1 or not 1 <= int(angle_samples) <= 128:
        raise ValueError('NA must be in [0,1); angle_samples in [1,128]')
    if illumination_na == 0:
        u, weights = np.array([0.]), np.array([1.])
    else:
        nodes, weights = np.polynomial.legendre.leggauss(int(angle_samples))
        # Uniform pupil area: u=sin(theta)^2 is uniform on [0,NA^2].
        u, weights = (nodes + 1) * illumination_na ** 2 / 2, weights / 2
    result = []
    for pol in ('s', 'p'):
        kz = _passive_sqrt(ep[:, None, :] - u[None, :, None]) if pol == 's' else _passive_sqrt(ep[:, None, :] * (1 - u[None, :, None] / ez[:, None, :]))
        admittance = kz if pol == 's' else ep[:, None, :] / kz
        interfaces = (admittance[:-1] - admittance[1:]) / (admittance[:-1] + admittance[1:])
        reflected = np.broadcast_to(interfaces[-1], (len(d), len(u), len(wave))).copy()
        for j in range(len(ep) - 3, -1, -1):
            phase = np.exp(4j * np.pi * kz[j + 1][None, :, :] * d[:, j + 1, None, None] / wave[None, None, :])
            reflected = (interfaces[j] + reflected * phase) / (1 + interfaces[j] * reflected * phase)
        result.append(np.einsum('a,taw->tw', weights, np.abs(reflected) ** 2))
    return (result[0] + result[1]) / 2


def silica_epsilon(wavelengths_nm):
    """Malitson fused silica at20C; an approximation to wet thermal oxide."""
    w = _finite(wavelengths_nm, 'wavelengths_nm') / 1000
    if np.any((w < .21) | (w > 3.71)):
        raise ValueError('outside original Malitson fitted range')
    result = np.ones_like(w)
    for b, c in ((.6961663, .0684043), (.4079426, .1162414), (.8974794, 9.896161)):
        result += b * w * w / (w * w - c * c)
    return result.astype(complex)


@dataclass
class OpticalModel:
    config: dict
    tensor: np.ndarray
    silicon_nk: np.ndarray

    def forward(self, thickness_nm, *, wavelengths_nm=None, oxide_nm=None,
                illumination_na=0., angle_samples=12, response=None, illustrative=False):
        thickness = np.atleast_1d(_finite(thickness_nm, 'thickness_nm'))
        if thickness.ndim != 1 or not len(thickness) or np.any(thickness < 0) or np.any(np.diff(thickness) <= 0):
            raise ValueError('thickness_nm must be a nonnegative increasing grid')
        wave = np.asarray(np.arange(400., 801., 2.) if wavelengths_nm is None else wavelengths_nm, float)
        low, high = self.config['valid_wavelength_nm']
        if wave.ndim != 1 or len(wave) < 2 or not np.all(np.isfinite(wave)) or np.any(np.diff(wave) <= 0) or wave[0] < low or wave[-1] > high:
            raise ValueError(f'wavelengths must be increasing inside [{low},{high}] nm')
        if illumination_na > self.config['objective_na']:
            raise ValueError('illumination NA exceeds objective acceptance NA')
        oxide = self.config['substrate']['nominal_oxide_nm'] if oxide_nm is None else float(oxide_nm)
        if not np.isfinite(oxide) or oxide <= 0:
            raise ValueError('oxide_nm must be positive')
        interp = lambda column: np.interp(wave, self.tensor[:, 0], self.tensor[:, column])
        ep = interp(1) + 1j * interp(2)
        ez = interp(5) + 1j * interp(6)
        si = np.interp(wave, self.silicon_nk[:, 0], self.silicon_nk[:, 1]) + 1j * np.interp(wave, self.silicon_nk[:, 0], self.silicon_nk[:, 2])
        eps = np.stack([np.ones_like(ep), ep, silica_epsilon(wave), si ** 2])
        epsz = eps.copy(); epsz[1] = ez
        depths = np.column_stack([np.zeros_like(thickness), thickness, np.full_like(thickness, oxide), np.zeros_like(thickness)])
        reflectance = uniaxial_reflectance(wave, eps, epsz, depths, illumination_na=illumination_na, angle_samples=angle_samples)
        background = uniaxial_reflectance(wave, eps, epsz, [0, 0, oxide, 0], illumination_na=illumination_na, angle_samples=angle_samples)[0]
        result = {
            'schema': 1, 'model_id': self.config['model_id'], 'contrast_convention': '(substrate-flake)/substrate',
            'model_provenance': {
                'config_sha256': hashlib.sha256(json.dumps(self.config, sort_keys=True, allow_nan=False).encode()).hexdigest(),
                'data_sha256': {k: v['sha256'] for k, v in self.config['data'].items()},
                'implementation_sha256': _IMPLEMENTATION_SHA256,
            },
            'wavelengths_nm': wave.tolist(), 'thickness_nm': thickness.tolist(),
            'reflectance': reflectance.tolist(), 'substrate_reflectance': background.tolist(),
            'oxide_nm': oxide, 'illumination_na': float(illumination_na),
            'angular_assumption': 'normal incidence' if illumination_na == 0 else 'uniform filled illumination pupil, unpolarized',
            'constants_regime': self.config['constants_regime'], 'calibration_status': 'spectral_only_no_rgb_response',
            'layer_assignment_allowed': False, 'material_identity_confirmed': False,
            'caveats': list(self.config['caveats']),
        }
        if response is not None and illustrative:
            raise ValueError('choose supplied response or illustrative response, not both')
        if illustrative:
            # Explicit diagnostic bands; these are NOT the Axiocam response.
            weights = np.stack([(wave >= 590) & (wave <= 720), (wave >= 520) & (wave <= 590), (wave >= 435) & (wave <= 520)], axis=1).astype(float)
            response = {'kind': 'illustrative_top_hat', 'wavelengths_nm': wave.tolist(), 'effective_weight_rgb': weights.tolist(), 'source': 'unmeasured rectangular RGB bands and equal spectral irradiance', 'radiometry_status': 'unknown'}
        if response is None:
            return result
        weights = _response_weights(response, wave)
        denom = np.trapezoid(weights * background[:, None], wave, axis=0)
        if np.any(denom <= 0):
            raise ValueError('each response channel needs nonzero substrate signal')
        channels = np.trapezoid(reflectance[:, :, None] * weights[None, :, :], wave, axis=1)
        result['contrast_rgb'] = (1 - channels / denom).tolist()
        result['response_provenance'] = {k: v for k, v in response.items() if k not in ('wavelengths_nm', 'effective_weight_rgb')}
        result['response_sha256'] = hashlib.sha256(json.dumps(response, sort_keys=True, allow_nan=False).encode()).hexdigest()
        result['calibration_status'] = ('illustrative_uncalibrated' if illustrative else 'conditional_supplied_response')
        result['caveats'].append('RGB response provenance is supplied by caller; neither material identity nor thin-layer optical response is validated by this model.')
        return result


def _response_weights(response, wave):
    x = _finite(response['wavelengths_nm'], 'response wavelengths')
    y = _finite(response['effective_weight_rgb'], 'response weights')
    if not response.get('source') or not response.get('kind') or not response.get('radiometry_status'):
        raise ValueError('response requires kind, source and radiometry_status provenance')
    if x.ndim != 1 or len(x) < 2 or np.any(np.diff(x) <= 0) or y.shape != (len(x), 3) or np.any(y < 0):
        raise ValueError('response requires increasing wavelengths and nonnegative RGB weights')
    if x[0] > wave[0] or x[-1] < wave[-1]:
        raise ValueError('response must cover all model wavelengths; no silent extrapolation')
    return np.column_stack([np.interp(wave, x, y[:, c]) for c in range(3)])


def load_optical_model(config_path):
    path = Path(config_path).resolve()
    config = json.loads(path.read_text(encoding='utf-8'))
    tables = []
    for key in ('mote2_tensor', 'silicon_nk'):
        item = config['data'][key]
        file = (path.parent / item['path']).resolve()
        if hashlib.sha256(file.read_bytes()).hexdigest() != item['sha256']:
            raise ValueError(f'optical dataset hash mismatch: {key}')
        tables.append(np.loadtxt(file))
    tensor, silicon = tables
    if tensor.ndim != 2 or tensor.shape[1] != 7 or silicon.ndim != 2 or silicon.shape[1] != 3:
        raise ValueError('invalid optical dataset columns')
    for table in tables:
        if not np.all(np.isfinite(table)) or np.any(np.diff(table[:, 0]) <= 0):
            raise ValueError('optical tables must be finite with increasing wavelengths')
    low, high = config['valid_wavelength_nm']
    if low >= high or any(low < table[0, 0] or high > table[-1, 0] for table in tables):
        raise ValueError('configured wavelength interval exceeds data support')
    if not np.allclose(tensor[:, 1:3], tensor[:, 3:5]):
        raise ValueError('this solver requires in-plane isotropy')
    return OpticalModel(config, tensor, silicon)


def score_contrast(contrast_rgb, curve, noise_covariance, *, model_covariance=None,
                   delta_chi2=3.841458820694124, chunk_size=8192):
    """Descriptive curve distance and likelihood-ratio interval; not layer labels.

    Covariance is in squared fractional-contrast units (RGB order), full3x3
    or per observation. Model covariance must be independently specified;
    absent covariance means model uncertainty is NOT quantified, not zero in nature.
    Intervals may bridge disjoint branches, marked by ambiguous_thickness.
    """
    if 'contrast_rgb' not in curve:
        raise ValueError('RGB response absent; optical evidence is unavailable')
    observed = _finite(contrast_rgb, 'contrast_rgb')
    if observed.ndim < 1 or observed.shape[-1] != 3:
        raise ValueError('contrast_rgb must end in three RGB channels')
    shape = observed.shape[:-1]; flat = observed.reshape(-1, 3)
    expected = _finite(curve['contrast_rgb'], 'curve RGB')
    thickness = _finite(curve['thickness_nm'], 'curve thickness')
    if expected.shape != (len(thickness), 3) or len(thickness) < 2 or np.any(np.diff(thickness) <= 0):
        raise ValueError('curve requires at least two strictly increasing thicknesses')
    if not np.isfinite(delta_chi2) or delta_chi2 <= 0 or int(chunk_size) < 1:
        raise ValueError('positive delta_chi2 and chunk_size required')
    cov = _finite(noise_covariance, 'noise covariance')
    if cov.shape not in ((3, 3), shape + (3, 3)):
        raise ValueError('covariance must be3x3 or match observation shape')
    cov = cov.reshape((-1, 3, 3))
    extra = np.zeros((3, 3)) if model_covariance is None else _finite(model_covariance, 'model covariance')
    if extra.shape != (3, 3) or not np.allclose(extra, extra.T) or np.min(np.linalg.eigvalsh(extra)) < -1e-12:
        raise ValueError('model covariance must be symmetric positive semidefinite3x3')
    if not np.allclose(cov, cov.swapaxes(-1, -2)):
        raise ValueError('noise covariance must be symmetric')
    try:
        np.linalg.cholesky(cov + extra)
    except np.linalg.LinAlgError as exc:
        raise ValueError('total covariance must be positive definite; no invented noise floor is added') from exc
    best = np.empty(len(flat)); lower = best.copy(); upper = best.copy(); distance = best.copy(); substrate_distance = best.copy()
    ambiguous = np.zeros(len(flat), bool); boundary = ambiguous.copy()
    for start in range(0, len(flat), int(chunk_size)):
        end = min(len(flat), start + int(chunk_size))
        inv = np.linalg.inv((cov if len(cov) == 1 else cov[start:end]) + extra)
        residual = flat[start:end, None, :] - expected[None, :, :]
        d2 = np.einsum('ntc,ncd,ntd->nt', residual, inv, residual)
        indexes = d2.argmin(axis=1); minimum = d2[np.arange(len(indexes)), indexes]
        accepted = d2 <= minimum[:, None] + delta_chi2
        best[start:end] = thickness[indexes]
        lower[start:end] = thickness[accepted.argmax(axis=1)]
        upper[start:end] = thickness[len(thickness) - 1 - accepted[:, ::-1].argmax(axis=1)]
        distance[start:end] = np.sqrt(np.maximum(minimum, 0))
        substrate_distance[start:end] = np.sqrt(np.maximum(np.einsum('nc,ncd,nd->n', flat[start:end], inv, flat[start:end]), 0))
        starts = accepted[:, 0].astype(int) + ((~accepted[:, :-1]) & accepted[:, 1:]).sum(axis=1)
        ambiguous[start:end] = starts > 1
        boundary[start:end] = accepted[:, 0] | accepted[:, -1]
    return {
        'best_conditional_thickness_nm': best.reshape(shape),
        'conditional_interval_nm': np.stack([lower, upper], axis=-1).reshape(shape + (2,)),
        'min_mahalanobis_distance': distance.reshape(shape),
        'substrate_mahalanobis_distance': substrate_distance.reshape(shape),
        'log_likelihood_improvement_vs_substrate': (.5 * (substrate_distance ** 2 - distance ** 2)).reshape(shape),
        'model_match_score': np.exp(-.5 * distance ** 2).reshape(shape),
        'ambiguous_thickness': ambiguous.reshape(shape), 'interval_reaches_grid_boundary': boundary.reshape(shape),
        'calibration_status': curve['calibration_status'], 'layer_assignment_allowed': False,
        'material_identity_confirmed': False, 'model_uncertainty_included': model_covariance is not None,
        'delta_chi2': float(delta_chi2),
        'caveats': curve['caveats'] + ['Score is not a probability. Conditional intervals use supplied covariance only and are not validated physical-thickness confidence intervals.'],
    }


def make_optical_provider(curve, *, model_covariance=None, chunk_size=8192):
    """Hook for terrace_analysis; accepts contrast RGB and covariance RGB."""
    if 'contrast_rgb' not in curve:
        raise ValueError('Cannot create RGB provider without an explicit spectral response')
    def provider(contrast_rgb, covariance_rgb):
        scorer = score_ensemble if np.asarray(curve['contrast_rgb']).ndim == 3 else score_contrast
        return scorer(contrast_rgb, curve, covariance_rgb,
                              model_covariance=model_covariance, chunk_size=chunk_size)
    provider.provenance = {
        'schema': 1, 'model_id': curve.get('model_id'),
        'model': curve.get('model_provenance', {}),
        'curve_sha256': hashlib.sha256(json.dumps(curve, sort_keys=True, allow_nan=False).encode()).hexdigest(),
        'calibration_status': curve['calibration_status'],
        'response_sha256': curve.get('response_sha256', curve.get('vendor_response_sha256')),
        'response_provenance': curve.get('response_provenance'),
        'scenario_provenance': curve.get('scenario_provenance'),
        'vendor_trace_uncertainty': curve.get('vendor_trace_uncertainty'),
        'oxide_nm': curve.get('oxide_nm'), 'illumination_na': curve.get('illumination_na'),
        'thickness_grid_nm': curve['thickness_nm'],
        'model_covariance_rgb': None if model_covariance is None else np.asarray(model_covariance).tolist(),
        'score_kind': 'profile_minimum_over_nuisance' if np.asarray(curve['contrast_rgb']).ndim == 3 else 'single_conditional_curve',
        'layer_assignment_allowed': False, 'material_identity_confirmed': False,
    }
    provider.classify_pixels = make_pixel_classifier(curve, model_covariance=model_covariance)
    return provider


def make_pixel_classifier(curve, *, model_covariance=None, chunk_size=65536):
    """Cached nearest-curve labeling, before connected-component terrace grouping.

    Pixel bins are the supplied nm grid, NEVER integer layer identities. Shared
    covariance is required; this fast path does not evaluate thickness intervals
    or global ambiguity. Unmatched pixels retain index=-1 for downstream geometry
    extraction, never a declaration of absence. At most four covariance trees
    are cached. Full uncertainty belongs to the per-terrace provider.
    """
    from scipy.spatial import cKDTree
    expected=_finite(curve['contrast_rgb'],'curve RGB')
    t=_finite(curve['thickness_nm'],'curve thickness')
    if expected.ndim==2: expected=expected[None,:,:]
    if expected.ndim!=3 or expected.shape[1:]!=(len(t),3) or len(t)<2 or np.any(np.diff(t)<=0) or int(chunk_size)<1:
        raise ValueError('invalid conditional curve grid or chunk size')
    template=expected.reshape(-1,3)
    extra=np.zeros((3,3)) if model_covariance is None else _finite(model_covariance,'model covariance')
    if extra.shape!=(3,3) or not np.allclose(extra,extra.T) or np.min(np.linalg.eigvalsh(extra)) < -1e-12:
        raise ValueError('model covariance must be positive semidefinite3x3')
    cache={}
    def classify(contrast_rgb,covariance_rgb,*,max_mahalanobis=None):
        points=np.asarray(contrast_rgb)
        if points.ndim<1 or points.shape[-1]!=3 or not np.issubdtype(points.dtype,np.number) or not np.all(np.isfinite(points)):
            raise ValueError('finite RGB contrast required')
        cov=_finite(covariance_rgb,'covariance')
        if cov.shape!=(3,3) or not np.allclose(cov,cov.T):
            raise ValueError('fast pixel classifier requires shared symmetric3x3 covariance')
        if max_mahalanobis is not None and (not np.isfinite(max_mahalanobis) or max_mahalanobis<=0):
            raise ValueError('max_mahalanobis must be positive when supplied')
        key=cov.tobytes()
        if key not in cache:
            try: whitening=np.linalg.inv(np.linalg.cholesky(cov+extra))
            except np.linalg.LinAlgError as exc: raise ValueError('positive definite covariance required') from exc
            if len(cache)>=4:cache.pop(next(iter(cache)))
            cache[key]=(whitening,cKDTree(template@whitening.T))
        whitening,tree=cache[key]
        shape=points.shape[:-1]; flat=points.reshape(-1,3); n=len(flat)
        bins=np.empty(n,np.int32); scenario=np.empty(n,np.int32)
        distances=np.empty(n,np.float32);background=np.empty(n,np.float32)
        for start in range(0,n,int(chunk_size)):
            stop=min(n,start+int(chunk_size));white=flat[start:stop]@whitening.T
            d,i=tree.query(white,workers=1)
            bins[start:stop]=i%len(t);scenario[start:stop]=i//len(t)
            distances[start:stop]=d;background[start:stop]=np.linalg.norm(white,axis=1)
        matched=np.ones(n,bool) if max_mahalanobis is None else distances<=max_mahalanobis
        best=t[bins].astype(np.float32);bins[~matched]=-1
        return {'conditional_thickness_index':bins.reshape(shape),'best_conditional_thickness_nm':best.reshape(shape),
            'min_mahalanobis_distance':distances.reshape(shape),'best_nuisance_scenario_index':scenario.reshape(shape),
            'substrate_mahalanobis_distance':background.reshape(shape),
            'log_likelihood_improvement_vs_substrate':(.5*(background**2-distances**2)).reshape(shape),
            'model_match_score':np.exp(-.5*distances**2).reshape(shape),
            'out_of_model':(~matched).reshape(shape),'match_threshold_supplied':max_mahalanobis is not None,
            'thickness_uncertainty_assessed':False,'ambiguity_status':'not evaluated per pixel; evaluate full curve/ensemble for terrace statistics',
            'calibration_status':curve['calibration_status'],'layer_assignment_allowed':False,'material_identity_confirmed':False,
            'bin_definition':'index of supplied physical_nm simulation grid, not a calibrated thickness class',
            'caveats':curve['caveats']}
    return classify


def score_ensemble_pixels(contrast_rgb, ensemble, covariance_rgb, *, model_covariance=None,
                          max_mahalanobis=None, chunk_size=65536):
    """One-shot fast labeling; reuse make_pixel_classifier to retain tree cache."""
    return make_pixel_classifier(ensemble,model_covariance=model_covariance,chunk_size=chunk_size)(
        contrast_rgb,covariance_rgb,max_mahalanobis=max_mahalanobis)


def conditional_ensemble(model, thickness_nm, vendor_response_path, *,
                         illumination_spectra=None, oxide_nm=(280., 285., 290.),
                         illumination_na=(0., .267, .4), gamma=(.99, 1.),
                         response_interpretations=('energy_response', 'photon_efficiency')):
    """Vendor-camera plus explicit nuisance scenarios for conditional screening.

    Default smooth spectra are sensitivity examples, not a measured lamp model
    and not guaranteed to bracket an LED's spectral peaks. Oxide +/-5nm and
    gamma .99..1 are scenario choices, not measured confidence limits. No
    scenario weights/probabilities are fabricated; score_ensemble profiles
    over the finite ensemble for permissive candidate ranking.
    """
    file = Path(vendor_response_path)
    vendor = json.loads(file.read_text(encoding='utf-8'))
    w = np.asarray(vendor['wavelengths_nm'], float)
    q = np.asarray(vendor['relative_sensitivity_rgb'], float)
    if q.shape != (len(w), 3) or np.any(q < 0) or not np.all(np.isfinite(q)):
        raise ValueError('invalid vendor response trace')
    if illumination_spectra is None:
        illumination_spectra = [{'name': 'equal_energy', 'values': np.ones(len(w)), 'source': 'unmeasured smooth sensitivity scenario'}]
        for temperature in (3000., 4500., 6500.):
            energy = w ** -5 / np.expm1(1.438776877e7 / (w * temperature))
            illumination_spectra.append({'name': f'blackbody_{temperature:g}K', 'values': energy / energy.max(), 'source': 'unmeasured Planck radiance per unit wavelength; not asserted to describe lamp'})
    responses = []
    for lamp in illumination_spectra:
        values = _finite(lamp['values'], 'illumination spectrum')
        if values.shape != w.shape or np.any(values < 0) or not np.any(values > 0) or not lamp.get('source'):
            raise ValueError('lamp spectrum requires nonnegative supported values and source')
        for interpretation in response_interpretations:
            if interpretation not in ('energy_response', 'photon_efficiency'):
                raise ValueError('unknown manufacturer response interpretation')
            # If curve is QE per photon, radiant energy converts to photons by lambda/hc.
            correction = 1 if interpretation == 'energy_response' else w / w.mean()
            weight = q * (values * correction)[:, None]
            responses.append({'name': lamp['name'], 'interpretation': interpretation,
                'response': {'kind': 'manufacturer_response_with_explicit_illumination_scenario',
                'source': vendor['source'] + '; ' + lamp['source'], 'radiometry_status': 'near-linear ladder without dark reference; conditional',
                'wavelengths_nm': w.tolist(), 'effective_weight_rgb': weight.tolist()}})
    curves=[]; scenarios=[]
    for oxide in oxide_nm:
        for na in illumination_na:
            spectral = model.forward(thickness_nm, wavelengths_nm=w, oxide_nm=oxide, illumination_na=na)
            reflected = np.asarray(spectral['reflectance']); background=np.asarray(spectral['substrate_reflectance'])
            for entry in responses:
                weight=np.asarray(entry['response']['effective_weight_rgb'])
                ratio=np.trapezoid(reflected[:,:,None]*weight[None,:,:],w,axis=1)/np.trapezoid(background[:,None]*weight,w,axis=0)
                for power in gamma:
                    if not np.isfinite(power) or power <= 0:
                        raise ValueError('gamma scenarios must be positive')
                    curves.append(1-np.maximum(ratio,0)**power)
                    scenarios.append({'oxide_nm':float(oxide),'illumination_na':float(na),'gamma':float(power),'lamp':entry['name'],'response_interpretation':entry['interpretation']})
    if not curves:
        raise ValueError('ensemble needs at least one scenario')
    return {'schema':1,'model_id':model.config['model_id'],'model_provenance':spectral['model_provenance'],'thickness_nm':np.asarray(thickness_nm,float).tolist(),
        'contrast_rgb':np.asarray(curves).tolist(),'scenario_provenance':scenarios,
        'calibration_status':'conditional_manufacturer_response_ensemble','layer_assignment_allowed':False,
        'material_identity_confirmed':False,'vendor_response_sha256':hashlib.sha256(file.read_bytes()).hexdigest(),
        'vendor_trace_uncertainty':{k:vendor[k] for k in ('plot_half_stroke_uncertainty_response','plot_half_stroke_uncertainty_nm','uncertainty_warning')},
        'caveats':model.config['caveats']+['Finite sensitivity scenarios are not a calibrated distribution or guaranteed bound on the unknown lamp/oxide/processing. Manufacturer plot tracing uncertainty is recorded but does not quantify device variation.','Profile likelihood selects the most compatible nuisance scenario; it is deliberately permissive and cannot confirm MoTe2 or thickness.']}


def score_ensemble(contrast_rgb, ensemble, noise_covariance, *, model_covariance=None,
                   delta_chi2=3.841458820694124, chunk_size=8192):
    """Minimum distance over explicit nuisance scenarios, with branch-aware range.

    Uses a whitened KD-tree for shared covariance, avoiding pixel x curve arrays.
    Observation-specific covariance uses bounded chunks. No ensemble prior or
    marginal probability is implied by the optimistic envelope score.
    """
    from scipy.spatial import cKDTree
    points=_finite(contrast_rgb,'contrast_rgb'); expected=_finite(ensemble['contrast_rgb'],'ensemble')
    t=_finite(ensemble['thickness_nm'],'thickness_nm')
    if points.ndim<1 or points.shape[-1]!=3 or expected.ndim!=3 or expected.shape[1:]!=(len(t),3) or len(t)<2 or np.any(np.diff(t)<=0):
        raise ValueError('invalid RGB or ensemble (scenarios,thickness,RGB) shapes')
    if not np.isfinite(delta_chi2) or delta_chi2<=0 or int(chunk_size)<1:
        raise ValueError('positive delta_chi2 and chunk_size required')
    shape=points.shape[:-1]; flat=points.reshape(-1,3); template=expected.reshape(-1,3)
    cov=_finite(noise_covariance,'covariance')
    if cov.shape not in ((3,3),shape+(3,3)):
        raise ValueError('covariance must be3x3 or observation-aligned')
    cov=cov.reshape(-1,3,3)
    extra=np.zeros((3,3)) if model_covariance is None else _finite(model_covariance,'model covariance')
    if extra.shape!=(3,3) or not np.allclose(extra,extra.T) or np.min(np.linalg.eigvalsh(extra)) < -1e-12 or not np.allclose(cov,cov.swapaxes(-1,-2)):
        raise ValueError('symmetric valid covariance required')
    try: cholesky=np.linalg.cholesky(cov+extra)
    except np.linalg.LinAlgError as exc: raise ValueError('positive definite covariance required') from exc
    distance=np.empty(len(flat)); best=np.empty(len(flat)); bounds=np.empty((len(flat),2)); ambiguous=np.zeros(len(flat),bool); boundary=ambiguous.copy(); scenario=np.empty(len(flat),int); substrate_distance=np.empty(len(flat))
    def record(index,d,index_best,candidates):
        indexes=np.unique(np.asarray(candidates,int)%len(t)); chosen=int(index_best)
        distance[index]=d;best[index]=t[chosen%len(t)];scenario[index]=chosen//len(t)
        bounds[index]=[t[indexes[0]],t[indexes[-1]]]
        ambiguous[index]=np.any(np.diff(indexes)>1);boundary[index]=indexes[0]==0 or indexes[-1]==len(t)-1
    if len(cov)==1:
        whitening=np.linalg.inv(cholesky[0]);tree=cKDTree(template@whitening.T)
        for start in range(0,len(flat),int(chunk_size)):
            whitened=flat[start:start+int(chunk_size)]@whitening.T
            substrate_distance[start:start+len(whitened)]=np.linalg.norm(whitened,axis=1)
            ds,ix=tree.query(whitened,workers=1)
            for k,(x,d,i) in enumerate(zip(whitened,ds,ix)):
                neighbors=tree.query_ball_point(x,np.sqrt(d*d+delta_chi2)+1e-12)
                record(start+k,d,i,neighbors)
    else:
        # Limit temporary scalar distances to ~16MB; avoid full image*curve allocation.
        step=min(int(chunk_size),max(1,2_000_000//len(template)))
        for start in range(0,len(flat),step):
            stop=min(len(flat),start+step);inv=np.linalg.inv(cov[start:stop]+extra)
            diff=flat[start:stop,None,:]-template[None,:,:]
            ds=np.einsum('nic,ncd,nid->ni',diff,inv,diff)
            substrate_distance[start:stop]=np.sqrt(np.maximum(np.einsum('nc,ncd,nd->n',flat[start:stop],inv,flat[start:stop]),0))
            for k,row in enumerate(ds):
                i=int(row.argmin());record(start+k,np.sqrt(max(row[i],0)),i,np.flatnonzero(row<=row[i]+delta_chi2))
    return {'best_conditional_thickness_nm':best.reshape(shape),'conditional_interval_nm':bounds.reshape(shape+(2,)),
        'min_mahalanobis_distance':distance.reshape(shape),'model_match_score':np.exp(-.5*distance**2).reshape(shape),
        'substrate_mahalanobis_distance':substrate_distance.reshape(shape),
        'log_likelihood_improvement_vs_substrate':(.5*(substrate_distance**2-distance**2)).reshape(shape),
        'best_nuisance_scenario_index':scenario.reshape(shape),'ambiguous_thickness':ambiguous.reshape(shape),
        'interval_reaches_grid_boundary':boundary.reshape(shape),'calibration_status':ensemble['calibration_status'],
        'layer_assignment_allowed':False,'material_identity_confirmed':False,'model_uncertainty_included':model_covariance is not None,
        'nuisance_scenarios_profiled':len(expected),'score_kind':'minimum_distance_over_finite_sensitivity_ensemble',
        'delta_chi2':float(delta_chi2),'caveats':ensemble['caveats']+['Score is not a probability. Conditional ranges are not validated thickness confidence intervals.']}


def fit_effective_response_basis(wavelengths_nm, substrate_reflectance, sample_reflectance,
                                 observed_contrast_rgb, basis, *, training_indices,
                                 validation_indices, reference_records, radiometry_status):
    """Fit a low-dimensional nonnegative effective response, with held-out checks.

    Input spectra must be independently measured or independently justified;
    fitting this against unverified thicknesses is circular. A fitted basis
    is not a measurement of the camera QE, and no spectral uniqueness is claimed.
    All channels share caller-supplied basis[L,K], but have separate coefficients.
    """
    from scipy.optimize import minimize
    w = _finite(wavelengths_nm, 'wavelengths'); sub = _finite(substrate_reflectance, 'substrate')
    samples = _finite(sample_reflectance, 'samples'); obs = _finite(observed_contrast_rgb, 'contrast')
    b = _finite(basis, 'basis'); train = np.asarray(training_indices, int); valid = np.asarray(validation_indices, int)
    if w.ndim != 1 or len(w) < 2 or np.any(np.diff(w) <= 0) or sub.shape != w.shape or samples.ndim != 2 or samples.shape[1] != len(w) or obs.shape != (len(samples), 3) or b.ndim != 2 or b.shape[0] != len(w) or np.any(b < 0) or np.any(sub < 0) or np.any(samples < 0):
        raise ValueError('incompatible nonnegative spectra/basis or contrast shapes')
    k = b.shape[1]
    if k < 1 or len(train) < k or not len(valid) or len(np.unique(train)) != len(train) or len(np.unique(valid)) != len(valid) or set(train) & set(valid) or np.any(train < 0) or np.any(valid < 0) or np.any(train >= len(samples)) or np.any(valid >= len(samples)):
        raise ValueError('require distinct disjoint training/validation references and at least K training samples')
    if len(reference_records) != len(samples) or any(not r.get('source') for r in reference_records) or not radiometry_status:
        raise ValueError('independent reference and radiometry provenance required')
    normalizer = np.trapezoid(b * sub[:, None], w, axis=0)
    if np.any(normalizer <= 0):
        raise ValueError('each basis needs nonzero substrate response')
    b = b / normalizer
    design = np.trapezoid(samples[:, :, None] * b[None, :, :], w, axis=1)
    rank = int(np.linalg.matrix_rank(np.vstack([design[train], np.ones(k)])))
    if rank < k:
        raise ValueError('reference spectra do not identify the supplied response basis')
    coeff = []
    for channel in range(3):
        target = 1 - obs[train, channel]
        fit = minimize(lambda a: float(np.sum((design[train] @ a - target) ** 2)), np.full(k, 1 / k), method='SLSQP', bounds=[(0, 1)] * k, constraints={'type': 'eq', 'fun': lambda a: a.sum() - 1}, options={'ftol': 1e-14, 'maxiter': 1000})
        if not fit.success:
            raise ValueError(f'response fit failed: {fit.message}')
        coeff.append(fit.x)
    coeff = np.asarray(coeff).T; predicted = 1 - design @ coeff
    return {
        'kind': 'fitted_effective_rgb_weight', 'source': 'nonnegative basis fit to supplied independent references',
        'radiometry_status': radiometry_status, 'wavelengths_nm': w.tolist(),
        'effective_weight_rgb': (b @ coeff).tolist(), 'basis_coefficients_rgb': coeff.tolist(),
        'validation_residual_rgb': (predicted[valid] - obs[valid]).tolist(),
        'training_residual_rgb': (predicted[train] - obs[train]).tolist(),
        'reference_records': reference_records, 'training_indices': train.tolist(), 'validation_indices': valid.tolist(),
        'basis_identifiable_rank': rank, 'calibrated_camera_qe': False,
        'caveat': 'Finite-basis fit only; held-out residuals must be judged against independently measured noise before application.',
    }
