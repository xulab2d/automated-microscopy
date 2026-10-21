"""Independent pixel-first optical plateau proposals and reviewable quality sieves.

No acquisition calls. Original analyses and parent/child geometry stay separate.
Optical-model agreement is descriptive and never a material/layer assignment.
"""
from dataclasses import asdict,dataclass
from pathlib import Path
import hashlib
import json
import time

import cv2
import numpy as np
from scipy.ndimage import binary_propagation,distance_transform_edt,find_objects

from .analysis import tile_inputs,focus_provenance,_boundary_evidence,_facet_fraction
from .colors import AnalysisOptions
from .imaging import load_frame
from .measure_existing_regions import ExistingRegionOptions,measure_existing_regions
from .model import AcquisitionError,atomic_json
from .staged_features import _geometry
from .storage import managed_output_path
from .terrace_domain import ChipDomain,DomainExclusion,build_chip_domain,digest,normalize_supported
from .terrace_parents import load_parent_field
from .terrace_substrate import check_substrate_consistency


@dataclass(frozen=True)
class TerraceOptions:
    minimum_area_um2: float = 25.
    small_region_um2: float = 50.
    maximum_review_area_um2: float = 20000.
    source_floor_um2: float = 5.
    parent_floor_um2: float = 20.
    edge_margin_um: float = 12.
    atlas_pixel_um: float = 8.
    smoothing_um: float = .3
    interior_erosion_um: float = .5
    seed_sigma: float = 3.5
    growth_sigma: float = 2.
    minimum_seed_contrast: float = .008
    max_color_components: int = 6
    minimum_signal_z: float = 3.
    minimum_uniform_fraction: float = .65
    small_uniform_fraction: float = .8
    minimum_boundary_support: float = .45
    small_boundary_support: float = .65
    optical_bin_nm: float = 1.

    def __post_init__(self):
        if not np.isfinite(list(asdict(self).values())).all():raise ValueError('Nonfinite terrace options.')
        if not 0<self.source_floor_um2<=self.parent_floor_um2<=self.minimum_area_um2<=self.small_region_um2<self.maximum_review_area_um2:
            raise ValueError('Inconsistent physical region areas.')
        if min(self.atlas_pixel_um,self.smoothing_um,self.interior_erosion_um,self.minimum_seed_contrast,self.minimum_signal_z,self.optical_bin_nm)<=0 or self.edge_margin_um<0:
            raise ValueError('Invalid physical evidence scales.')
        if not 1<=self.max_color_components<=8 or not 0<self.growth_sigma<self.seed_sigma:
            raise ValueError('Invalid segmentation complexity.')
        if not 0<self.minimum_uniform_fraction<=self.small_uniform_fraction<=1 or not 0<self.minimum_boundary_support<=self.small_boundary_support<=1:
            raise ValueError('Invalid quality tiers.')


def largest_uniform_rectangle(mask,pixel_size_um=(1.,1.),origin_xy=(0,0)):
    """Exact largest all-True source-axis rectangle, not a bounding rectangle."""
    mask=np.asarray(mask,bool);heights=np.zeros(mask.shape[1],np.int32);best=(0,0,0,0,0)
    for y,row in enumerate(mask):
        heights=np.where(row,heights+1,0);stack=[]
        for x in range(len(heights)+1):
            height=int(heights[x]) if x<len(heights) else 0;start=x
            while stack and stack[-1][1]>height:
                left,previous=stack.pop();area=previous*(x-left)
                if area>best[0]:best=(area,left,y-previous+1,x-left,previous)
                start=left
            if height and (not stack or stack[-1][1]<height):stack.append((start,height))
    area,x,y,w,h=best;x+=origin_xy[0];y+=origin_xy[1]
    return {'bbox_px':[int(x),int(y),int(w),int(h)],'polygon_px':[[x,y],[x+w,y],[x+w,y+h],[x,y+h]],
        'area_um2':float(area*np.prod(pixel_size_um)),'width_um':float(w*pixel_size_um[0]),'height_um':float(h*pixel_size_um[1]),
        'orientation':'source_image_axes','maximum_scope':'Exact for source-axis rectangles; not rotation-optimized',
        'all_pixels_inside_eroded_uniform_region':True}


def _robust_noise(smooth,contrast,support):
    values=smooth[support][::max(1,int(support.sum()/25000))]
    centre=np.median(values,axis=0);distance=np.linalg.norm(values-centre,axis=1)
    core=values[distance<=np.percentile(distance,75)]
    covariance=np.cov(core.T)+np.eye(3)*.0015**2
    high=contrast-cv2.GaussianBlur(np.float32(contrast),(0,0),1)
    pixels=high[support][::max(1,int(support.sum()/25000))]
    raw=np.maximum(1.4826*np.median(abs(pixels-np.median(pixels,axis=0)),axis=0),.001)
    return centre,covariance,raw


def _color_components(values,covariance,maximum,seed):
    """Pixel RGB mixture, independent of the parent's average or optical prior."""
    eigen,vectors=np.linalg.eigh(covariance);whiten=(vectors/np.sqrt(np.maximum(eigen,1e-8)))@vectors.T
    sample=values[::max(1,len(values)//2000)][:2000]@whiten
    sample=np.float32(sample);best=None
    # Penalized residual in noise-whitened RGB; the noise floor discourages
    # partitioning an otherwise uniform plateau into tiny noise populations.
    for count in range(1,min(maximum,max(1,len(sample)//40))+1):
        cv2.setRNGSeed(seed)
        error,_,centres=cv2.kmeans(sample,count,None,(cv2.TERM_CRITERIA_EPS|cv2.TERM_CRITERIA_MAX_ITER,40,.01),1,cv2.KMEANS_PP_CENTERS)
        variance=max(error/(3*len(sample)),1.)
        criterion=3*len(sample)*np.log(variance)+4*count*np.log(len(sample))
        if best is None or criterion<best[0]:best=(criterion,centres,count)
    centre=best[1];labels=np.empty(len(values),np.int16)
    for begin in range(0,len(values),100000):
        part=values[begin:begin+100000]@whiten
        distances=((part[:,None,:]-centre[None,:,:])**2).sum(axis=2)
        labels[begin:begin+len(part)]=distances.argmin(axis=1)
    return labels,best[2],centre@np.linalg.inv(whiten)


def segment_plateaus(frame,contrast,valid,support,options=None,optical_provider=None,parent_labels=None):
    options=options or TerraceOptions();pixel_area=float(np.prod(frame.pixel_size_um))
    sigma=options.smoothing_um/min(frame.pixel_size_um)
    # Normalized convolution prevents invalid exterior zeros bleeding into chip.
    denominator=cv2.GaussianBlur(valid.astype(np.float32),(0,0),sigma)
    smooth=cv2.GaussianBlur(np.float32(contrast*valid[:,:,None]),(0,0),sigma)/np.maximum(denominator[:,:,None],1e-6)
    centre,covariance,raw_noise=_robust_noise(smooth,contrast,support)
    delta=smooth-centre;inverse=np.linalg.inv(covariance)
    mahal=np.einsum('...i,ij,...j->...',delta,inverse,delta,optimize=True)
    magnitude=np.linalg.norm(delta,axis=2)
    seeds=valid&(mahal>options.seed_sigma**2)&(magnitude>options.minimum_seed_contrast)
    allowed=valid&(mahal>options.growth_sigma**2)&(magnitude>options.minimum_seed_contrast/2)
    optical_classes=np.zeros(valid.shape,np.int16);optical_record={'status':'unavailable'};optical_maps={}
    if optical_provider is not None:
        classifier=getattr(optical_provider,'classify_pixels',None)
        if classifier is None:raise ValueError('Optical provider must expose the fast pre-grouping pixel classifier.')
        # Pixel hypotheses are computed BEFORE any connected object or mean.
        # The independent substrate-like population is omitted from expensive
        # queries; no material-whitelist gate deletes unmatched source pixels.
        white=float(frame.metadata.get('white_level',65535))
        candidate=valid&np.all(frame.rgb<white,axis=2)&(magnitude>options.minimum_seed_contrast/2)
        if parent_labels is not None:candidate&=parent_labels>0
        prediction=classifier(smooth[candidate],covariance)
        distance=np.asarray(prediction['min_mahalanobis_distance'])
        thickness=np.asarray(prediction['best_conditional_thickness_nm'])
        d0=np.sqrt(np.maximum(mahal[candidate],0))
        compatible=np.isfinite(thickness)&(distance<=4)&(d0*d0-distance*distance>=6.)
        classes=np.zeros(len(thickness),np.int16)
        classes[compatible]=np.minimum(32000,1+np.floor(thickness[compatible]/options.optical_bin_nm)).astype(np.int16)
        optical_classes[candidate]=classes
        for key in ('best_conditional_thickness_nm','min_mahalanobis_distance'):
            optical_maps[key]=np.full(valid.shape,np.nan,np.float32)
            optical_maps[key][candidate]=prediction[key]
        seeds|=optical_classes>0;allowed|=optical_classes>0
        optical_record={'status':'pre_grouping_conditional_pixel_classification','candidate_pixels':int(candidate.sum()),
            'conditionally_matching_pixels':int(compatible.sum()),'thickness_bin_nm':options.optical_bin_nm,
            'covariance':covariance.tolist(),'provider_provenance':getattr(optical_provider,'provenance',{}),
            'qualification':'Bins are conditional optical thickness hypotheses, not material or layer labels. Model-mismatched pixels remain unclassified contrast proposals.'}
    accepted_parents=parent_labels is not None
    if accepted_parents:
        if parent_labels.shape!=valid.shape:raise AcquisitionError('Accepted parent mask dimensions mismatch.')
        parent_labels=np.int32(parent_labels);foreground=parent_labels>0
        components=[]
        for label,section in enumerate(find_objects(parent_labels),1):
            if section is None:continue
            ys,xs=section;components.append((label,xs.start,ys.start,xs.stop-xs.start,ys.stop-ys.start,int(np.sum(parent_labels[section]==label))))
    else:
        foreground=binary_propagation(seeds,mask=allowed,structure=np.ones((3,3),bool))
        count,parent_labels,stats,_=cv2.connectedComponentsWithStats(np.uint8(foreground),8)
        components=[(parent,*map(int,stats[parent])) for parent in range(1,count)]
    result=np.zeros(valid.shape,np.int32);kept_parents=parent_labels.copy() if accepted_parents else np.zeros(valid.shape,np.int32);evidence={};next_label=1;next_parent=1
    for parent,x,y,w,h,parent_pixels in components:
        if parent_pixels*pixel_area<options.parent_floor_um2:continue
        local=(parent_labels[y:y+h,x:x+w]==parent)&valid[y:y+h,x:x+w]
        if not local.any():continue
        local_groups=np.full(local.shape,-1,np.int32);descriptors={};next_group=0
        local_optical=optical_classes[y:y+h,x:x+w]
        for optical_class in np.unique(local_optical[local]):
            if optical_class<=0:continue
            pixels=local&(local_optical==optical_class)
            local_groups[pixels]=next_group
            descriptors[next_group]={'pixel_classification':'conditional_optical_thickness_bin',
                'conditional_thickness_bin_nm':[(int(optical_class)-1)*options.optical_bin_nm,int(optical_class)*options.optical_bin_nm],
                'pixel_color_group_mean_rgb':np.mean(smooth[y:y+h,x:x+w][pixels],axis=0).tolist()}
            next_group+=1
        unmatched=local&(local_optical==0)
        if unmatched.any():
            values=smooth[y:y+h,x:x+w][unmatched]
            groups,number,means=_color_components(values,covariance,options.max_color_components,
                (x*73856093 ^ y*19349663 ^ w*83492791 ^ h)&0x7fffffff)
            local_groups[unmatched]=groups+next_group
            for j in range(number):descriptors[next_group+j]={'pixel_classification':'unclassified_contrast_population','pixel_color_group_mean_rgb':means[j].tolist()}
            next_group+=number
        parent_id=parent if accepted_parents else next_parent
        if not accepted_parents:kept_parents[y:y+h,x:x+w][local]=parent_id
        for group in range(next_group):
            parts,labels,pstats,_=cv2.connectedComponentsWithStats(np.uint8(local_groups==group),8)
            for part in range(1,parts):
                if pstats[part,cv2.CC_STAT_AREA]*pixel_area<options.source_floor_um2:continue
                result[y:y+h,x:x+w][labels==part]=next_label
                evidence[next_label]={'parent_instance_label':parent_id,'parent_area_um2':parent_pixels*pixel_area,
                    'pixel_color_group':group,'parent_color_group_count':next_group,**descriptors[group],
                    'segmentation_method':'Pre-grouping optical pixel hypotheses plus unmatched RGB populations, each split by spatial connectivity'}
                next_label+=1
        next_parent+=1
    return result,kept_parents,evidence,{'contrast':contrast,'smooth':smooth,'valid':valid,'support':support,
        'foreground':foreground,'noise_rgb':np.sqrt(np.diag(covariance)),'raw_noise_rgb':raw_noise,'substrate_covariance':covariance,
        'optical_pixel_classification':optical_record,'optical_maps':optical_maps,
        'parent_source':'checksummed_accepted_instances' if accepted_parents else 'experimental_global_hysteresis'}


def _localized_boundary(mask,contrast,pixel_size_um,noise_rgb,minimum_step):
    """Allow submicrometre contour localization error, not a diffuse ramp."""
    signed=distance_transform_edt(mask,sampling=(pixel_size_um[1],pixel_size_um[0]))-distance_transform_edt(~mask,sampling=(pixel_size_um[1],pixel_size_um[0]))
    gy,gx=np.gradient(signed,pixel_size_um[1],pixel_size_um[0])
    contours,_=cv2.findContours(np.uint8(mask),cv2.RETR_EXTERNAL,cv2.CHAIN_APPROX_NONE)
    points=max(contours,key=cv2.contourArea)[:,0,:];points=points[::max(1,len(points)//600)]
    normals=np.stack([gx[points[:,1],points[:,0]],gy[points[:,1],points[:,0]]],axis=1)
    normals/=np.maximum(np.linalg.norm(normals,axis=1,keepdims=True),1e-9)
    vectors=normals/np.asarray(pixel_size_um)
    def sample(p):
        return cv2.remap(np.float32(contrast),np.float32(p[:,0,None]),np.float32(p[:,1,None]),cv2.INTER_LINEAR,borderMode=cv2.BORDER_REFLECT_101)[:,0,:]
    supported=np.zeros(len(points),bool);steps=[]
    for offset in (-.5,0.,.5):
        centre=points+vectors*offset
        far=sample(centre+vectors)-sample(centre-vectors)
        near=sample(centre+.35*vectors)-sample(centre-.35*vectors)
        magnitude=np.linalg.norm(far,axis=1)
        localized=np.sum(near*far,axis=1)/np.maximum(magnitude,1e-9)>=.4*magnitude
        supported|=(magnitude>=minimum_step)&(np.linalg.norm(far/noise_rgb,axis=1)>=5)&localized
        steps.append(magnitude)
    return float(supported.mean()),float(np.median(np.max(steps,axis=0)))


def _region_quality(frame,labels,label,section,prepared,options):
    ys,xs=section;pad=int(np.ceil(2/min(frame.pixel_size_um)))+3
    x0=max(0,xs.start-pad);y0=max(0,ys.start-pad);x1=min(labels.shape[1],xs.stop+pad);y1=min(labels.shape[0],ys.stop+pad)
    mask=labels[y0:y1,x0:x1]==label;contrast=prepared['contrast'][y0:y1,x0:x1];smooth=prepared['smooth'][y0:y1,x0:x1]
    eroded=mask&(distance_transform_edt(np.pad(mask,1),sampling=(frame.pixel_size_um[1],frame.pixel_size_um[0]))[1:-1,1:-1]>options.interior_erosion_um)
    white=float(frame.metadata.get('white_level',np.iinfo(frame.rgb.dtype).max if frame.rgb.dtype.kind in 'ui' else 1.))
    eroded&=np.all(frame.rgb[y0:y1,x0:x1]<white,axis=2)
    if eroded.any():
        # Spatial mean uniformity tolerates camera noise while preserving real
        # holes/foreign labels. It does not fill those holes in the usable mask.
        radius=.5/min(frame.pixel_size_um)
        weights=cv2.GaussianBlur(eroded.astype(np.float32),(0,0),radius)
        local_average=cv2.GaussianBlur(smooth*eroded[:,:,None],(0,0),radius)/np.maximum(weights[:,:,None],1e-6)
        centre=np.median(local_average[eroded],axis=0);d=local_average-centre
        uniform=eroded&(np.einsum('...i,ij,...j->...',d,np.linalg.inv(prepared['substrate_covariance']),d,optimize=True)<=9.)
        spread=np.sqrt(np.mean((smooth[eroded]-centre)**2,axis=0))
        texture=float(np.median(np.linalg.norm((contrast-smooth)[eroded]/prepared['raw_noise_rgb'],axis=1)))
    else:uniform=eroded;spread=np.full(3,np.inf);texture=float('inf')
    # Choose a connected clean interior. Disconnected pieces cannot pool area.
    n,parts,stats,_=cv2.connectedComponentsWithStats(np.uint8(uniform),8)
    clean=parts==int(np.argmax(stats[1:,cv2.CC_STAT_AREA])+1) if n>1 else np.zeros(mask.shape,bool)
    rectangle=largest_uniform_rectangle(clean,frame.pixel_size_um,(x0,y0))
    contours,_=cv2.findContours(np.uint8(mask),cv2.RETR_EXTERNAL,cv2.CHAIN_APPROX_NONE)
    contour=max(contours,key=cv2.contourArea);facets,vertices=_facet_fraction(contour,frame.pixel_size_um)
    physical=np.float32(contour)*np.float32(frame.pixel_size_um)
    perimeter=cv2.arcLength(physical,True)
    circularity=float(np.clip(4*np.pi*mask.sum()*np.prod(frame.pixel_size_um)/max(perimeter**2,1e-9),0,1))
    outside_distance=distance_transform_edt(~mask,sampling=(frame.pixel_size_um[1],frame.pixel_size_um[0]))
    halo_ring=(outside_distance>=.5)&(outside_distance<=2.)&(labels[y0:y1,x0:x1]==0)&prepared['valid'][y0:y1,x0:x1]
    halo_rgb=np.median(smooth[halo_ring],axis=0).tolist() if halo_ring.sum()>=12 else None
    polygon=cv2.approxPolyDP(physical,.75,True)[:,0,:]
    segments=np.roll(polygon,-1,axis=0)-polygon;lengths=np.linalg.norm(segments,axis=1)
    long=lengths>=2.;angles=np.arctan2(segments[:,1],segments[:,0])
    alignment=float(abs(np.sum(lengths[long]*np.exp(6j*angles[long])))/max(float(lengths[long].sum()),1e-9)) if long.sum()>=3 else None
    boundary,step=_localized_boundary(mask,contrast,frame.pixel_size_um,prepared['raw_noise_rgb'],options.minimum_seed_contrast)
    return {'interior_erosion_um':options.interior_erosion_um,'eroded_area_um2':float(eroded.sum()*np.prod(frame.pixel_size_um)),
        'uniform_area_um2':float(clean.sum()*np.prod(frame.pixel_size_um)),
        'uniform_fraction':float(clean.sum()/max(eroded.sum(),1)),'uniform_spread_rgb':spread.tolist() if np.isfinite(spread).all() else None,
        'texture_noise_ratio':texture if np.isfinite(texture) else None,'boundary_support':boundary,'boundary_step':step,
        'facet_fraction':facets,'facet_vertices':vertices,'circularity':circularity,'outside_ring_contrast_rgb':halo_rgb,'sixfold_edge_alignment':alignment,
        'sixfold_evidence_scope':'Weak rotation-invariant 60/120-degree edge-direction concentration; not a required shape or crystal identity.',
        'usable_rectangle':rectangle,'boundary_localization_tolerance_um':.5,
        'quality_evidence_scope':'Independent optical geometry/texture evidence; engineering ranking, not a material probability'}


def analyze_terrace_frame(item,frame,domain,options=None,optical_provider=None,parent_labels=None,parent_rows=None,domain_masks=None):
    options=options or TerraceOptions();started=time.perf_counter()
    timings={};phase=time.perf_counter()
    valid,support,domain_report=domain_masks or domain.masks(item,frame,options.edge_margin_um)
    timings['domain']=time.perf_counter()-phase;phase=time.perf_counter()
    substrate_reference=getattr(domain,'qc',{}).get('substrate_consistency_reference',{})
    substrate_check=check_substrate_consistency(substrate_reference,frame,support,item['tile_id'])
    timings['chip_substrate_consistency']=time.perf_counter()-phase;phase=time.perf_counter()
    contrast,normalization=normalize_supported(frame,valid,support)
    timings['normalization']=time.perf_counter()-phase;phase=time.perf_counter()
    labels,parents,evidence,prepared=segment_plateaus(frame,contrast,valid,support,options,optical_provider,parent_labels)
    timings['pixel_classification_and_segmentation']=time.perf_counter()-phase;phase=time.perf_counter()
    if labels.max()>65535:raise AcquisitionError('Too many source plateaus for a uint16 instance map.')
    white=float(frame.metadata.get('white_level',65535));photometric_valid=valid&np.all(frame.rgb<white,axis=2)
    measurements,measurement_report=measure_existing_regions(frame,contrast,labels,photometric_valid,
        ExistingRegionOptions(minimum_area_um2=options.minimum_area_um2,interior_erosion_um=options.interior_erosion_um),
        additional_foreground=prepared['foreground']|~support)
    timings['local_measurement']=time.perf_counter()-phase;phase=time.perf_counter()
    # Score all valid region means together. This avoids rebuilding a model
    # tree per region while retaining each region's own measurement covariance.
    optical_by_label={}
    saturated_counts=np.bincount(labels.ravel(),weights=(~photometric_valid).ravel(),minlength=int(labels.max())+1)
    if optical_provider is not None:
        ids=[key for key,m in measurements.items() if m['measurement_status']=='measured' and not saturated_counts[key]]
        if ids:
            predictions=optical_provider(np.asarray([measurements[key]['contrast_rgb'] for key in ids]),
                np.asarray([measurements[key]['contrast_covariance'] for key in ids]))
            for i,key in enumerate(ids):
                optical_by_label[key]={k:v[i] if isinstance(v,np.ndarray) and v.ndim and v.shape[0]==len(ids) else v for k,v in predictions.items()}
    timings['optical_region_means']=time.perf_counter()-phase;phase=time.perf_counter()
    timings['geometry_and_quality']=0.
    focus,focus_limited=focus_provenance(item,frame);rows=[]
    parent_lookup={r['instance_label']:r for r in (parent_rows or [])}
    for label,section in enumerate(find_objects(labels),1):
        if section is None:continue
        phase=time.perf_counter()
        row=_geometry(labels,label,section,frame,item['transform'])
        assessed=row['area_um2']>=options.minimum_area_um2
        quality=_region_quality(frame,labels,label,section,prepared,options) if assessed else {
            'status':'not_assessed_below_review_area','interior_erosion_um':options.interior_erosion_um,
            'eroded_area_um2':None,'uniform_area_um2':None,'uniform_fraction':None,'uniform_spread_rgb':None,
            'texture_noise_ratio':None,'boundary_support':None,'boundary_step':None,'facet_fraction':None,'facet_vertices':None,
            'usable_rectangle':{'status':'not_computed','bbox_px':[0,0,0,0],'area_um2':0.,'width_um':0.,'height_um':0.,
                'maximum_scope':'Not measured below the review area; source geometry is retained'}}
        region=labels[section]==label
        saturated_fraction=float(np.mean(~photometric_valid[section][region]))
        measurement=measurements[label];small=row['area_um2']<options.small_region_um2
        reasons=[];quality_flags=[];measured=measurement['measurement_status']=='measured';signal=None
        if measured:
            c=np.asarray(measurement['contrast_rgb']);cov=np.asarray(measurement['contrast_covariance'])
            signal=float(np.sqrt(max(0,c@np.linalg.solve(cov,c))))
        else:reasons.append('local_substrate_measurement_unavailable')
        if row['area_um2']<options.minimum_area_um2:reasons.append('below_review_area')
        if row['area_um2']>options.maximum_review_area_um2:quality_flags.append('large_mixed_or_debris_region')
        if signal is None or signal<options.minimum_signal_z:reasons.append('contrast_unresolved')
        if assessed:
            if measurement.get('interior_fallback_to_full_mask'):quality_flags.append('no_reliable_eroded_measurement_interior')
            if quality['uniform_fraction']<(options.small_uniform_fraction if small else options.minimum_uniform_fraction):quality_flags.append('heterogeneous_or_disconnected_interior')
            if quality['boundary_support']<(options.small_boundary_support if small else options.minimum_boundary_support):quality_flags.append('weak_or_diffuse_boundary_or_defocus')
            if quality['usable_rectangle']['area_um2']<(4. if small else 8.):quality_flags.append('insufficient_clean_rectangle')
            if quality['texture_noise_ratio'] is None or quality['texture_noise_ratio']>(4. if small else 6.):quality_flags.append('textured_interior')
        else:quality_flags.append('quality_not_assessed_below_review_area')
        if saturated_fraction>0:quality_flags.append('saturated_pixels_excluded_from_photometry')
        if substrate_check['status']!='consistent_with_chip_reference':quality_flags.append('chip_substrate_'+substrate_check['status'])
        optical={'status':'unavailable','reason':'No calibrated or explicitly conditional RGB optical provider supplied.',
            'layer_assignment_allowed':False,'material_identity_confirmed':False}
        if saturated_fraction>0:
            optical={'status':'unavailable_saturated_region','reason':'Clipped channels do not determine optical thickness; unsaturated-subset contrast is separate.',
                'layer_assignment_allowed':False,'material_identity_confirmed':False}
        if label in optical_by_label:
            optical=optical_by_label[label]
            thickness=prepared['optical_maps']['best_conditional_thickness_nm'][section][region]
            distance=prepared['optical_maps']['min_mahalanobis_distance'][section][region]
            finite=np.isfinite(thickness)&np.isfinite(distance)
            optical['pixel_evidence']={'classified_pixels':int(finite.sum()),'source_pixels':int(region.sum()),
                'conditional_thickness_quantiles_nm':np.percentile(thickness[finite],[10,50,90]).tolist() if finite.any() else None,
                'median_model_distance':float(np.median(distance[finite])) if finite.any() else None,
                'classification_scope':'Reused pre-grouping smooth fitted-substrate pixel hypotheses. Local-annulus-corrected region-mean evidence is separate; no per-pixel interval or layer assignment.'}
        clean_area=quality['usable_rectangle']['area_um2']
        score=float(quality['uniform_fraction']**(2 if small else 1)*quality['boundary_support']*
            (.75+.25*quality['facet_fraction'])*np.sqrt(clean_area/max(row['area_um2'],1e-9))*np.log1p(clean_area)/
            (1+max(0,(quality['texture_noise_ratio'] or 10)-2))) if assessed else 0.
        if small and quality_flags:score*=.25
        geometry_score=score;optical_factor=1.;dark_round_halo=0.
        if label in optical_by_label:
            distance=float(optical['min_mahalanobis_distance']);improvement=float(optical.get('log_likelihood_improvement_vs_substrate',0))
            optical_factor=.75+.5*np.exp(-.5*min(distance**2,1400))*float(np.clip(improvement/6.,0,1))
        if assessed and measured:
            darkness=float(np.clip(np.min(c)/.5,0,1));circularity=quality['circularity']
            halo=np.asarray(quality['outside_ring_contrast_rgb']) if quality['outside_ring_contrast_rgb'] is not None else np.zeros(3)
            opposing_halo=float(np.clip(-np.mean(halo)/.03,0,1))
            dark_round_halo=darkness*max(float(np.clip((circularity-.45)/.45,0,1)),opposing_halo)
        debris_factor=1-.35*dark_round_halo
        score*=optical_factor*debris_factor
        optical_selected=bool(not reasons and label in optical_by_label and
            substrate_check['status']=='consistent_with_chip_reference' and
            float(optical['min_mahalanobis_distance'])<=4. and
            float(optical.get('log_likelihood_improvement_vs_substrate',0))>=3.)
        row.update(evidence[label]);row.update(id='t3-'+hashlib.sha256((item['key']+'|'+str(label)+'|'+item.get('analysis_fingerprint','')).encode()).hexdigest()[:14],
            source_image='tiles/'+item['tile_id']+'.tiff',source_tile=item['tile_id'],tile_key=item['key'],tile_id=item['tile_id'],chip_id=item['chip_id'],
            geometry_method='Pixel-first connected color plateau; exact child mask and separate parent mask',
            parent_id=parent_lookup.get(evidence[label]['parent_instance_label'],{}).get('id') or 'p3-'+hashlib.sha256((item['key']+'|'+str(evidence[label]['parent_instance_label'])+'|'+item.get('analysis_fingerprint','')).encode()).hexdigest()[:14],contrast_rgb=measurement.get('contrast_rgb'),
            local_contrast_measurement=measurement,quality_evidence=quality,optical_model_evidence=optical,
            focus_status=focus,focus_limited=focus_limited,saturated_fraction=saturated_fraction,
            selected_for_review=not reasons,disposition='rejected' if reasons else 'candidate',
            conditional_optical_selected=optical_selected,
            conditional_optical_sieve={'selected':optical_selected,'maximum_model_distance':4.,'minimum_log_likelihood_improvement_vs_substrate':3.,
                'requires_reliable_local_contrast':True,'layer_assignment_allowed':False,
                'chip_substrate_status':substrate_check['status'],
                'scope':'Conditional model-compatible shortlist, separate from broad source features. Not material identification or a calibrated thin-layer label.'},
            proposal_stage='ineligible' if reasons else 'small_plateau_review' if small else 'plateau_review',
            filter_reasons=reasons,quality_flags=quality_flags,evidence_warnings=quality_flags+(['focus_not_locally_validated'] if focus_limited else [])+(['chip_enclosure_unverified'] if domain.geometry_status!='observed_enclosed' else []),
            quality_score=score,ranking_score=score,ranking_tier='small_stricter' if small else 'standard',
            ranking_components={'geometry_score':geometry_score,'conditional_optical_factor':float(optical_factor),
                'dark_round_or_opposed_halo_evidence':dark_round_halo,'bounded_debris_factor':debris_factor,
                'scope':'Engineering ranking only. Uncalibrated optical agreement is bounded to 0.75–1.25; dark/round/halo evidence is not a dust label.'},
            focus_evidence_scope='Saved focus evidence concerns the tile centre; tilted specimens may remain defocused within this field. Diffuse boundaries affect rank only.',
            useful_area_um2=quality['usable_rectangle']['area_um2'],layer_number=None,chemical_identity_confirmed=False,
            review_role='saturated_feature_review' if saturated_fraction>0 else 'unclassified_optical_plateau',priority='review_required' if not reasons else 'rejected',
            sieve_results=[{'name':'pixel_plateau','status':'pass','reason':'Connected pixels; not parent mean classification'},
                {'name':'independent_substrate','status':'pass' if measured else 'unmeasured','reason':measurement['reason']},
                {'name':'physical_area','status':'pass' if row['area_um2']>=options.minimum_area_um2 else 'reject','minimum_area_um2':options.minimum_area_um2},
                {'name':'quality_tier','status':'ranking_only','tier':'small_stricter' if small else 'standard','flags':quality_flags},
                {'name':'optical_model','status':'descriptive_only','layer_assignment_allowed':False}])
        rows.append(row)
        timings['geometry_and_quality']+=time.perf_counter()-phase
    return rows,{'instances':np.uint16(labels),'parents':np.uint16(parents),'valid':valid,'substrate_support':support},{
        'domain':domain_report,'normalization':normalization,'measurement':measurement_report,'focus':focus,'focus_limited':focus_limited,
        'chip_substrate_consistency':substrate_check,
        'elapsed_seconds':time.perf_counter()-started,'timing_seconds':timings,'all_masks_preserved_before_quality_sieves':True,
        'optical_pixel_classification':prepared['optical_pixel_classification'],
        'parent_source':prepared['parent_source'],
        'qualification':'Uncalibrated optical plateau proposals. No material or layer identity assigned.'}


def _plain(value):
    if isinstance(value,dict):return {k:_plain(v) for k,v in value.items()}
    if isinstance(value,(list,tuple)):return [_plain(v) for v in value]
    if isinstance(value,np.ndarray):return _plain(value.tolist())
    if isinstance(value,np.generic):return _plain(value.item())
    if isinstance(value,float) and not np.isfinite(value):return None
    return value


def analyze_terraces(folder,output,options=None,tile_ids=None,optical_provider=None,callback=None,parent_analysis=None,experimental_global=False):
    """Write an independent versioned result; no all-dataset run is implicit."""
    if not tile_ids:raise ValueError('Explicit representative tile IDs are required for this initial pipeline.')
    folder=Path(folder).resolve();out=managed_output_path(output);options=options or TerraceOptions()
    parent_analysis=None if experimental_global else Path(parent_analysis or folder/'analysis-staged-features').resolve()
    if out==folder or out.is_relative_to(folder/'tiles'):raise ValueError('Derived output cannot overwrite acquisition.')
    if out.exists() and any(out.iterdir()):raise FileExistsError('Use a new independent output folder; existing results are preserved.')
    config={'algorithm':'pixel-first-optical-terraces-v3','options':asdict(options),
        'sources':{name:digest(Path(__file__).with_name(name)) for name in ('terrace_analysis.py','terrace_domain.py','terrace_parents.py','terrace_substrate.py','measure_existing_regions.py')},
        'parent_source':'experimental_global_hysteresis' if experimental_global else 'checksummed_accepted_instances',
        'parent_analysis_summary_sha256':digest(parent_analysis/'analysis.json') if parent_analysis else None,
        'optical_provider_supplied':optical_provider is not None,'optical_provider_provenance':getattr(optical_provider,'provenance',{})}
    fingerprint=hashlib.sha256(json.dumps(config,sort_keys=True).encode()).hexdigest()
    domain=build_chip_domain(folder,options);items=[i for i in tile_inputs(folder) if i['tile_id'] in set(tile_ids)]
    if set(tile_ids)!={i['tile_id'] for i in items}:raise ValueError('Unknown representative tile IDs.')
    out.mkdir(parents=True);(out/'fields').mkdir();atomic_json(out/'pipeline.json',{**config,'fingerprint':fingerprint})
    source_archive=out/'source-code';source_archive.mkdir()
    for name,expected in config['sources'].items():
        content=Path(__file__).with_name(name).read_bytes()
        if hashlib.sha256(content).hexdigest()!=expected:raise RuntimeError('Source code changed while initializing the analysis.')
        (source_archive/name).write_bytes(content)
    atomic_json(out/'chip-domain.json',domain.qc)
    if domain.preview is not None:cv2.imwrite(str(out/'chip-domain.jpg'),domain.preview)
    tiles=[];catalog=[];parent_catalog=[];excluded=[];failures=[];started=time.perf_counter()
    for item in items:
        item['analysis_fingerprint']=fingerprint;key=item['key'];slug=key.replace('/','__')
        try:
            source_hashes={suffix:digest(item['path'].with_suffix(suffix)) for suffix in ('.tiff','.json')}
            item['source_hashes']=source_hashes
            record=item['manifest']['completed'][item['tile_id']]
            for suffix,value in source_hashes.items():
                if record.get('hashes',{}).get('tiles/'+item['tile_id']+suffix)!=value:raise AcquisitionError('Source acquisition checksum mismatch.')
            frame=load_frame(item['path']);item['transform'].validate_frame(frame)
            accepted_labels=accepted_rows=parent_provenance=None
            domain_masks=domain.masks(item,frame,options.edge_margin_um) if parent_analysis else None
            if parent_analysis:accepted_labels,accepted_rows,parent_provenance=load_parent_field(parent_analysis,item,frame)
            rows,maps,report=analyze_terrace_frame(item,frame,domain,options,optical_provider,accepted_labels,accepted_rows,domain_masks)
            mask_name=slug+'.instances.png';parent_name=slug+'.parents.png'
            for name,array in ((mask_name,maps['instances']),(parent_name,maps['parents'])):
                if not cv2.imwrite(str(out/name),array):raise OSError('Mask write failed')
            if accepted_labels is not None:
                import shutil
                if not np.array_equal(maps['parents'],accepted_labels):raise AcquisitionError('Accepted parent geometry changed.')
                shutil.copyfile(parent_analysis/parent_provenance['instance_mask_relative_path'],out/parent_name)
                if digest(out/parent_name)!=parent_provenance['instance_mask_sha256']:raise AcquisitionError('Copied parent mask checksum mismatch.')
            np.savez_compressed(out/(slug+'.support.npz'),valid=maps['valid'],substrate_support=maps['substrate_support'])
            tile={'key':key,'tile_id':item['tile_id'],'chip_id':item['chip_id'],'source':'tiles/'+item['tile_id']+'.tiff',
                'position':asdict(frame.position),'shape':list(frame.rgb.shape[:2]),'pixel_size_um':list(frame.pixel_size_um),
                'sha256':hashlib.sha256(frame.rgb.tobytes()).hexdigest(),'instance_mask':mask_name,'parent_mask':parent_name,
                'observations_file':'fields/'+slug+'.json','proposal_count':len(rows),'selected_count':sum(r['selected_for_review'] for r in rows),
                'parent_count':len(accepted_rows or []),'conditional_optical_count':sum(r['conditional_optical_selected'] for r in rows),
                'rejected_count':sum(not r['selected_for_review'] for r in rows),'focus_limited':report['focus_limited']}
            saved={'schema':1,'fingerprint':fingerprint,'source_identity':hashlib.sha256(json.dumps(source_hashes,sort_keys=True).encode()).hexdigest(),
                'tile':tile,'rows':rows,'parent_rows':accepted_rows or [],'accepted_parent_provenance':parent_provenance,
                'mask_sha256':digest(out/mask_name),'report':report,
                'provenance':{'source_files_sha256':source_hashes,'context':item['manifest'].get('context'),'acquisition_fingerprint':item['manifest'].get('fingerprint')}}
            atomic_json(out/tile['observations_file'],_plain(saved));tiles.append(tile);catalog.extend(r for r in rows if r['selected_for_review'])
            parent_catalog.extend(accepted_rows or [])
        except DomainExclusion as error:excluded.append({'tile_key':key,'status':'excluded','kind':error.kind,'reason':str(error)})
        except (AcquisitionError,ValueError,OSError,cv2.error) as error:failures.append({'tile_key':key,'error':str(error)})
        summary={'schema':2,'backend':'pixel_first_terraces_v3','dataset_id':fingerprint[:20],'tiles':tiles,'failed_tiles':failures,
            'excluded_tiles':excluded,'completed_tiles':len(tiles),'total_tiles':len(items),'raw_regions':sum(t['proposal_count'] for t in tiles),
            'candidate_count':len(catalog),'selected_observation_count':len(catalog),'catalog_deduplicated':False,
            'parent_count':len(parent_catalog),'conditional_optical_count':sum(r['conditional_optical_selected'] for r in catalog),
            'analysis_state':'complete' if len(tiles)+len(excluded)+len(failures)==len(items) else 'analysing',
            'validation_status':'experimental_optical_plateaus','warning':'Representative pilot only. No material/thickness validation. Catalog contains source observations; overlap may repeat a feature.'}
        atomic_json(out/'analysis.json',summary);atomic_json(out/'flakes.json',_plain({'flakes':catalog,'dataset_id':fingerprint[:20]}))
        atomic_json(out/'parent-catalog.json',{'rows':parent_catalog,'scope':'Accepted original broad source feature observations, identities and geometry preserved; not material labels.'})
        atomic_json(out/'conditional-optical.json',_plain({'rows':[r for r in catalog if r['conditional_optical_selected']],
            'scope':'Conditional optical model-compatible review subset, not a calibrated thin-flake identity.'}))
        atomic_json(out/'observations.json',{'schema':2,'storage':'per_tile_shards','shards':[{'tile_key':t['key'],'file':t['observations_file'],'count':t['proposal_count']} for t in tiles]})
        atomic_json(out/'analysis-progress.json',{'state':summary['analysis_state'],'completed_tiles':len(tiles),'total_tiles':len(items),
            'excluded_tiles':len(excluded),'failed_tiles':len(failures),'elapsed_seconds':time.perf_counter()-started,'updated_at':time.time()})
        if callback:callback(key,summary)
    return summary


def write_terrace_review(output,source_folder):
    """Compact portable pilot review, using copied JPEGs but never raw copies."""
    import shutil
    out=managed_output_path(output);source=Path(source_folder)
    summary=json.loads((out/'analysis.json').read_text(encoding='utf-8'));fields=[]
    preview_dir=out/'review-previews';preview_dir.mkdir(exist_ok=True)
    entries={t['tile_id']:t for t in summary['tiles']}
    outcomes={r['tile_key'].split('/')[-1]:r for r in summary.get('excluded_tiles',[])+summary.get('failed_tiles',[])}
    for tile_id in sorted(set(entries)|set(outcomes)):
        image=source/'tiles'/(tile_id+'.jpg');target=preview_dir/image.name
        shutil.copyfile(image,target)
        if digest(image)!=digest(target):raise OSError('Review preview verification failed')
        if tile_id in entries:
            data=json.loads((out/entries[tile_id]['observations_file']).read_text(encoding='utf-8'))
            fields.append({'id':tile_id,'image':'review-previews/'+image.name,'shape':data['tile']['shape'],
                'rows':data['rows'],'parent_rows':data.get('parent_rows',[]),'report':data['report'],'status':'analysed'})
        else:
            fields.append({'id':tile_id,'image':'review-previews/'+image.name,'rows':[],
                'status':outcomes[tile_id].get('status','failed'),'outcome':outcomes[tile_id]})
    payload=json.dumps(_plain(fields),allow_nan=False,separators=(',',':')).replace('<','\\u003c')
    page='''<!doctype html><html lang="en"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>Pixel-first plateau pilot</title><style>body{background:#0b131d;color:#e7eff5;font:14px system-ui;margin:0;padding:18px}button,select{background:#193246;color:inherit;border:1px solid #4d6b7a;padding:8px;border-radius:5px}h1{font-size:22px}a{color:#7ce0b4}.row{display:flex;gap:12px;flex-wrap:wrap;align-items:center}.layout{display:grid;grid-template-columns:minmax(0,1fr) 350px;gap:18px;margin-top:15px}canvas{width:100%;max-height:72vh;object-fit:contain;background:#070c12}pre{white-space:pre-wrap;overflow-wrap:anywhere;font-size:12px;max-height:48vh;overflow:auto}.list{max-height:40vh;overflow:auto}.list button{display:block;width:100%;text-align:left;margin:4px 0}.note{color:#cdbb92}.muted{color:#a3b6c5}@media(max-width:850px){.layout{grid-template-columns:1fr}}</style><h1>Pixel-first plateau pilot</h1><p class="note">Experimental optical regions. Review eligibility is not MoTe₂ identification or a layer assignment. Existing parent masks are unchanged. Internal terraces and the conditional optical shortlist are separate views. Optical-model agreement does not establish material or layer identity. Rectangles are source-axis usable-area lower bounds.</p><div class="row"><select id="field"></select><select id="mode"><option value="parents">Existing source boundaries</option><option value="terraces">Internal terraces</option><option value="conditional">Conditional optical shortlist</option></select><select id="filter"><option value="all">All source plateaus</option><option value="selected">Review eligible</option><option value="rejected">Rejected</option></select><label><input id="outlines" type="checkbox" checked>Outlines</label><label><input id="rectangles" type="checkbox">Clean rectangles</label><button id="full">Full image</button><a id="raw" target="_blank">Open original preview</a></div><p id="status" class="muted"></p><div class="layout"><div><canvas id="canvas"></canvas><p class="muted">Choose a source plateau to inspect its crop and independent reasons. Green: eligible · amber: rejected.</p></div><aside><div id="list" class="list"></div><pre id="detail"></pre></aside></div><script>const F='''+payload+''';const $=id=>document.getElementById(id),canvas=$('canvas'),ctx=canvas.getContext('2d');let field,image,selected=null,view=null;function rows(){const mode=$('mode').value,source=mode==='parents'?(field.parent_rows||[]):field.rows;return source.filter(r=>(mode!=='conditional'||r.conditional_optical_selected)&&($('filter').value==='all'||($('filter').value==='selected'?r.selected_for_review:!r.selected_for_review))).sort((a,b)=>(b.ranking_score??b.quality_score??0)-(a.ranking_score??a.quality_score??0))}function draw(){if(!image)return;const [h,w]=field.shape||[image.naturalHeight,image.naturalWidth];canvas.width=w;canvas.height=h;const v=view||[0,0,w,h];ctx.drawImage(image,v[0]*image.naturalWidth/w,v[1]*image.naturalHeight/h,v[2]*image.naturalWidth/w,v[3]*image.naturalHeight/h,0,0,w,h);ctx.save();ctx.scale(w/v[2],h/v[3]);ctx.translate(-v[0],-v[1]);for(const r of rows()){ctx.strokeStyle=r===selected?'#71d8ff':r.selected_for_review?'#70ffac':'#f4b77b';ctx.lineWidth=(r===selected?3:1.5)*v[2]/w;if($('outlines').checked){ctx.beginPath();for(const p of [r.polygon_px,...(r.holes_px||[])]){if(!p.length)continue;ctx.moveTo(...p[0]);p.slice(1).forEach(q=>ctx.lineTo(...q));ctx.closePath()}ctx.stroke()}if($('rectangles').checked&&r.quality_evidence?.usable_rectangle?.bbox_px){ctx.strokeRect(...r.quality_evidence.usable_rectangle.bbox_px)}}ctx.restore()}function inspect(r){selected=r;const b=r.bbox_px,[h,w]=field.shape,vw=Math.min(w,Math.max(180,b[2]*1.7,b[3]*1.7*w/h)),vh=vw*h/w;view=[Math.max(0,Math.min(w-vw,b[0]+b[2]/2-vw/2)),Math.max(0,Math.min(h-vh,b[1]+b[3]/2-vh/2)),vw,vh];$('detail').textContent=JSON.stringify({id:r.id,decision:r.selected_for_review?'Review eligible':'Rejected',reasons:r.filter_reasons,parent_id:r.parent_id,parent_area_um2:r.parent_area_um2,plateau_area_um2:r.area_um2,quality:r.quality_evidence,measurement:r.local_contrast_measurement,optical:r.optical_model_evidence,warnings:r.evidence_warnings},null,2);draw()}function listing(){const box=$('list');box.replaceChildren();for(const r of rows()){const b=document.createElement('button');b.textContent=(r.selected_for_review?'Eligible · ':'Rejected · ')+r.area_um2.toFixed(1)+' µm² · '+r.id+' · '+r.filter_reasons.join(', ');b.onclick=()=>inspect(r);box.append(b)}draw()}async function load(){field=F[Number($('field').value)];image=new Image();image.src=field.image;await image.decode();selected=null;view=null;$('raw').href=field.image;$('status').textContent=field.id+' · '+field.status+' · '+(field.parent_rows||[]).length+' preserved source features · '+field.rows.length+' internal terraces · '+field.rows.filter(r=>r.conditional_optical_selected).length+' conditional optical matches';$('detail').textContent=JSON.stringify(field.outcome||field.report.domain,null,2);listing()}F.forEach((f,i)=>{const o=document.createElement('option');o.value=i;o.textContent=f.id+' · '+f.status;$('field').append(o)});$('field').onchange=load;$('mode').onchange=()=>{selected=null;view=null;listing()};$('filter').onchange=listing;$('outlines').onchange=draw;$('rectangles').onchange=draw;$('full').onclick=()=>{view=null;draw()};if(F.length)load();</script></html>'''
    (out/'review.html').write_text(page,encoding='utf-8')
    return out/'review.html'
