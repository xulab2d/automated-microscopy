"""Explicit literature-guided search prior, never a layer-number classifier."""
import numpy as np

THIN_PRIOR={
    'schema':1,'kind':'thin_contrast_prior','model_id':'mote2-rg-direction-yamamoto2014-v1',
    'material':'2H-MoTe2','substrate':'SiO2/Si','nominal_oxide_nm':285,
    'source':'https://doi.org/10.1021/nn5007607.s001','figure':'S4',
    'source_qualification':'Caption says 285 nm oxide; plot inset says 300 nm. Treat as a nearby-oxide search prior, not a numerical calibration.',
    'contrast_definition':'(substrate-film)/substrate; opposite sign to the publication',
    'approximate_reference_rg':[[.085,.135],[.17,.27],[.22,.285],[.25,.275]],
    'reference_layers':[1,2,3,4],
    'digitization_note':'Approximate visual readings, about 0.015 contrast reading precision; not uncertainty bounds or camera transfer calibration.',
    'parameters':{'minimum_direction_z':3.,'green_search_interval':[.02,.45],
                  'catalog_supported_area_um2':100.,'minimum_width_um':5.},
    'components':[],
    'qualification':'The first few published layer contrasts darken green more than red. Direction, local coherence and a broad magnitude window rank potential thin flakes. Camera spectrum, oxide, phase and processing are not calibrated; no chemical or layer identity is assigned.',
    'search_interval_note':'2–45% green is an explicit broad engineering search window encompassing the approximately 7% literature scale and S4 values. It is not a monolayer acceptance interval.',
}


def thin_contrast_evidence(contrast,covariance,profile=None):
    """Require resolved +R and +(G-R), accounting for correlated errors.

    z values describe supplied regional measurement uncertainty. Segmentation
    selection bias and unknown optical transfer prevent probability claims.
    Blue remains measured but has no imposed sign or assumed layer value.
    """
    profile=profile or THIN_PRIOR
    vector=np.asarray(contrast,float);cov=np.asarray(covariance,float)
    if vector.shape!=(3,) or cov.shape!=(3,3) or not np.isfinite(vector).all() or not np.isfinite(cov).all():
        raise ValueError('Thin contrast requires finite RGB and 3x3 covariance.')
    cov=(cov+cov.T)/2
    if np.linalg.eigvalsh(cov).min() < -1e-10:raise ValueError('Contrast covariance is not positive semidefinite.')
    directions=np.array([[1.,0.,0.],[-1.,1.,0.]])
    margins=directions@vector
    variances=np.einsum('ni,ij,nj->n',directions,cov,directions)
    z=margins/np.sqrt(np.maximum(variances,1e-12))
    settings=profile['parameters'];low,high=settings['green_search_interval']
    in_window=bool(low<=vector[1]<=high)
    direction=bool(np.min(z)>=settings['minimum_direction_z'])
    compatible=direction and in_window
    reason=('Resolved darkening in red with stronger darkening in green, within the broad search interval.' if compatible else
            'Color direction is unresolved or outside the first-few-layer reference direction.' if not direction else
            'Green contrast is outside the declared broad thin-material search interval.')
    return {'status':'thin_contrast_candidate' if compatible else 'unresolved_color_plateau',
        'compatible':compatible,'score':float(max(0,np.min(z))) if in_window else 0.,
        'red_z':float(z[0]),'green_minus_red_z':float(z[1]),
        'green_minus_red_contrast':float(margins[1]),'within_search_interval':in_window,
        'reason':reason,'reference':profile['source'],'model_id':profile['model_id'],
        'layer_number':None,'chemical_identity_confirmed':False,
        'uncertainty_scope':'Regional spatial/color uncertainty only; optical transfer and selection bias are uncalibrated.'}
