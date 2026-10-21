"""Whole-field optical populations followed by connected color domains.

This is an exploratory color segmentation, not a material/layer classifier.
One mixture is fitted per field, independently of object proposals. Exposed
substrate remains an explicit statistical class; disconnected particles never
pool their area. Color populations may still describe residue or optical halos.
"""
import time

import cv2
import numpy as np

from .colors import ColorGroups


def _quadratic(values,mean,inverse):
    d=values-np.asarray(mean,np.float32)
    return np.einsum('...i,ij,...j->...',d,np.asarray(inverse,np.float32),d,optimize=False)


def _balanced_samples(values,budget,noise,rng):
    """Equalize occupied color cells without fitting to object-selected pixels.

    Half the budget preserves the measured image density; the other half limits
    the dominance of bare substrate so small, rare-color platelets enter EM.
    Cell width is set by measured color noise, with a 1% contrast floor.
    """
    if len(values)<=budget:return values
    pool=values[rng.choice(len(values),min(len(values),80000),replace=False)]
    bins=np.floor(pool/np.maximum(3*np.asarray(noise),.01)).astype(np.int32)
    _,inverse,counts=np.unique(bins,axis=0,return_inverse=True,return_counts=True)
    weights=1/np.sqrt(counts[inverse]);weights/=weights.sum()
    rare=pool[rng.choice(len(pool),budget//2,replace=False,p=weights)]
    density=pool[rng.choice(len(pool),budget-len(rare),replace=False)]
    return np.concatenate((density,rare))


def global_domain_labels(frame,prepared,options):
    start=time.perf_counter();valid=prepared['valid'];rng=np.random.default_rng(1729)
    # Resolve the color at a fixed physical scale; this is much smaller than a
    # useful terrace and does not include an operation joining nearby particles.
    sigma_um=options.spatial_sigma_um
    smooth=prepared['smooth']
    values=smooth[valid]
    substrate_sample=values[::max(1,len(values)//30000)].astype(float)
    centre=np.median(substrate_sample,axis=0)
    delta=substrate_sample-centre
    core=substrate_sample[np.linalg.norm(delta,axis=1)<=np.percentile(np.linalg.norm(delta,axis=1),60)]
    covariance=np.cov(core.T)+np.eye(3)*.002**2
    inverse=np.linalg.inv(covariance)
    noise=np.sqrt(np.diag(covariance))
    sample=_balanced_samples(values,options.sample_per_tile,noise,rng)
    groups=ColorGroups.fit(sample,options.max_components)
    # Add an anchored substrate component. It cannot be displaced by a bright
    # crystal population or reinterpreted as a material thickness class.
    background={'id':'substrate','mean':centre.tolist(),'covariance':covariance.tolist(),
                'weight':.5,'contrast_norm':float(np.linalg.norm(centre))}
    optical=[]
    for group in groups.groups:
        if float(_quadratic(np.array(group['mean']),centre,inverse))>9:
            group=dict(group);group['weight']*=.5;optical.append(group)
    model=ColorGroups({'groups':[background]+optical})
    colors=np.zeros(valid.shape,np.uint8);distance=np.zeros(valid.shape,np.float32)
    # Chunk prediction keeps peak memory independent of the number of model
    # components and avoids allocating N_pixels x N_components likelihoods.
    flat=smooth.reshape(-1,3);flat_labels=colors.ravel();flat_distance=distance.ravel()
    precisions=[np.linalg.inv(np.array(g['covariance'])) for g in model.groups]
    constants=[-.5*np.linalg.slogdet(np.array(g['covariance']))[1]+np.log(max(g['weight'],1e-12)) for g in model.groups]
    for offset in range(0,len(flat),131072):
        chunk=flat[offset:offset+131072];best=np.full(len(chunk),-np.inf,np.float32);chosen=np.zeros(len(chunk),np.uint8)
        for j,(group,precision,constant) in enumerate(zip(model.groups,precisions,constants)):
            d=_quadratic(chunk,group['mean'],precision)
            if j==0:flat_distance[offset:offset+len(chunk)]=d
            score=-.5*d+constant;better=score>best;best[better]=score[better];chosen[better]=j
        flat_labels[offset:offset+len(chunk)]=chosen
    # A three-sigma substrate core is explicitly background. Strong material
    # evidence must occur somewhere within a region; uncertain boundaries may
    # remain connected to it. No original foreground/object mask is consulted.
    strength=np.linalg.norm(smooth-centre,axis=2)
    support=valid&(distance>9)&(strength>options.min_contrast/2)
    seed=valid&(distance>options.substrate_sigma**2)&(strength>options.min_contrast)
    labels=np.zeros(valid.shape,np.int32);evidence={};next_label=1;pixel_area=float(np.prod(frame.pixel_size_um))
    for group_index,group in enumerate(model.groups[1:],1):
        count,components,stats,_=cv2.connectedComponentsWithStats(np.uint8((colors==group_index)&support),8)
        for component in range(1,count):
            x,y,w,h,pixels=map(int,stats[component])
            if pixels*pixel_area<options.min_detection_area_um2:continue
            piece=components[y:y+h,x:x+w]==component
            if not np.any(piece&seed[y:y+h,x:x+w]):continue
            labels[y:y+h,x:x+w][piece]=next_label
            evidence[next_label]={'segmentation_method':'whole_field_color_population_connected_domain',
                'global_color_population':group_index,'global_color_mean_rgb':group['mean'],
                'global_color_covariance':group['covariance'],'global_background_contrast_rgb':centre.tolist(),
                'spatial_sigma_um':sigma_um,'population_assignment':'optical color only; material/layer unconfirmed'}
            next_label+=1
    report={'method':'whole-field density-balanced full-covariance mixture; anchored substrate; connected domains',
        'model':model.data,'sample_count':len(sample),'spatial_sigma_um':sigma_um,
        'elapsed_seconds':time.perf_counter()-start,'region_count':len(evidence),
        'minimum_detection_area_um2':options.min_detection_area_um2,
        'warning':'Experimental optical populations may describe residue, halos or crystals. No layer assignment.'}
    return labels,evidence,report
