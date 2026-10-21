"""CPU pixelwise material terraces with reviewable, independent sieves.

All supplied parent masks are returned unchanged. No hull filling, morphological
closing, or contour redraw can bridge islands/holes. A color mismatch remains an
unknown optical feature; it is never reported as proof that no flake is present.
"""
from dataclasses import asdict, dataclass
import time
import cv2
import numpy as np
from scipy.ndimage import binary_propagation, find_objects
from scipy.stats import chi2

from .material_contrast import MaterialContrastClassifier, covariance


@dataclass(frozen=True)
class MaterialTerraceOptions:
    smoothing_um: float = .3
    interior_erosion_um: float = .3
    seed_quantile: float = .999
    growth_quantile: float = .99
    source_floor_um2: float = 1.
    useful_area_um2: float = 25.
    useful_width_um: float = 3.
    maximum_stat_pixels: int = 20000
    patch_um: float = 2.
    merge_material_populations: bool = False
    preserve_parent_regions: bool = False

    def __post_init__(self):
        if type(self.merge_material_populations) is not bool:raise ValueError('Material population merging must be boolean.')
        if type(self.preserve_parent_regions) is not bool:raise ValueError('Parent-region preservation must be boolean.')
        if self.preserve_parent_regions and self.merge_material_populations:raise ValueError('Choose image regions or material population merging, not both.')
        if not np.isfinite(list(asdict(self).values())).all():raise ValueError('Finite segmentation options required.')
        if self.smoothing_um<0 or self.interior_erosion_um<0 or not 0<self.growth_quantile<self.seed_quantile<1:
            raise ValueError('Invalid smoothing or Gaussian seed/growth quantiles.')
        if not 0<self.source_floor_um2<=self.useful_area_um2 or self.useful_width_um<=0 or self.patch_um<=0 or self.maximum_stat_pixels<100:
            raise ValueError('Invalid physical sizes or statistics sample cap.')


def _geometry(mask,origin,pixel_size):
    contours,hierarchy=cv2.findContours(np.uint8(mask),cv2.RETR_CCOMP,cv2.CHAIN_APPROX_SIMPLE)
    if not contours:return {'polygon_px':[],'holes_px':[]}
    hierarchy=hierarchy[0];outer=max((i for i,h in enumerate(hierarchy) if h[3]<0),key=lambda i:cv2.contourArea(contours[i]))
    shift=np.array(origin);polygon=(contours[outer][:,0,:]+shift).tolist()
    holes=[(contour[:,0,:]+shift).tolist() for i,contour in enumerate(contours) if hierarchy[i][3]==outer]
    points=contours[outer][:,0,:].astype(float)*pixel_size
    width=min(cv2.minAreaRect(np.float32(points))[1]) if len(points)>=3 else 0.
    return {'polygon_px':polygon,'holes_px':holes,'bounding_width_um':float(width)}


def _patch_statistics(values,mask,section,pixel_size,patch_um):
    ys,xs=np.nonzero(mask);yp=int(max(1,round(patch_um/pixel_size[1])));xp=int(max(1,round(patch_um/pixel_size[0])))
    keys=np.column_stack([(ys+section[0].start)//yp,(xs+section[1].start)//xp])
    _,index=np.unique(keys,axis=0,return_inverse=True);count=np.bincount(index)
    sums=np.stack([np.bincount(index,weights=values[:,c]) for c in range(3)],axis=1)
    means=sums/count[:,None]
    return means,count


def segment_material_terraces(contrast_rgb, valid_mask, substrate_mask, noise_covariance,
                             pixel_size_um, classifier, *, parent_labels=None, options=None,
                             saturated_mask=None, background_systematic_covariance=None, check_cancel=None):
    """Return exact terrace labels, untouched parent labels, all regions and maps.

    Inputs are *already normalized* raw linear RGB contrast and independently
    recorded substrate samples. valid_mask is geometric domain; substrate_mask
    only estimates background/noise and never excludes differently colored flakes.
    noise_covariance is measured raw-pixel covariance. It is not divided by area.
    Large-region mean uncertainty uses independent spatial patches, with caller
    background systematic covariance added without pixel-count division.
    """
    started=time.perf_counter();options=options or MaterialTerraceOptions()
    if options.preserve_parent_regions and parent_labels is None:raise ValueError('Image-region preservation requires supplied parent labels.')
    check=check_cancel or (lambda:None);check()
    if not isinstance(classifier,MaterialContrastClassifier):raise TypeError('Supply MaterialContrastClassifier with explicit context/response.')
    contrast=np.asarray(contrast_rgb,np.float32);valid=np.asarray(valid_mask,bool);support=np.asarray(substrate_mask,bool)
    pixel=np.asarray(pixel_size_um,float)
    if contrast.ndim!=3 or contrast.shape[2]!=3 or valid.shape!=contrast.shape[:2] or support.shape!=valid.shape or not np.isfinite(contrast[valid]).all():
        raise ValueError('Matching finite contrast/domain/substrate arrays required.')
    if pixel.shape!=(2,) or not np.isfinite(pixel).all() or np.any(pixel<=0):raise ValueError('Positive calibrated pixel sizes required.')
    contrast=np.where(valid[:,:,None],contrast,0).astype(np.float32)
    raw_cov=covariance(noise_covariance);systematic=np.zeros((3,3)) if background_systematic_covariance is None else covariance(background_systematic_covariance,'background systematic covariance',False)
    saturated=np.zeros_like(valid) if saturated_mask is None else np.asarray(saturated_mask,bool)
    if saturated.shape!=valid.shape:raise ValueError('Saturation mask shape mismatch.')
    support=support&valid&~saturated
    if support.sum()<100:raise ValueError('At least100 independent recorded substrate pixels required; no inferred zero-background pass.')
    sigma=options.smoothing_um/pixel
    if options.smoothing_um:
        denominator=cv2.GaussianBlur(valid.astype(np.float32),(0,0),float(sigma[0]),sigmaY=float(sigma[1]))
        smooth=cv2.GaussianBlur(np.where(valid[:,:,None],contrast,0).astype(np.float32),(0,0),float(sigma[0]),sigmaY=float(sigma[1]))/np.maximum(denominator[:,:,None],1e-8)
        # Require substrate samples to be clear of the unsupported/sample edges.
        radius=int(np.ceil(max(sigma)*3));kernel=cv2.getStructuringElement(cv2.MORPH_ELLIPSE,(2*radius+1,2*radius+1))
        noise_support=cv2.erode(np.uint8(support),kernel)>0
    else:smooth=contrast.copy();noise_support=support
    background_sample_count=int(noise_support.sum())
    if background_sample_count<100:raise ValueError('Insufficient clear substrate support at the configured smoothing scale.')
    sample=smooth[noise_support][::max(1,int(background_sample_count/options.maximum_stat_pixels))]
    centre=np.median(sample,axis=0)
    if np.any(1-centre<=0):raise ValueError('Invalid substrate-relative denominator.')
    # Correct local Weber coordinates, including denominator, rather than simple
    # mean subtraction. The same transform is used for material and null pixels.
    normalized=(smooth-centre)/(1-centre)
    from sklearn.covariance import OAS
    fitted=OAS().fit((sample-centre)/(1-centre)).covariance_
    numeric=np.eye(3)*max(float(np.trace(raw_cov))*1e-8,1e-16)
    Jnull=np.diag(1/(1-centre))
    cov=fitted+Jnull@systematic@Jnull.T+numeric;covariance(cov,'smoothed measured substrate covariance')
    timing_normalize=time.perf_counter()-started
    check();maps=classifier.classify(normalized[valid&~saturated],cov);check()
    class_map=np.full(valid.shape,-2,np.int16);material_map=np.zeros_like(valid);debris_map=np.zeros_like(valid)
    candidate=valid&~saturated;class_map[candidate]=maps['class_index'];material_map[candidate]=maps['material_compatible'];debris_map[candidate]=maps['debris_reference_compatible']
    null_distance=np.zeros(valid.shape,np.float32);null_distance[candidate]=maps['substrate_distance']
    seeds=candidate&(null_distance**2>chi2.ppf(options.seed_quantile,3))
    growth=candidate&(null_distance**2>chi2.ppf(options.growth_quantile,3))
    # Full-frame classifier scratch is not needed during per-region work.
    del maps,normalized,smooth
    if parent_labels is None:
        foreground=binary_propagation(seeds,mask=growth,structure=np.array([[0,1,0],[1,1,1],[0,1,0]],bool))
        _,parents=cv2.connectedComponents(np.uint8(foreground),connectivity=4)
        provenance='new_noise_qualified_connected_features'
    else:
        parents=np.asarray(parent_labels)
        if parents.shape!=valid.shape or not np.issubdtype(parents.dtype,np.integer) or np.any(parents<0):raise ValueError('Nonnegative integer parent labels matching source pixels required.')
        parents=parents.copy();foreground=(parents>0)&valid
        provenance='supplied_parent_labels_preserved_exactly'
        if options.preserve_parent_regions and np.any((parents>0)&(~valid|saturated)):
            raise ValueError('Image regions must be inside the valid unsaturated domain.')
    timing_classify=time.perf_counter()-started-timing_normalize
    gx=cv2.Sobel(contrast,cv2.CV_32F,1,0,ksize=3)/8;gy=cv2.Sobel(contrast,cv2.CV_32F,0,1,ksize=3)/8
    raw_gradient=np.sqrt((gx*gx+gy*gy).sum(axis=2))
    del gx,gy
    inverse_raw_cov=np.linalg.inv(raw_cov)
    labels=np.zeros(valid.shape,np.int32);regions=[];next_label=1;area_pixel=float(np.prod(pixel))
    rectangle_seconds=0.
    for parent,section in enumerate(find_objects(parents),1):
        check()
        if section is None:continue
        local=(parents[section]==parent)&valid[section]
        semantic=class_map[section]
        if options.preserve_parent_regions:
            # The independently observed image mask defines geometry. Optical
            # classes (including null/unknown pixels) are measurements only.
            semantic=np.full(semantic.shape,-4,dtype=np.int16)
        elif options.merge_material_populations:
            # Geometry may span adjacent compatible optical bins, without
            # asserting a common thickness. Preserve the original class map.
            semantic=semantic.copy()
            semantic[material_map[section]]=-3
        # Null pixels stay visible in the preserved parent mask; they do not become
        # giant terrace candidates. Saturated parent pixels remain unmeasured.
        for class_index in np.unique(semantic[local]):
            if class_index in (0,-2):continue
            count,parts,stats,_=cv2.connectedComponentsWithStats(np.uint8(local&(semantic==class_index)),connectivity=4)
            for part in range(1,count):
                check()
                pixels=int(stats[part,cv2.CC_STAT_AREA]);area=pixels*area_pixel
                if area<options.source_floor_um2 and not options.preserve_parent_regions:continue
                # Work within the component rectangle, not the entire parent ROI.
                px,py,pw,ph=map(int,stats[part,:4])
                component_section=(slice(section[0].start+py,section[0].start+py+ph),
                                   slice(section[1].start+px,section[1].start+px+pw))
                mask=parts[py:py+ph,px:px+pw]==part
                labels[component_section][mask]=next_label
                geometry=_geometry(mask,(component_section[1].start,component_section[0].start),pixel)
                rx,ry=np.ceil(options.interior_erosion_um/pixel).astype(int)
                core=cv2.erode(np.uint8(mask),cv2.getStructuringElement(cv2.MORPH_ELLIPSE,(2*rx+1,2*ry+1)),
                               borderType=cv2.BORDER_CONSTANT,borderValue=0)>0
                core_pixels=int(core.sum())
                # Keep an unresolved thin/filamentary source visible. Its full-mask
                # color is diagnostic only and cannot qualify a material proposal.
                measured_mask=core if core_pixels else mask
                usable={'available':False,'reason':'Unknown contrast population; uniform usable terrace not established.'}
                merged=bool(options.merge_material_populations and class_index==-3)
                if merged:
                    usable={'available':False,'reason':'Merged material-compatible populations; uniform thickness or terrace not established.'}
                geometry['usable_width_um']=0.
                if class_index>0:
                    from .terrace_analysis import largest_uniform_rectangle
                    rectangle_started=time.perf_counter()
                    usable=largest_uniform_rectangle(core,pixel,
                        origin_xy=(component_section[1].start,component_section[0].start))
                    usable.update(available=True,uniformity_basis='same pixelwise contrast-reference class; material/thickness unconfirmed',erosion_um=options.interior_erosion_um)
                    geometry['usable_width_um']=min(usable['width_um'],usable['height_um'])
                    rectangle_seconds+=time.perf_counter()-rectangle_started
                geometry['usable_rectangle']=usable
                values=contrast[component_section][measured_mask].astype(float)
                patch_means,patch_counts=_patch_statistics(values,measured_mask,component_section,pixel,options.patch_um)
                mean=np.average(patch_means,axis=0,weights=patch_counts)
                random_cov=np.cov(patch_means.T)/len(patch_means) if len(patch_means)>1 else raw_cov
                local_mean=(mean-centre)/(1-centre)
                J=np.diag(1/(1-centre));Jbackground=np.diag((mean-1)/(1-centre)**2)
                mean_cov=J@random_cov@J.T+Jbackground@systematic@Jbackground.T+numeric
                evidence=classifier.classify(local_mean,mean_cov)
                median=np.median(values,axis=0);residual=values-median
                texture=float(np.median(np.einsum('nc,cd,nd->n',residual,inverse_raw_cov,residual)))
                inner=cv2.erode(np.uint8(mask),np.ones((3,3),np.uint8),borderType=cv2.BORDER_CONSTANT,borderValue=0)>0;boundary=mask&~inner
                # A descriptive raw neighboring-step statistic, not a shape veto.
                edge=float(np.median(raw_gradient[component_section][boundary])) if boundary.any() else 0.
                occupancy=float(material_map[component_section][mask].mean());debris_fraction=float(debris_map[component_section][mask].mean())
                # Geometric size is independent of whether this optical model
                # assigned a colour class. Unknown classes have no measured
                # uniform rectangle; that absence is not a zero-width flake.
                size_ok=area>=options.useful_area_um2
                color_ok=bool(core_pixels) and bool(evidence['material_compatible']) and occupancy>=.5
                reason=[]
                if not core_pixels:reason.append('no_blur_free_interior')
                if not size_ok:reason.append('below_useful_size')
                if not color_ok:reason.append('color_unclassified_or_not_coherent_material_population')
                class_name=('image-observed optical feature' if options.preserve_parent_regions else 'material-compatible optical feature' if merged else 'unknown' if class_index<0 else classifier.components[int(class_index)-1]['label'] if classifier.components is not None else f"conditional_{classifier.model['thickness_nm'][int(class_index)-1]:g}nm")
                regions.append({'instance_label':next_label,'parent_instance_label':int(parent),'area_um2':area,'area_px':pixels,
                    **geometry,'class_index':int(class_index),'class_name':class_name,'contrast_rgb':local_mean.tolist(),'contrast_covariance':mean_cov.tolist(),
                    **({'image_region_preserved':True,
                        'constituent_class_indices':np.unique(class_map[component_section][mask]).astype(int).tolist(),
                        'geometry_qualification':'Supplied observed image regions, not carved by material, substrate, or unknown optical classes.'} if options.preserve_parent_regions else {}),
                    **({'material_population_merged':True,
                        'constituent_class_indices':np.unique(class_map[component_section][mask]).astype(int).tolist(),
                        'geometry_qualification':'Connected material-compatible pixels; numerical optical bins are not geometric boundaries or established layer numbers.'} if merged else {}),
                    'material_pixel_fraction':occupancy,'debris_reference_pixel_fraction':debris_fraction,
                    'selected_for_review':bool(color_ok),'proposal_stage':'material_contrast_candidate' if color_ok else 'unknown_optical_feature',
                    'priority':'useful_size_review' if color_ok and size_ok and area>=50 else 'small_feature_edge_shape_review' if color_ok else 'unclassified_review',
                    'small_feature_requires_extra_review':bool(area<50),
                    'size_is_ranking_only':True,
                    'size_assessment':{'area_qualified':bool(size_ok),'minimum_area_um2':options.useful_area_um2,
                        'uniform_core_width_um':geometry['usable_width_um'] if usable.get('available') else None,
                        'uniform_core_width_is_diagnostic':True,'material_color_independent':True},
                    'layer_number':None,'chemical_identity_confirmed':False,'filter_reasons':reason,
                    'sieve_results':[{'name':'useful_size','status':'pass' if size_ok else 'below_priority','area_um2':area,'minimum_area_um2':options.useful_area_um2},
                        {'name':'material_color_population','status':'compatible' if color_ok else 'unknown','support_fraction':occupancy,'reference_distance':float(evidence['reference_distance']),
                         'log_likelihood_ratio_vs_substrate':float(evidence['log_likelihood_ratio_vs_substrate'])},
                        {'name':'edge','status':'descriptive_only','median_raw_contrast_gradient':edge},
                        {'name':'texture','status':'descriptive_only','median_raw_noise_normalized_residual2':texture}],
                    'measurement':{'contrast_basis':'eroded_interior' if core_pixels else 'whole_region_diagnostic_only',
                        'interior_erosion_um':options.interior_erosion_um,'interior_pixels':core_pixels,
                        'contrast_measurement_pixels':int(measured_mask.sum()),'edge_pixels_excluded':pixels-int(measured_mask.sum()),
                        'non_interior_pixels':pixels-core_pixels,
                        'interior_measurement_available':bool(core_pixels),'spatial_patch_count':len(patch_means),'patch_um':options.patch_um,'background_sample_count':background_sample_count,
                        'background_systematic_supplied':background_systematic_covariance is not None,'background_uncertainty_not_divided_by_region_area':True,
                        'qualification':'Patch means account for spatially correlated variation but are not validated independent samples; missing normalization systematic uncertainty is unquantified.'}})
                next_label+=1
    return {'labels':labels,'parent_labels':parents,'regions':regions,'class_map':class_map,
        'material_compatible_mask':material_map,'debris_compatible_mask':debris_map,
        'report':{'schema':1,'method':'supplied_image_regions_with_optical_measurements' if options.preserve_parent_regions else 'pixel_population_classification_then_connected_regions','parent_geometry':provenance,
            'options':asdict(options),'contrast_convention':'(substrate-flake)/substrate','classifier':classifier.metadata,
            'background_centre_rgb':centre.tolist(),'smoothed_substrate_covariance':cov.tolist(),
            'saturation_mask_supplied':saturated_mask is not None,
            'normalization_systematic_supplied':background_systematic_covariance is not None,
            'source_regions':len(regions),'selected_regions':sum(r['selected_for_review'] for r in regions),
            'seconds':{'normalization':timing_normalize,'pixel_classification':timing_classify,'usable_rectangles':rectangle_seconds,'total':time.perf_counter()-started},
            'holes_preserved':True,'disconnected_islands_not_unioned':True,'layer_assignment_allowed':False,
            'negative_result_means':'No accepted regions under this model; not verified absence of thin material.'}}
