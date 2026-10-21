"""Measure optical contrast on immutable previously delineated objects.

This module never segments, changes a mask, filters an object, or assigns a
material/layer label. A failed measurement remains a result for that label.
"""
from dataclasses import dataclass,asdict
import hashlib,time

import cv2
import numpy as np
from scipy.ndimage import find_objects,distance_transform_edt


@dataclass(frozen=True)
class ExistingRegionOptions:
    minimum_area_um2: float = 25.
    interior_erosion_um: float = .5
    substrate_clearance_um: float = 1.
    annulus_width_um: float = 7.
    spatial_patch_um: float = 1.
    minimum_interior_pixels: int = 8
    minimum_background_patches: int = 12

    def __post_init__(self):
        if not all(np.isfinite(v) for v in asdict(self).values()):raise ValueError('Nonfinite measurement options.')
        if min(self.minimum_area_um2,self.interior_erosion_um,self.substrate_clearance_um)<0 or min(self.annulus_width_um,self.spatial_patch_um)<=0:
            raise ValueError('Invalid physical measurement scales.')
        if self.minimum_interior_pixels<3 or self.minimum_background_patches<3:raise ValueError('Insufficient measurement sample limits.')


def _patches(values,mask,origin_xy,pixel_size_um,patch_um,full_width):
    yy,xx=np.nonzero(mask)
    if len(xx)==0:return np.empty((0,3)),np.empty((0,2)),np.empty(0)
    bx=max(1,int(round(patch_um/pixel_size_um[0])));by=max(1,int(round(patch_um/pixel_size_um[1])))
    gx=xx+origin_xy[0];gy=yy+origin_xy[1]
    cell=(gy//by)*(full_width//bx+1)+gx//bx
    _,which=np.unique(cell,return_inverse=True);counts=np.bincount(which).astype(float)
    means=np.stack([np.bincount(which,weights=values[mask,i])/counts for i in range(3)],axis=1)
    xy=np.stack([np.bincount(which,weights=gx)/counts,np.bincount(which,weights=gy)/counts],axis=1)
    # A partial spatial cell is still measured; its area is used in weighting.
    return means,xy,counts


def _weighted_stats(samples,counts):
    weights=counts/counts.sum();mean=weights@samples
    neffective=1/float(weights@weights)
    delta=samples-mean
    covariance=(delta.T*weights)@delta/max(1-float(weights@weights),1e-12)
    return mean,covariance,neffective


def _measure_one(contrast,labels,clear,valid,label,section,frame,options):
    ys,xs=section;px,py=frame.pixel_size_um
    padx=int(np.ceil(options.annulus_width_um/px))+2;pady=int(np.ceil(options.annulus_width_um/py))+2
    x0=max(0,xs.start-padx);x1=min(labels.shape[1],xs.stop+padx)
    y0=max(0,ys.start-pady);y1=min(labels.shape[0],ys.stop+pady)
    mask=labels[y0:y1,x0:x1]==label;values=contrast[y0:y1,x0:x1]
    record={'instance_label':int(label),'measurement_status':'unmeasurable','mask_modified':False,
        'mask_area_pixels':int(mask.sum()),'mask_area_um2':float(mask.sum()*px*py),
        'layer_number':None,'chemical_identity_confirmed':False}
    if record['mask_area_um2']<options.minimum_area_um2:
        return {**record,'reason':'below_measurement_area_floor'}
    # Padding makes erosion correct even for objects touching the source edge.
    padded=np.pad(mask,1)
    distance=distance_transform_edt(padded,sampling=(py,px))[1:-1,1:-1]
    interior=mask&(distance>options.interior_erosion_um)&valid[y0:y1,x0:x1]
    fallback=False
    if interior.sum()<options.minimum_interior_pixels:
        interior=mask&valid[y0:y1,x0:x1];fallback=True
    record.update(interior_pixels=int(interior.sum()),interior_fallback_to_full_mask=fallback)
    if interior.sum()<options.minimum_interior_pixels:
        return {**record,'reason':'insufficient_valid_interior_pixels'}
    outside_distance=distance_transform_edt(~mask,sampling=(py,px))
    annulus=clear[y0:y1,x0:x1]&(outside_distance<=options.annulus_width_um)
    object_patches,object_xy,object_counts=_patches(values,interior,(x0,y0),frame.pixel_size_um,options.spatial_patch_um,labels.shape[1])
    bg_patches,bg_xy,bg_counts=_patches(values,annulus,(x0,y0),frame.pixel_size_um,options.spatial_patch_um,labels.shape[1])
    record.update(interior_spatial_patches=len(object_patches),background_spatial_patches=len(bg_patches),
        background_pixels=int(annulus.sum()),background_excludes_all_source_instances=True)
    if len(object_patches)<3:return {**record,'reason':'insufficient_interior_spatial_patches'}
    if len(bg_patches)<options.minimum_background_patches:return {**record,'reason':'insufficient_clear_local_substrate'}
    mean,object_cov,nobject=_weighted_stats(object_patches,object_counts)
    # The median is not selected for being close to zero or a material color.
    # All other known foreground and their clearance bands were excluded first.
    background=np.median(bg_patches,axis=0)
    _,bg_cov,nbg=_weighted_stats(bg_patches,bg_counts)
    centre=np.average(object_xy,axis=0,weights=object_counts)
    dxy=(bg_xy-centre)*[px,py]
    sectors=np.floor((np.arctan2(dxy[:,1],dxy[:,0])+np.pi)*4/np.pi).astype(int)%8
    medians=[np.median(bg_patches[sectors==s],axis=0) for s in range(8) if np.sum(sectors==s)>=2]
    systematic=np.cov(np.asarray(medians).T) if len(medians)>=3 else bg_cov
    denominator=1-background
    if np.any(denominator<=0):return {**record,'reason':'invalid_local_substrate_intensity'}
    local=(mean-background)/denominator
    Jo=np.diag(1/denominator);Jb=np.diag((mean-1)/denominator**2)
    # Background sector variation is systematic and is NOT divided by N.
    covariance=Jo@(object_cov/nobject)@Jo.T+Jb@(systematic+bg_cov/nbg)@Jb.T
    eigen,vectors=np.linalg.eigh((covariance+covariance.T)/2)
    covariance=(vectors*np.maximum(eigen,1e-12))@vectors.T
    return {**record,'measurement_status':'measured','reason':'exact_existing_mask_with_clear_local_substrate',
        'contrast_rgb':local.tolist(),'contrast_covariance':covariance.tolist(),
        'standard_error_rgb':np.sqrt(np.diag(covariance)).tolist(),
        'global_interior_contrast_rgb':mean.tolist(),'local_substrate_contrast_rgb':background.tolist(),
        'interior_patch_covariance':object_cov.tolist(),'background_patch_covariance':bg_cov.tolist(),
        'background_systematic_covariance':systematic.tolist(),'background_sector_count':len(medians),
        'effective_interior_patches':nobject,'effective_background_patches':nbg,
        'interior_patch_spread_rgb':np.sqrt(np.maximum(np.diag(object_cov),0)).tolist(),
        'measurement_method':'Existing exact mask; spatial patch means; foreground-excluded local substrate and sector uncertainty',
        'uncertainty_note':'Includes measured interior heterogeneity and local substrate variation; spatial correlation and optical transfer remain uncalibrated.'}


def measure_existing_regions(frame,contrast,labels,valid=None,options=None,additional_foreground=None):
    """Return ({instance_label: measurement}, report), preserving EVERY label.

    ``contrast`` is (fitted substrate - stored RGB)/fitted substrate, before
    preview gamma. ``labels`` is the exact saved source instance map, including
    rejected/small objects. Optional extra foreground only excludes background
    pixels; it cannot create, remove or alter a measured object.
    """
    start=time.perf_counter();options=options or ExistingRegionOptions()
    contrast=np.asarray(contrast);labels=np.asarray(labels)
    if contrast.shape!=frame.rgb.shape or labels.shape!=contrast.shape[:2] or not np.isfinite(contrast).all():
        raise ValueError('Source image, contrast and instance-map shapes must agree.')
    if not np.issubdtype(labels.dtype,np.integer) or np.any(labels<0):raise ValueError('Expected nonnegative integer instance labels.')
    valid=np.ones(labels.shape,bool) if valid is None else np.asarray(valid,bool)
    if valid.shape!=labels.shape:raise ValueError('Valid substrate mask shape mismatch.')
    occupied=labels>0
    if additional_foreground is not None:
        extra=np.asarray(additional_foreground,bool)
        if extra.shape!=labels.shape:raise ValueError('Extra foreground shape mismatch.')
        occupied=occupied|extra
    distance=distance_transform_edt(~occupied,sampling=(frame.pixel_size_um[1],frame.pixel_size_um[0]))
    clear=valid&~occupied&(distance>options.substrate_clearance_um)
    rows={}
    for label,section in enumerate(find_objects(labels),1):
        if section is None:continue
        rows[label]=_measure_one(contrast,labels,clear,valid,label,section,frame,options)
    report={'method':'immutable_existing_mask_local_contrast','options':asdict(options),'regions':len(rows),
        'measured_regions':sum(row['measurement_status']=='measured' for row in rows.values()),
        'mask_modified':False,'mask_sha256':hashlib.sha256(labels.tobytes()).hexdigest(),
        'clear_substrate_fraction':float(clear.sum()/max(valid.sum(),1)),
        'elapsed_seconds':time.perf_counter()-start,'qualification':'Measurements refine optical color evidence; all original object geometries remain unchanged.'}
    return rows,report
