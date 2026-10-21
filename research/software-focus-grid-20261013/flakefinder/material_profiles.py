"""Conditional graphene/hBN optics on SiO2/Si, with explicit RGB response.

No microscope access. The existing passive multilayer solver is shared with
MoTe2; material constants, priors and layer assumptions are NOT shared.
"""
from dataclasses import dataclass
from pathlib import Path
import hashlib
import json
import numpy as np

from .mote2_optics import uniaxial_reflectance, silica_epsilon, _response_weights

CONVENTION = '(substrate-flake)/substrate'


def canonical_hash(value):
    return hashlib.sha256(json.dumps(value,sort_keys=True,allow_nan=False).encode()).hexdigest()


@dataclass(frozen=True)
class MaterialProfile:
    config: dict
    silicon_nk: np.ndarray
    source_path: str

    @property
    def metadata(self):
        return {**self.config, 'profile_sha256':canonical_hash(self.config),
                'layer_assignment_allowed':False, 'chemical_identity_confirmed':False}

    def forward(self, thickness_nm, **kwargs):
        return predict_material_contrast(self, thickness_nm, **kwargs)


def load_material_profile(path):
    path=Path(path).resolve();config=json.loads(path.read_text(encoding='utf-8-sig'))
    if config.get('schema')!=1 or config.get('kind')!='conditional_material_optics':
        raise ValueError('Expected a version1 conditional material optics profile.')
    if config.get('material') not in ('graphene','hBN'):
        raise ValueError('This profile loader supports graphene and hBN.')
    if config.get('contrast_convention')!=CONVENTION:
        raise ValueError('Contrast convention mismatch.')
    item=config['silicon_data'];file=(path.parent/item['path']).resolve()
    if hashlib.sha256(file.read_bytes()).hexdigest()!=item['sha256']:
        raise ValueError('Silicon reference data hash mismatch.')
    silicon=np.loadtxt(file)
    if silicon.ndim!=2 or silicon.shape[1]!=3 or not np.isfinite(silicon).all() or np.any(np.diff(silicon[:,0])<=0):
        raise ValueError('Invalid silicon optical table.')
    n,k=config['optical_constants']['n'],config['optical_constants']['k']
    if not np.isfinite([n,k,config['layer_spacing_nm'],config['substrate']['nominal_oxide_nm']]).all() or n<=0 or k<0 or config['layer_spacing_nm']<=0 or config['substrate']['nominal_oxide_nm']<=0:
        raise ValueError('Invalid material constants or substrate thickness.')
    return MaterialProfile(config,silicon,str(path))


def predict_material_contrast(profile, thickness_nm, *, response=None, illustrative=False,
                              oxide_nm=None, wavelengths_nm=None, illumination_na=0., angle_samples=12):
    """Predict spectra and optionally weighted RGB; thickness is nm, not a label.

    Constant n,k are a published thin-film approximation. For finite NA the
    isotropic extension is only a scenario, especially for anisotropic hBN.
    RGB response must include source/kind/radiometry_status and wavelength weights.
    """
    if not isinstance(profile,MaterialProfile):profile=load_material_profile(profile)
    config=profile.config;t=np.atleast_1d(np.asarray(thickness_nm,float))
    wave=np.asarray(np.arange(400.,801.,2.) if wavelengths_nm is None else wavelengths_nm,float)
    low,high=config['valid_wavelength_nm']
    if t.ndim!=1 or len(t)<1 or not np.isfinite(t).all() or np.any(t<0) or np.any(np.diff(t)<=0):
        raise ValueError('Thickness grid must be finite, nonnegative, strictly increasing.')
    if wave.ndim!=1 or len(wave)<2 or not np.isfinite(wave).all() or np.any(np.diff(wave)<=0) or wave[0]<max(low,profile.silicon_nk[0,0]) or wave[-1]>min(high,profile.silicon_nk[-1,0]):
        raise ValueError('Wavelength grid outside optical data support.')
    oxide=float(config['substrate']['nominal_oxide_nm'] if oxide_nm is None else oxide_nm)
    if not np.isfinite(oxide) or oxide<=0:raise ValueError('Positive oxide thickness required.')
    constants=config['optical_constants'];material=complex(constants['n'],constants['k'])**2
    silicon=np.interp(wave,profile.silicon_nk[:,0],profile.silicon_nk[:,1])+1j*np.interp(wave,profile.silicon_nk[:,0],profile.silicon_nk[:,2])
    eps=np.stack([np.ones_like(wave),np.full_like(wave,material,dtype=complex),silica_epsilon(wave),silicon**2])
    depths=np.column_stack([np.zeros_like(t),t,np.full_like(t,oxide),np.zeros_like(t)])
    reflectance=uniaxial_reflectance(wave,eps,eps,depths,illumination_na=illumination_na,angle_samples=angle_samples)
    substrate=uniaxial_reflectance(wave,eps,eps,[0,0,oxide,0],illumination_na=illumination_na,angle_samples=angle_samples)[0]
    result={'schema':1,'model_id':config['profile_id'],'material':config['material'],
        'profile_sha256':canonical_hash(config),'contrast_convention':CONVENTION,
        'thickness_nm':t.tolist(),'wavelengths_nm':wave.tolist(),
        'reflectance':reflectance.tolist(),'substrate_reflectance':substrate.tolist(),
        'spectral_contrast':(1-reflectance/substrate).tolist(),'oxide_nm':oxide,
        'illumination_na':float(illumination_na),'angular_model':'uniform pupil s/p average; isotropic material approximation',
        'model_provenance':{'constants':constants,'silicon':config['silicon_data'],'silica':config['substrate']['silica_source'],
            'implementation_sha256':hashlib.sha256(Path(__file__).read_bytes()).hexdigest()},
        'calibration_status':'spectral_only_no_rgb_response','layer_assignment_allowed':False,
        'chemical_identity_confirmed':False,'caveats':list(config['caveats'])}
    if response is not None and illustrative:raise ValueError('Choose supplied response or illustrative bands.')
    if illustrative:
        response={'kind':'illustrative_top_hat','source':'Unmeasured rectangular bands, equal energy; not an Axiocam calibration.',
            'radiometry_status':'illustrative_linear','wavelengths_nm':wave.tolist(),
            'effective_weight_rgb':np.stack([(wave>=590)&(wave<=720),(wave>=520)&(wave<=590),(wave>=435)&(wave<=520)],axis=1).astype(float).tolist()}
    if response is not None:
        weights=_response_weights(response,wave)
        background=np.trapezoid(weights*substrate[:,None],wave,axis=0)
        if np.any(background<=0):raise ValueError('Every channel needs positive substrate response.')
        result.update(contrast_rgb=(1-np.trapezoid(reflectance[:,:,None]*weights[None,:,:],wave,axis=1)/background).tolist(),
            calibration_status='illustrative_uncalibrated' if illustrative else 'conditional_supplied_response',
            response_sha256=canonical_hash(response),response_provenance={k:v for k,v in response.items() if k not in ('wavelengths_nm','effective_weight_rgb')})
    return result


def predict_material_ensemble(profile, thickness_nm, responses, *, oxide_nm=None, illumination_na=(0.,), gamma=(1.,), wavelengths_nm=None):
    """Finite sensitivity scenarios, not a calibrated probability distribution."""
    if not isinstance(profile,MaterialProfile):profile=load_material_profile(profile)
    oxides=[profile.config['substrate']['nominal_oxide_nm']] if oxide_nm is None else list(oxide_nm)
    curves=[];scenarios=[]
    for oxide in oxides:
        for na in illumination_na:
            for response in responses:
                curve=profile.forward(thickness_nm,response=response,oxide_nm=oxide,illumination_na=na,wavelengths_nm=wavelengths_nm)
                for exponent in gamma:
                    if not np.isfinite(exponent) or exponent<=0:raise ValueError('Positive gamma scenario required.')
                    curves.append((1-np.maximum(1-np.asarray(curve['contrast_rgb']),0)**exponent).tolist())
                    scenarios.append({'oxide_nm':float(oxide),'illumination_na':float(na),'gamma':float(exponent),'response_sha256':curve['response_sha256']})
    if not curves:raise ValueError('Nonempty optical scenarios required.')
    return {**{k:v for k,v in curve.items() if k not in ('reflectance','substrate_reflectance','spectral_contrast')},
        'contrast_rgb':curves,'scenario_provenance':scenarios,'calibration_status':'conditional_material_sensitivity_ensemble',
        'caveats':curve['caveats']+['Finite scenarios are not a confidence distribution; best matching scenario is permissive and can match contamination.']}


def manufacturer_response_scenarios(path, *, temperatures_k=(3000.,4500.,6500.), include_equal_energy=True):
    """Combine a vendor RGB sensitivity trace with explicitly unmeasured lamps.

    This is suitable for conditional sensitivity plots, not camera calibration.
    Both energy-response and photon-efficiency interpretations are retained.
    """
    path=Path(path);vendor=json.loads(path.read_text(encoding='utf-8-sig'))
    wave=np.asarray(vendor['wavelengths_nm'],float);q=np.asarray(vendor['relative_sensitivity_rgb'],float)
    if wave.ndim!=1 or len(wave)<2 or np.any(np.diff(wave)<=0) or q.shape!=(len(wave),3) or not np.isfinite(q).all() or np.any(q<0):
        raise ValueError('Invalid manufacturer response table.')
    lamps=[('equal_energy',np.ones(len(wave)))] if include_equal_energy else []
    for temperature in temperatures_k:
        if not np.isfinite(temperature) or temperature<=0:raise ValueError('Positive temperature required.')
        value=wave**-5/np.expm1(1.438776877e7/(wave*temperature))
        lamps.append((f'blackbody_{temperature:g}K',value/value.max()))
    responses=[]
    for name,spectrum in lamps:
        for interpretation in ('energy_response','photon_efficiency'):
            correction=1 if interpretation=='energy_response' else wave/wave.mean()
            responses.append({'kind':'manufacturer_trace_with_unmeasured_lamp_scenario','source':vendor['source'],
                'vendor_trace_sha256':hashlib.sha256(path.read_bytes()).hexdigest(),'lamp':name,'interpretation':interpretation,
                'radiometry_status':'conditional linear raw response, unmeasured effective lamp spectrum',
                'wavelengths_nm':wave.tolist(),'effective_weight_rgb':(q*(spectrum*correction)[:,None]).tolist(),
                'qualification':'Blackbody/equal energy scenarios do not reproduce measured LED spectra and are not guaranteed bounds.'})
    return responses
