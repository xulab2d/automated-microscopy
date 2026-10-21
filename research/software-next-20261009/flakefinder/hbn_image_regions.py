"""Image-only connected photometric regions; chemical evidence is downstream.

No reference curve, class map, hull, hole filling, shape rejection or thickness
assignment participates in these masks. Supplied foreground parents bound every
region. Photometric modes are estimates, not known terraces or chemical species.
"""
import cv2
import numpy as np
from scipy.ndimage import binary_propagation, find_objects
from scipy.stats import chi2
from sklearn.covariance import OAS
from sklearn.mixture import GaussianMixture


def image_regions(contrast, valid, substrate, noise, pixel, parent_labels=None,
                  *, check_cancel=None):
    """Return labels, preserved parents, region records and image-only diagnostics.

    Coordinates and areas use the supplied analysis grid. Tiny regions remain
    visible; the only 1 um2 floor limits splitting, never source retention.
    """
    check=check_cancel or (lambda:None)
    c=np.asarray(contrast,np.float32);valid=np.asarray(valid,bool)
    support=np.asarray(substrate,bool)&valid;pixel=np.asarray(pixel,float)
    raw_cov=np.asarray(noise,float)
    if c.shape!=valid.shape+(3,) or support.shape!=valid.shape or not np.isfinite(c[valid]).all():
        raise ValueError('Finite matching RGB contrast and support arrays required')
    if pixel.shape!=(2,) or np.any(pixel<=0) or not np.isfinite(pixel).all():
        raise ValueError('Positive calibrated pixel sizes required')
    if raw_cov.shape!=(3,3) or not np.isfinite(raw_cov).all() or np.linalg.eigvalsh(raw_cov).min()<=0:
        raise ValueError('Positive definite measured noise covariance required')
    if support.sum()<100:raise ValueError('At least 100 independent substrate pixels required')
    sigma=.3/pixel
    den=cv2.GaussianBlur(valid.astype(np.float32),(0,0),float(sigma[0]),sigmaY=float(sigma[1]))
    smooth=cv2.GaussianBlur(np.where(valid[:,:,None],c,0),(0,0),float(sigma[0]),sigmaY=float(sigma[1]))/np.maximum(den[:,:,None],1e-8)
    radius=int(np.ceil(max(sigma)*3))
    clear=cv2.erode(support.astype(np.uint8),cv2.getStructuringElement(cv2.MORPH_ELLIPSE,(2*radius+1,2*radius+1)))>0
    if clear.sum()<100:raise ValueError('Insufficient clear substrate support')
    samples=smooth[clear][::max(1,int(clear.sum()/20000))]
    centre=np.median(samples,axis=0)
    cov=OAS().fit(samples-centre).covariance_+np.eye(3)*max(np.trace(raw_cov)*1e-8,1e-16)
    inverse=np.linalg.inv(cov)
    residual=smooth-centre
    distance=np.einsum('...c,cd,...d->...',residual,inverse,residual)
    if parent_labels is None:
        foreground=binary_propagation(valid&(distance>chi2.ppf(.999,3)),mask=valid&(distance>chi2.ppf(.99,3)))
        _,parents=cv2.connectedComponents(foreground.astype(np.uint8),connectivity=4)
    else:
        parents=np.asarray(parent_labels)
        if parents.shape!=valid.shape or parents.dtype.kind not in 'iu' or np.any(parents<0):
            raise ValueError('Nonnegative integer parent labels required')
        parents=parents.astype(np.int32,copy=True)
    # Raw measured covariance regularizes subregion contrast; smoothed noise
    # alone would turn tiny shading changes into statistically certain cuts.
    white=np.linalg.inv(np.linalg.cholesky(raw_cov))
    labels=np.zeros(valid.shape,np.int32);regions=[];diagnostics=[];next_id=1
    area_pixel=float(np.prod(pixel))
    for parent,section in enumerate(find_objects(parents),1):
        check()
        if section is None:continue
        mask=(parents[section]==parent)&valid[section]
        if not mask.any():continue
        # Independent nearby recorded substrate is a local background estimate,
        # but cannot cut a parent or select a desired material color.
        y,x=section;pad=max(3,int(np.ceil(5/min(pixel))))
        nearby=(slice(max(0,y.start-pad),min(valid.shape[0],y.stop+pad)),slice(max(0,x.start-pad),min(valid.shape[1],x.stop+pad)))
        ns=clear[nearby]
        local_bg=np.median(smooth[nearby][ns],axis=0) if ns.sum()>=100 else centre
        values=(smooth[section][mask]-local_bg)@white.T
        modes=np.zeros(mask.shape,np.int16);model=None;selected_k=1;bics=[]
        # At least 64 pixels per attempted mode; deterministic bounded fitting.
        if len(values)>=128 and len(values)*area_pixel>=2:
            sample=values[::max(1,int(np.ceil(len(values)/3000)))]
            models=[]
            for k in range(1,min(3,len(sample)//64)+1):
                check()
                fitted=GaussianMixture(k,covariance_type='full',reg_covar=1.,random_state=0,n_init=1,max_iter=80).fit(sample)
                models.append(fitted);bics.append(float(fitted.bic(sample)))
            # Require distinct measured RGB populations, not just enough pixels
            # to make a minute color difference pass a likelihood test.
            for best in np.argsort(bics):
                if not best:break
                candidate=models[best]
                separation=[np.linalg.norm(candidate.means_[a]-candidate.means_[b]) for a in range(best+1) for b in range(a)]
                if bics[0]-bics[best]>30 and min(separation)>=5:
                    model=candidate;selected_k=int(best)+1;break
        if model is not None:
            unary=-np.log(np.maximum(model.predict_proba(values),1e-30))
            unary-=unary.min(axis=1,keepdims=True)
            # Potts regularization favors neighboring support while respecting
            # real RGB jumps. Every assignment stays inside its original parent.
            costs=np.zeros(mask.shape+(selected_k,),np.float32);costs[mask]=unary
            modes[mask]=np.argmin(unary,axis=1)
            whitened=(smooth[section]-local_bg)@white.T
            weights=[]
            for axis in (0,1):
                delta=np.diff(whitened,axis=axis)
                weights.append(3*np.exp(-np.sum(delta*delta,axis=2)/18))
            for _ in range(5):
                energy=costs.copy()
                for k in range(selected_k):
                    mismatch=(modes!=k)&mask
                    w=weights[0]*(mask[:-1]&mask[1:])
                    energy[:-1,:,k]+=w*mismatch[1:];energy[1:,:,k]+=w*mismatch[:-1]
                    w=weights[1]*(mask[:,:-1]&mask[:,1:])
                    energy[:,:-1,k]+=w*mismatch[:,1:];energy[:,1:,k]+=w*mismatch[:,:-1]
                modes[mask]=energy[mask].argmin(axis=1)
        diagnostics.append({'parent_instance_label':parent,'photometric_modes':selected_k,'bic':bics,'local_background_rgb':local_bg.tolist()})
        for mode in range(selected_k):
            n,parts,stats,_=cv2.connectedComponentsWithStats(np.uint8(mask&(modes==mode)),connectivity=4)
            for part in range(1,n):
                part_mask=parts==part
                labels[section][part_mask]=next_id
                regions.append({'instance_label':next_id,'parent_instance_label':parent,'photometric_mode':mode,
                    'area_px':int(stats[part,cv2.CC_STAT_AREA]),'area_um2':float(stats[part,cv2.CC_STAT_AREA]*area_pixel),
                    'geometry_basis':'image_only_regularized_rgb_populations','chemical_identity_confirmed':False,'layer_number':None})
                next_id+=1
    return {'labels':labels,'parent_labels':parents,'regions':regions,'diagnostics':diagnostics,
        'report':{'method':'image_only_parent_bounded_regularized_rgb_gmm','chemical_class_used_for_geometry':False,
            'options':{'smoothing_um':.3,'maximum_modes':3,'gmm_regularization_noise_variance':1.,
                'minimum_mode_separation_raw_noise_sigma':5.,'minimum_bic_improvement':30.,
                'spatial_pairwise_strength':3.,'spatial_iterations':5,'maximum_fit_samples':3000},
            'background_centre_rgb':centre.tolist(),'smoothed_substrate_covariance':cov.tolist(),
            'holes_preserved':True,'disconnected_islands_not_unioned':True,'source_regions':len(regions),
            'qualification':'Photometric populations are image-derived hypotheses, not validated chemical species or uniform thickness.'}}
