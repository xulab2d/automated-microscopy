"""Whole-image recovery of a user-identified optical contrast population.

This is a bulk reference control, not a monolayer classifier. The default seed
rectangle is the user's R2C1 red/green cluster. Raw scientific values and optical
background are retained; no previously detected proposal is required as a seed.
"""
import cv2
import numpy as np


def bulk_reference_labels(frame,prepared,options,limits=(-.8,-.5)):
    # This high-contrast user group has ample pixel SNR. A submicrometre
    # smoothing kernel would erase small, clearly visible bright platelets.
    contrast=prepared['contrast'];valid=prepared['valid']
    bounds=np.asarray(limits,float)
    if bounds.shape==(2,):bounds=np.tile(bounds,(2,1))
    if bounds.shape!=(2,2) or not np.all((-5<bounds[:,0])&(bounds[:,0]<bounds[:,1])&(bounds[:,1]<0)):
        raise ValueError('Bulk reference bounds must be ordered negative red/green contrasts.')
    lower,upper=bounds[:,0],bounds[:,1]
    rg=contrast[:,:,:2]
    seed=valid&np.all((rg>=lower)&(rg<=upper),axis=2)
    values=contrast[seed]
    result=np.zeros(seed.shape,np.int32);metadata={}
    if len(values)<30:return result,metadata
    centre=np.median(values,axis=0)
    # Edge pixels are optical mixtures of the material interior and substrate.
    # Fit their scalar material fraction on this measured R/G contrast direction;
    # the half-height contour is the boundary of a blurred step. This is not a
    # lower-thickness extension of the bulk class.
    denominator=float(centre[:2]@centre[:2])
    fraction=np.einsum('...i,i->...',rg,centre[:2])/denominator
    # Preserve exactly the color-direction tolerance supplied by the user's
    # rectangle. Blue was not one of the selected axes, so no uncalibrated blue
    # restriction is invented. The intervals are possible substrate/material
    # mixture fractions for each selected channel.
    t_low=np.max(rg/lower,axis=2);t_high=np.min(rg/upper,axis=2)
    support=valid&(fraction>=.5)&(t_low<=t_high)
    # Every component must contain actual pixels in the user-indicated cluster.
    # Empty gaps and unconnected bright patches cannot be pooled across space.
    count,components,stats,_=cv2.connectedComponentsWithStats(np.uint8(support),8)
    next_label=1;pixel_area=float(np.prod(frame.pixel_size_um))
    for component in range(1,count):
        x,y,w,h,pixels=map(int,stats[component])
        if pixels*pixel_area<options.min_detection_area_um2:continue
        local=components[y:y+h,x:x+w]==component
        seeds=int(np.count_nonzero(local&seed[y:y+h,x:x+w]))
        if not seeds:continue
        result[y:y+h,x:x+w][local]=next_label
        metadata[next_label]={'segmentation_method':'user_bulk_contrast_reference',
            'reference_kind':'user-identified bulk population; not a useful thin-flake target',
            'reference_seed_axes':['red','green'],'reference_seed_limits':bounds.tolist(),
            'reference_seed_pixels':seeds,'reference_contrast_rgb':centre.tolist(),
            'boundary_method':'half-height optical-mixture contour, connected to user contrast-cluster pixels'}
        next_label+=1
    return result,metadata
