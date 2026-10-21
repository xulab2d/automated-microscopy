"""Reference-calibrated RGB classes for pixel-first flake segmentation.

Distinct physical flakes, not pixels or repeated images, are training samples.
This implements the small-reference Gaussian approach without assigning any
layer names to the current unlabeled dataset. It is independent of acquisition.
"""
from collections import defaultdict
import json
import numpy as np


def profile_fingerprint(profile):
    import hashlib
    required=('material','substrate','oxide_nm','objective','radiometry_id')
    if any(profile.get(k) is None for k in required):raise ValueError('Incomplete optical/radiometry profile')
    return hashlib.sha256(json.dumps(profile,sort_keys=True).encode()).hexdigest()


def fit_reference_clusters(records,profile,*,minimum_flakes=5):
    """Fit one shrinkage Gaussian per independently confirmed thickness class.

    records: flake_id, class_id, contrast_rgb, profile_id, source,
    confirmation_method (AFM/Raman/PL or bare_substrate), reference_id,
    thickness_nm (positive material classes only). Repeated same-flake rows are
    averaged once and never count as extra independent calibration examples.
    """
    from sklearn.covariance import OAS
    identity=profile_fingerprint(profile);by_flake=defaultdict(list)
    if minimum_flakes<5:raise ValueError('At least five distinct references per class are required')
    for record in records:
        if any(not record.get(k) for k in ('flake_id','class_id','source','reference_id','confirmation_method')):
            raise ValueError('Reference identity and independent confirmation are required')
        if record.get('profile_id')!=identity:raise ValueError('Mixed optical profiles cannot share a color classifier')
        rgb=np.asarray(record.get('contrast_rgb'),float)
        if rgb.shape!=(3,) or not np.isfinite(rgb).all():raise ValueError('Finite RGB contrast required')
        if record['class_id']=='substrate':
            if record['confirmation_method']!='bare_substrate':raise ValueError('Substrate requires a bare-substrate control')
        elif (record['confirmation_method'] not in ('AFM','Raman','PL') or
              not np.isfinite(record.get('thickness_nm',np.nan)) or record['thickness_nm']<=0):
            raise ValueError('Positive thickness classes require an independent thickness reference')
        by_flake[record['flake_id']].append(record)
    by_class=defaultdict(list)
    for flake,observations in by_flake.items():
        classes={r['class_id'] for r in observations}
        if len(classes)!=1:raise ValueError('A reference ID spans different terraces; give each measured terrace a distinct physical ID')
        by_class[observations[0]['class_id']].append((flake,np.mean([r['contrast_rgb'] for r in observations],axis=0),observations))
    if 'substrate' not in by_class or len(by_class)<2:raise ValueError('Need substrate and at least one confirmed material class')
    classes=[]
    for label,groups in sorted(by_class.items()):
        if len(groups)<minimum_flakes:raise ValueError(f'{label}: too few independently confirmed flakes/patches')
        values=np.asarray([g[1] for g in groups]);fit=OAS().fit(values)
        if np.linalg.eigvalsh(fit.covariance_).min()<=0:raise ValueError('Degenerate references; no artificial spread is invented')
        refs=[r for _,_,rows in groups for r in rows]
        classes.append({'id':label,'mean_rgb':fit.location_.tolist(),'covariance_rgb':fit.covariance_.tolist(),
            'independent_reference_count':len(groups),'reference_ids':[g[0] for g in groups],
            'thickness_nm':None if label=='substrate' else float(np.median([r['thickness_nm'] for r in refs]))})
    return {'schema':1,'method':'one OAS-shrinkage Gaussian per independently confirmed class',
        'profile':profile,'profile_id':identity,'classes':classes,'references':records,
        'contrast_convention':'(substrate-flake)/substrate','posterior_calibrated_on_held_out_data':False,
        'scope':'Optical classification within supplied references; retain unknown/background pixels and review held-out chips.'}


def classify_pixels(contrast_rgb,model,*,profile_id,maximum_distance=4.,minimum_log_improvement=2.,chunk_size=65536):
    """Return class indices, including substrate=0 and unknown=-1, before blobs.

    Equal class priors prevent dataset prevalence from silently suppressing rare
    classes. Both within-class distance and improvement over the substrate null
    are required. Scores are model likelihoods, not validated probabilities.
    """
    from scipy.special import logsumexp
    if profile_id!=model['profile_id']:raise ValueError('Acquisition profile differs from reference calibration')
    if maximum_distance<=0 or minimum_log_improvement<0 or chunk_size<1:raise ValueError('Invalid classifier thresholds')
    pixels=np.asarray(contrast_rgb,dtype=float)
    if pixels.shape[-1:]!=(3,) or not np.isfinite(pixels).all():raise ValueError('Finite RGB contrasts required')
    classes=sorted(model['classes'],key=lambda c:c['id']!='substrate')
    if classes[0]['id']!='substrate':raise ValueError('Missing substrate null')
    means=np.asarray([c['mean_rgb'] for c in classes]);covs=np.asarray([c['covariance_rgb'] for c in classes])
    if means.shape!=(len(classes),3) or covs.shape!=(len(classes),3,3):raise ValueError('Malformed class model')
    np.linalg.cholesky(covs)
    inv=np.linalg.inv(covs);logdet=np.linalg.slogdet(covs)[1]
    flat=pixels.reshape(-1,3);labels=np.full(len(flat),-1,np.int16);improvement=np.empty(len(flat),np.float32);posterior=np.empty(len(flat),np.float32)
    for start in range(0,len(flat),chunk_size):
        stop=min(start+chunk_size,len(flat));res=flat[start:stop,None,:]-means
        d2=np.einsum('nki,kij,nkj->nk',res,inv,res)
        logp=-.5*(d2+logdet);best=logp.argmax(axis=1);ii=np.arange(stop-start)
        delta=logp[ii,best]-logp[:,0]
        accepted=(d2[ii,best]<=maximum_distance**2)&((best==0)|(delta>=minimum_log_improvement))
        labels[start:stop]=np.where(accepted,best,-1)
        improvement[start:stop]=delta
        posterior[start:stop]=np.exp(logp[ii,best]-logsumexp(logp,axis=1))
    shape=pixels.shape[:-1]
    return {'class_index':labels.reshape(shape),'classes':[c['id'] for c in classes],
        'log_likelihood_improvement_vs_substrate':improvement.reshape(shape),
        'best_posterior_within_reference_model':posterior.reshape(shape),
        'unknown_index':-1,'substrate_index':0,'posterior_is_empirically_calibrated':False}
