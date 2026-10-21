"""Small, offline response/noise check on stationary RGB exposure pairs.

Camera output is not assumed linear. This is not an EMVA characterization or a
material-specific contrast measurement. No hardware modules are imported.
"""
import json

import cv2
import numpy as np


def _images(frames):
    frames=list(frames)
    if not frames:
        raise ValueError('An image series is required.')
    images=[np.asarray(getattr(f,'rgb',f)) for f in frames]
    shape=images[0].shape
    if len(shape)!=3 or shape[2]!=3 or min(shape[:2])<32:
        raise ValueError('Expected RGB images at least 32 by 32 pixels.')
    for a in images:
        if a.shape!=shape or not np.issubdtype(a.dtype,np.number) or not np.isfinite(a).all() or np.min(a)<0:
            raise ValueError('Image shape/range mismatch or non-finite RGB values.')
    if any(hasattr(f,'metadata') for f in frames) and not all(hasattr(f,'metadata') for f in frames):
        raise ValueError('Do not mix Frame objects and arrays in one series.')
    return frames,images


def _profile(frame, dark=False):
    m=getattr(frame,'metadata',{})
    keys=('camera_job_template','objective_position','objective','pixel_type','white_level',
          'camera_gamma','gamma','camera_settings','radiometry_profile')
    profile={k:m.get(k) for k in keys}
    if not dark:
        profile['illumination_percent']=m.get('illumination_percent')
    profile['camera_resources']={k:v for k,v in m.get('camera_resources',{}).items() if k!='ff_exposure_ms'}
    profile['pixel_size_um']=getattr(frame,'pixel_size_um',None)
    return json.dumps(profile,sort_keys=True),profile


def _registration(images):
    h,w=images[0].shape[:2];scale=min(1.,512/max(h,w));size=(round(w*scale),round(h*scale))
    def feature(a):
        x=cv2.resize(a[:,:,1].astype(np.float32),size,interpolation=cv2.INTER_AREA)
        x=x-cv2.GaussianBlur(x,(0,0),2)
        return x/max(float(np.std(x)),1e-6)
    ref=feature(images[0]);window=cv2.createHanningWindow(size,cv2.CV_32F);rows=[]
    for index,a in enumerate(images[1:],1):
        shift,response=cv2.phaseCorrelate(ref.copy(),feature(a),window)
        finite=np.isfinite(response) and np.isfinite(shift).all()
        reliable=bool(finite and response>=.3)
        native=[float(x/scale) for x in shift] if finite else None
        if reliable and np.linalg.norm(native)>2:
            raise ValueError(f'Stationary-series check found image shift >2 pixels at frame {index}.')
        rows.append({'frame_index':index,'shift_xy_px':native,'response':float(response) if finite else None,
                     'reliable':reliable})
    return rows


def _patches(reference,white):
    # Same coordinates at every exposure; no level-specific brightness mask
    # that would preferentially discard the noisy/clipped side of a histogram.
    h,w=reference.shape[:2];side=max(8,min(32,min(h,w)//8))
    sample=reference[::max(1,h//256),::max(1,w//256)].reshape(-1,3)
    centre=np.median(sample,axis=0);candidates=[]
    for y in np.linspace(0,h-side,min(8,h//side),dtype=int):
        for x in np.linspace(0,w-side,min(8,w//side),dtype=int):
            a=reference[y:y+side,x:x+side].reshape(-1,3).astype(float)
            med=np.median(a,axis=0)
            if np.min(med)<white*.005 or np.max(np.percentile(a,99,axis=0))>=white*.99:
                continue
            texture=np.median(abs(a-med),axis=0)/np.maximum(med,1)
            score=float(np.max(texture)+np.linalg.norm((med-centre)/np.maximum(centre,1)))
            candidates.append((score,x,y,side))
    if len(candidates)<4:
        raise ValueError('Insufficient illuminated, unsaturated patches in the lowest exposure.')
    return [(int(x),int(y),int(s)) for _,x,y,s in sorted(candidates)[:16]]


def _sample(image,patches):
    return np.concatenate([image[y:y+s,x:x+s].reshape(-1,3) for x,y,s in patches]).astype(float)


def _covariance(differences):
    d=np.concatenate(differences);med=np.median(d,axis=0)
    mad=np.maximum(1.4826*np.median(abs(d-med),axis=0),1e-6)
    keep=np.all(abs(d-med)<=6*mad,axis=1)
    # A perfectly noiseless channel must not trim otherwise valid samples.
    if keep.sum()<32:
        keep=np.ones(len(d),bool)
    d=d[keep]
    return np.cov(d.T)/2,(d.T@d)/(2*len(d)),np.mean(d,axis=0),int(len(d))


def _response(exposures,signals):
    if len(exposures)<3:
        return {'status':'insufficient_unsaturated_levels','gamma':None}
    x=np.asarray(exposures,float);y=np.asarray(signals,float)
    slopes=[(y[j]-y[i])/(x[j]-x[i]) for i in range(len(x)) for j in range(i+1,len(x))]
    slope=float(np.median(slopes));intercept=float(np.median(y-slope*x))
    residual=float(np.max(abs(y-(intercept+slope*x)))/max(float(np.max(y)),1))
    lx=np.log(x);ly=np.log(y)
    gamma=float(np.median([(ly[j]-ly[i])/(lx[j]-lx[i]) for i in range(len(x)) for j in range(i+1,len(x))]))
    compatible=bool(slope>0 and abs(gamma-1)<=.08 and residual<=.02)
    return {'status':'consistent_with_linear_response' if compatible else 'nonlinear_or_unstable_response',
            'gamma':gamma,'gamma_model':'dark-subtracted signal proportional to exposure**gamma; descriptive fit',
            'affine_slope_adu_per_ms':slope,'affine_intercept_adu':intercept,
            'max_affine_residual_fraction_of_peak':residual,'levels_used':len(x)}


def assess_exposure_series(frames, exposures_ms, dark_frames=None, white_level=4095):
    """Return JSON-safe diagnostics and a measured exposure recommendation.

    ``frames`` is an all-Frame or all-HxWx3-array sequence. Every distinct
    positive exposure needs an even number of repeats (at least two); at least
    three levels are required. Repeats are paired in input order. Optional dark
    frames need at least two matching images. Their measured offset is assumed
    constant across the short exposure ladder, not independently verified.

    Auto-selected patches are low-texture, common-color regions, not certified
    substrate. Clipping is checked across the image as well as those patches.
    Preferred exposure minimizes measured R-G relative noise including pair
    drift, subject to clipping; it does not measure thin-flake sensitivity.
    """
    frames,images=_images(frames);t=np.asarray(exposures_ms,float)
    if t.shape!=(len(images),) or not np.isfinite(t).all() or np.any(t<=0):
        raise ValueError('Provide one finite positive exposure for each image.')
    if not np.isfinite(white_level) or white_level<=0:
        raise ValueError('White level must be finite and positive.')
    if any(np.max(a)>white_level for a in images):
        raise ValueError('Pixels exceed the declared white level.')
    levels=np.unique(t)
    if len(levels)<3 or any(np.sum(t==e)<2 or np.sum(t==e)%2 for e in levels):
        raise ValueError('Need at least three distinct exposure levels with complete repeat pairs.')
    identity,profile=_profile(frames[0]);warnings=[]
    for i,f in enumerate(frames):
        if _profile(f)[0]!=identity:
            raise ValueError(f'Optical profile mismatch at frame {i}.')
        m=getattr(f,'metadata',{});recorded=m.get('camera_resources',{}).get('ff_exposure_ms')
        if recorded is not None and not np.isclose(recorded,t[i],rtol=1e-6,atol=1e-6):
            raise ValueError(f'Exposure metadata mismatch at frame {i}.')
        if m.get('white_level',white_level)!=white_level:
            raise ValueError('White-level metadata mismatch.')
        if hasattr(f,'position'):
            p=frames[0].position;q=f.position
            if abs(p.z-q.z)>.3 or np.hypot(p.x-q.x,p.y-q.y)>min(f.pixel_size_um)*2:
                raise ValueError('Stationary-series stage/focus position mismatch.')
    metadata_verified=bool(all(getattr(f,'metadata',{}).get('camera_job_template') and
                               getattr(f,'metadata',{}).get('camera_resources') for f in frames))
    if not metadata_verified:
        warnings.append('Optical profile metadata incomplete; array values cannot verify camera settings.')
    registration=_registration(images)
    if not all(r['reliable'] for r in registration):
        warnings.append('Translation unverified for low-confidence image pairs; no registration was applied.')
    patches=_patches(images[int(np.argmin(t))],white_level)
    dark_rgb=np.zeros(3);dark_cov=None
    if dark_frames is not None:
        darks,dark_images=_images(dark_frames)
        if len(darks)<2 or len(darks)%2:
            raise ValueError('Dark reference requires complete repeat pairs.')
        if any(a.shape!=images[0].shape or np.max(a)>white_level for a in dark_images):
            raise ValueError('Dark image shape/range mismatch.')
        for f in darks:
            if _profile(f,dark=True)[0]!=_profile(frames[0],dark=True)[0]:
                raise ValueError('Dark optical profile mismatch (illumination and exposure may differ).')
        samples=[_sample(a,patches) for a in dark_images]
        dark_rgb=np.median(np.concatenate(samples),axis=0)
        dark_cov=_covariance([samples[i+1]-samples[i] for i in range(0,len(samples),2)])[0].tolist()
        warnings.append('Supplied dark reference is assumed valid and exposure-independent; neither is proven by this series.')
    else:
        warnings.append('No dark reference: apparent gamma is offset-biased; linearity cannot be confirmed.')
    rows=[]
    for e in levels:
        indices=np.flatnonzero(t==e);samples=[_sample(images[i],patches) for i in indices]
        signal=np.median(np.stack([np.median(s,axis=0) for s in samples]),axis=0)-dark_rgb
        cov,second,bias,count=_covariance([samples[j+1]-samples[j] for j in range(0,len(samples),2)])
        patch_clip=np.max([np.mean(s>=white_level*.99,axis=0) for s in samples],axis=0)
        # Whole-image clipping is computed exactly; sparse sampling can miss tiny bright flakes.
        clip=np.max([np.mean(images[i]>=white_level,axis=(0,1)) for i in indices],axis=0)
        usable=(signal>max(1.,white_level*.002)) & (patch_clip<.001)
        safe=np.maximum(signal,1);relative=cov/np.outer(safe,safe);v=np.array([1/safe[0],-1/safe[1],0.])
        rows.append({'exposure_ms':float(e),'frame_indices':indices.tolist(),'signal_rgb_adu':signal.tolist(),
                     'temporal_covariance_rgb_adu2':cov.tolist(),'paired_second_moment_rgb_adu2':second.tolist(),
                     'relative_covariance_rgb':relative.tolist(),'relative_noise_sigma_rgb':np.sqrt(np.maximum(np.diag(relative),0)).tolist(),
                     'rg_relative_noise_sigma':float(np.sqrt(max(v@cov@v,0))),
                     'rg_relative_noise_including_pair_drift':float(np.sqrt(max(v@second@v,0))),
                     'mean_pair_difference_rgb_adu':bias.tolist(),'noise_samples':count,
                     'patch_near_clip_fraction_rgb':patch_clip.tolist(),'whole_image_clip_fraction_rgb':clip.tolist(),
                     'response_usable_rgb':usable.tolist(),'eligible_for_preference':bool(usable.all() and np.max(clip)<=.001)})
    fits=[_response([r['exposure_ms'] for r in rows if r['response_usable_rgb'][c]],
                    [r['signal_rgb_adu'][c] for r in rows if r['response_usable_rgb'][c]]) for c in range(3)]
    eligible=[r for r in rows if r['eligible_for_preference']]
    best=min(eligible,key=lambda r:(r['rg_relative_noise_including_pair_drift'],r['exposure_ms'])) if eligible else None
    status=('indeterminate_without_dark_reference' if dark_frames is None else
            'consistent_with_linear_response' if all(f['status']=='consistent_with_linear_response' for f in fits)
            else 'nonlinear_or_insufficient_response')
    return {'schema':1,'status':status,'emva_certified':False,'linearity_confirmed':False,
            'scope':'Conditional camera-output response/noise check; no material or thin-flake validation.',
            'profile_metadata_complete':metadata_verified,'profile':profile,'white_level':float(white_level),
            'patches_xy_size_px':[list(p) for p in patches],
            'patch_kind':'fixed low-texture common-color regions; substrate identity unverified',
            'dark_reference_supplied':dark_frames is not None,'dark_offset_rgb_adu':dark_rgb.tolist(),
            'dark_temporal_covariance_rgb_adu2':dark_cov,'registration':registration,
            'response_rgb':fits,'levels':rows,
            'preferred_setting':None if best is None else {'exposure_ms':best['exposure_ms'],'frame_indices':best['frame_indices'],
                'criterion':'minimum measured R-G relative temporal noise including pair drift among unclipped settings',
                'conditional':True,'response_requires_review':status!='consistent_with_linear_response'},
            'engineering_criteria':{'gamma_distance_from_one':.08,'affine_residual_fraction_of_peak':.02,
                'max_patch_near_clip_fraction':.001,'max_whole_image_clip_fraction':.001,'max_reliable_shift_px':2},
            'warnings':warnings}
