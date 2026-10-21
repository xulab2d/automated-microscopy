"""Noise-aware connected color plateaus, independent of a material catalog.

The ordered-adjacency/union-find strategy follows statistical region merging,
but this implementation uses a measured RGB Gaussian noise model and a pooled
mean test, not the distribution-free predicate of Nock & Nielsen (2004).
Consequently their theoretical error bound does not apply here. Region scores
are descriptive, selection-biased evidence, not calibrated discovery p-values.
"""
from dataclasses import dataclass, asdict
import time

import cv2
import numpy as np
from scipy.ndimage import find_objects
from scipy.stats import chi2


@dataclass(frozen=True)
class ThinRegionOptions:
    block_size_um: float = .7
    merge_probability: float = .999
    local_evidence_probability: float = .999
    minimum_region_area_um2: float = 25.
    useful_area_um2: float = 100.
    annulus_width_um: float = 6.
    minimum_annulus_blocks: int = 30

    def __post_init__(self):
        if not all(np.isfinite(v) for v in asdict(self).values()):
            raise ValueError('Region options must be finite.')
        if min(self.block_size_um,self.minimum_region_area_um2,self.annulus_width_um)<=0:
            raise ValueError('Physical region scales must be positive.')
        if self.useful_area_um2<self.minimum_region_area_um2 or self.minimum_annulus_blocks<12:
            raise ValueError('Invalid region/annulus area.')
        if not .5<self.merge_probability<1 or not .5<self.local_evidence_probability<1:
            raise ValueError('Invalid nominal probability.')


def _covariance(values):
    values=np.asarray(values,np.float64)
    centre=np.median(values,axis=0)
    scale=np.maximum(1.4826*np.median(abs(values-centre),axis=0),1e-7)
    score=np.sum(((values-centre)/scale)**2,axis=1)
    core=values[score<=chi2.ppf(.99,3)]
    cov=np.cov(core.T) if len(core)>12 else np.diag(scale**2)
    # Only a numerical eigenvalue floor; no absolute optical contrast gate.
    eigen,vectors=np.linalg.eigh(cov)
    floor=max(float(eigen.max())*1e-5,1e-14)
    return (vectors*np.maximum(eigen,floor))@vectors.T


def _block_image(contrast,valid,pixel_size_um,options):
    bx=max(1,int(round(options.block_size_um/pixel_size_um[0])))
    by=max(1,int(round(options.block_size_um/pixel_size_um[1])))
    height,width=contrast.shape[:2];h=height//by;w=width//bx
    cells=np.asarray(contrast[:h*by,:w*bx],np.float32).reshape(h,by,w,bx,3)
    yy,xx=np.indices((by,bx))
    partition=((yy//2+xx//2)%2)==0 if min(bx,by)>=4 else ((yy+xx)%2)==0
    if partition.all():
        block=cells.mean((1,3));measure=block.copy();separated=False
    else:
        weight=partition[None,:,None,:,None]
        block=(cells*weight).sum((1,3))/partition.sum()
        measure=(cells*(~weight)).sum((1,3))/(~partition).sum()
        separated=True
    good=np.asarray(valid[:h*by,:w*bx],bool).reshape(h,by,w,bx).all((1,3))
    if good.sum()<100:raise ValueError('Too little valid substrate for regional analysis.')
    return block,measure,good,bx,by,separated


def _merge_regions(block,valid,covariance,threshold):
    """Merge adjacent region means; disconnected pixels cannot pool area."""
    h,w=valid.shape;n=h*w
    whitening=np.linalg.inv(np.linalg.cholesky(covariance)).T
    values=(block.reshape(-1,3)@whitening).astype(np.float64)
    indices=np.arange(n,dtype=np.int32).reshape(h,w)
    pair_h=valid[:,:-1]&valid[:,1:];pair_v=valid[:-1]&valid[1:]
    left=np.concatenate((indices[:,:-1][pair_h],indices[:-1][pair_v]))
    right=np.concatenate((indices[:,1:][pair_h],indices[1:][pair_v]))
    difference=values[left]-values[right]
    order=np.argsort(np.sum(difference*difference,axis=1),kind='stable')
    parent=np.arange(n,dtype=np.int32);count=np.ones(n,np.int32)
    sums=values.copy()
    # Scalar loops avoid per-edge array creation, and require no JIT/runtime.
    for edge in order:
        a=int(left[edge]);b=int(right[edge])
        while parent[a]!=a:
            parent[a]=parent[parent[a]];a=int(parent[a])
        while parent[b]!=b:
            parent[b]=parent[parent[b]];b=int(parent[b])
        if a==b:continue
        na=int(count[a]);nb=int(count[b]);sa=sums[a];sb=sums[b]
        d0=sa[0]/na-sb[0]/nb;d1=sa[1]/na-sb[1]/nb;d2=sa[2]/na-sb[2]/nb
        if (d0*d0+d1*d1+d2*d2)>threshold*(1/na+1/nb):continue
        if na<nb:a,b=b,a
        parent[b]=a;count[a]+=count[b];sums[a]+=sums[b]
    roots=parent.copy()
    while True:
        next_roots=roots[roots]
        if np.array_equal(next_roots,roots):break
        roots=next_roots
    roots=roots.reshape(h,w)
    _,inverse=np.unique(roots[valid],return_inverse=True)
    labels=np.zeros((h,w),np.int32);labels[valid]=inverse+1
    return labels


def _local_evidence(block,valid,labels,label,section,covariance,options,block_scale_xy):
    pixel_area=float(np.prod(block_scale_xy))
    ys,xs=section;radius=max(2,int(np.ceil(options.annulus_width_um/np.sqrt(pixel_area))))
    y0=max(0,ys.start-radius);y1=min(labels.shape[0],ys.stop+radius)
    x0=max(0,xs.start-radius);x1=min(labels.shape[1],xs.stop+radius)
    local_labels=labels[y0:y1,x0:x1];mask=local_labels==label
    values=block[y0:y1,x0:x1];v=valid[y0:y1,x0:x1]
    annulus=cv2.dilate(np.uint8(mask),cv2.getStructuringElement(cv2.MORPH_ELLIPSE,(2*radius+1,2*radius+1))).astype(bool)&~mask&v
    if annulus.sum()<options.minimum_annulus_blocks:return None
    samples=values[annulus]
    # A robust annulus population supplies the local substrate. Its sector
    # spread retains illumination uncertainty. Nearby dominant contamination
    # remains a known failure mode and is visible in the saved statistics.
    inverse=np.linalg.inv(covariance)
    # Use the robust local annulus population, not an arbitrary half closest to
    # zero: truncating at zero would bias the faint local contrast itself.
    centre=np.median(samples,axis=0)
    centered=samples-centre
    distance=np.einsum('ni,ij,nj->n',centered,inverse,centered,optimize=False)
    quiet=samples[distance<=np.percentile(distance,90)]
    background=np.median(quiet,axis=0)
    interior=values[mask];mean=interior.mean(axis=0)
    denominator=np.maximum(1-background,1e-6)
    delta=(mean-background)/denominator
    quiet_limit=np.percentile(distance,90)
    y,x=np.indices(mask.shape);cy,cx=np.mean(np.argwhere(mask),axis=0)
    sector=np.floor((np.arctan2(y-cy,x-cx)+np.pi)*4/np.pi).astype(int)%8
    local_distance=np.einsum('...i,ij,...j->...',values-centre,inverse,values-centre,optimize=False)
    quiet_mask=annulus&(local_distance<=quiet_limit)
    sector_means=[np.median(values[quiet_mask&(sector==s)],axis=0) for s in range(8) if np.sum(quiet_mask&(sector==s))>=3]
    background_cov=np.cov(np.asarray(sector_means).T) if len(sector_means)>=3 else _covariance(quiet)
    # Do not divide sector variability by pixel count: it is a systematic local
    # background uncertainty, not independent camera noise.
    jacobian_interior=np.diag(1/denominator)
    jacobian_background=np.diag((mean-1)/denominator**2)
    uncertainty=jacobian_interior@(covariance/max(len(interior),1))@jacobian_interior.T
    uncertainty+=jacobian_background@(background_cov+covariance/max(len(quiet),1))@jacobian_background.T
    statistic=float(delta@np.linalg.solve(uncertainty,delta))
    whitened=(interior-mean)@np.linalg.inv(np.linalg.cholesky(covariance)).T
    heterogeneity=float(np.mean(np.sum(whitened**2,axis=1))/3)
    # A measured plane describes gradients; expose rather than hide this value.
    xy=np.argwhere(mask).astype(float);xy-=xy.mean(axis=0)
    coefficients=np.linalg.lstsq(xy,interior-mean,rcond=None)[0]
    ramp=xy@coefficients
    ramp_span=np.percentile(ramp,95,axis=0)-np.percentile(ramp,5,axis=0)
    points=np.argwhere(mask)
    width_height=cv2.minAreaRect(np.float32(points[:,::-1]*block_scale_xy))[1] if len(points)>2 else (0,0)
    return {'contrast_rgb':delta.tolist(),'global_contrast_rgb':mean.tolist(),
        'local_substrate_contrast_rgb':background.tolist(),'contrast_covariance':uncertainty.tolist(),
        'standard_error_rgb':np.sqrt(np.diag(uncertainty)).tolist(),
        'local_evidence_statistic':statistic,'noise_normalized_heterogeneity':heterogeneity,
        'within_region_ramp_span_rgb':ramp_span.tolist(),'background_sector_count':len(sector_means),
        'centroid_block_xy':[float(points[:,1].mean()+x0),float(points[:,0].mean()+y0)],
        'minimum_width_um':float(min(width_height)+min(block_scale_xy)),
        'region_blocks':len(interior),'background_blocks':len(quiet),
        'uncertainty_note':'Includes measured local sector variation; excludes optical calibration and selection bias.'}


def propose_thin_regions(frame,contrast,valid=None,options=None):
    """Return full-source labels, evidence keyed by label, and a runtime report.

    ``contrast`` is (substrate - stored intensity) / substrate, never preview
    gamma RGB. A physical reflectance interpretation additionally requires a
    verified linear camera response. No bulk reference or material RGB direction is consulted.
    The label geometry is block-resolved with the reported boundary uncertainty.
    """
    options=options or ThinRegionOptions();start=time.perf_counter()
    contrast=np.asarray(contrast,np.float32)
    if contrast.shape!=frame.rgb.shape or not np.isfinite(contrast).all():raise ValueError('Invalid linear contrast map.')
    valid=np.ones(contrast.shape[:2],bool) if valid is None else np.asarray(valid,bool)
    block,measure,good,bx,by,separated=_block_image(contrast,valid,frame.pixel_size_um,options)
    dh=(block[:,1:]-block[:,:-1])[good[:,1:]&good[:,:-1]]
    dv=(block[1:]-block[:-1])[good[1:]&good[:-1]]
    differences=np.concatenate((dh[::max(1,len(dh)//30000)],dv[::max(1,len(dv)//30000)]))/np.sqrt(2)
    covariance=_covariance(differences)
    mh=(measure[:,1:]-measure[:,:-1])[good[:,1:]&good[:,:-1]]
    mv=(measure[1:]-measure[:-1])[good[1:]&good[:-1]]
    measurement_covariance=_covariance(np.concatenate((mh[::max(1,len(mh)//30000)],mv[::max(1,len(mv)//30000)]))/np.sqrt(2))
    labels=_merge_regions(block,good,covariance,float(chi2.ppf(options.merge_probability,3)))
    merge_seconds=time.perf_counter()-start
    sizes=np.bincount(labels.ravel());pixel_area=bx*by*float(np.prod(frame.pixel_size_um))
    locations=find_objects(labels);rows={};lookup=np.zeros(len(sizes),np.int32);next_label=1
    threshold=float(chi2.ppf(options.local_evidence_probability,3))
    # Ignore tiny domains before calculating annuli, but do not join/fill them.
    for old,section in enumerate(locations,1):
        if section is None or sizes[old]*pixel_area<options.minimum_region_area_um2:continue
        evidence=_local_evidence(measure,good,labels,old,section,measurement_covariance,options,
            (bx*frame.pixel_size_um[0],by*frame.pixel_size_um[1]))
        if evidence is None or evidence['local_evidence_statistic']<threshold:continue
        area=float(sizes[old]*pixel_area);ys,xs=section
        lookup[old]=next_label
        rows[next_label]={**evidence,'area_um2':area,'useful_area':area>=options.useful_area_um2,
            'centroid_px':[(evidence['centroid_block_xy'][0]+.5)*bx,(evidence['centroid_block_xy'][1]+.5)*by],
            'bbox_px':[xs.start*bx,ys.start*by,(xs.stop-xs.start)*bx,(ys.stop-ys.start)*by],
            'layer_number':None,'material_identity':None,'screening_role':'uncalibrated_color_plateau',
            'segmentation_method':'noise_whitened_statistical_region_merging',
            'boundary_resolution_um':[bx*frame.pixel_size_um[0],by*frame.pixel_size_um[1]],
            'source_component_label':old}
        next_label+=1
    kept=lookup[labels]
    full=np.zeros(contrast.shape[:2],np.int32)
    full[:kept.shape[0]*by,:kept.shape[1]*bx]=np.repeat(np.repeat(kept,by,axis=0),bx,axis=1)
    report={'schema':1,'method':'noise_whitened_statistical_region_merging','options':asdict(options),
        'block_shape_hw':list(block.shape[:2]),'block_size_px':[bx,by],
        'unanalysed_bottom_right_pixels':[contrast.shape[0]%by,contrast.shape[1]%bx],
        'block_noise_covariance':covariance.tolist(),'measurement_block_noise_covariance':measurement_covariance.tolist(),
        'separated_spatial_measurement_partition':separated,'all_color_domain_count':len(sizes)-1,
        'region_count':len(rows),'useful_region_count':sum(row['useful_area'] for row in rows.values()),
        'merge_seconds':merge_seconds,'elapsed_seconds':time.perf_counter()-start,
        'qualification':'Uncalibrated connected color plateaus, not verified material or thin flakes.',
        'noise_assumption':'Segmentation and measurement use separate native pixel partitions; demosaicing/spatial correlation can remain, so this is not an independent validation frame.',
        'literature':['https://doi.org/10.1109/TPAMI.2004.110','https://doi.org/10.1038/s41598-021-85159-9'],
        'predicate_note':'Measured Gaussian pooled-mean variant; published SRM distribution-free guarantee does not apply.'}
    return full,rows,report
