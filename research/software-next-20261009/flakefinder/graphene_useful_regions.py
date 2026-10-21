"""Field-cached geometric refinement/ranking independent of material colour.

This is geometric evidence for human review, never chemical identification.
Every added pixel must have foreground-like measured RGB after physical-scale
edge-preserving denoising; no hull, closing, or hole-fill operation is used.
"""
from dataclasses import dataclass,asdict
import cv2
import numpy as np
from scipy.ndimage import distance_transform_edt,binary_propagation,map_coordinates


@dataclass(frozen=True)
class RefinementOptions:
    denoise_um:float=.4
    maximum_growth_um:float=2.
    background_inner_um:float=3.
    background_outer_um:float=10.
    boundary_probe_um:float=1.
    minimum_support:int=100


def _geometry(mask):
    contours,hierarchy=cv2.findContours(np.uint8(mask),cv2.RETR_CCOMP,cv2.CHAIN_APPROX_SIMPLE)
    if hierarchy is None:return []
    result=[]
    for i,contour in enumerate(contours):
        if hierarchy[0,i,3]<0:
            result.append({'polygon_px':contour[:,0,:].tolist(),
                'holes_px':[c[:,0,:].tolist() for j,c in enumerate(contours) if hierarchy[0,j,3]==i]})
    return result


def prepare_field(rgb,valid,substrate,pixel_size_um,options=None):
    """One denoising pass reused by every useful region in this saved field."""
    options=options or RefinementOptions();rgb=np.asarray(rgb,np.float32)
    support=np.asarray(valid,bool)&np.asarray(substrate,bool)
    samples=rgb[support]
    if len(samples)<100:raise ValueError('Insufficient recorded field substrate support.')
    background=np.median(samples[::max(1,len(samples)//20000)],axis=0)
    normalized=rgb/background
    pairs=support[:,1:]&support[:,:-1];differences=np.diff(normalized,axis=1)[pairs]
    if len(differences)<32:raise ValueError('Insufficient field substrate noise support.')
    differences=differences[::max(1,len(differences)//20000)]
    noise=np.maximum(np.median(np.abs(differences-np.median(differences,axis=0)),axis=0)/.67448975/np.sqrt(2),1e-5)
    whitened=np.asarray((normalized-1)/noise,np.float32);pixel=np.asarray(pixel_size_um,float)
    radius=max(1,int(np.ceil(2*options.denoise_um/min(pixel))))
    smooth=cv2.bilateralFilter(whitened,2*radius+1,3.,options.denoise_um/float(np.mean(pixel)))
    return {'smooth':smooth,'normalized':normalized,'noise':noise}


def refine_candidate(rgb,original_mask,valid_mask,pixel_size_um,*,substrate_mask=None,options=None,prepared=None):
    options=options or RefinementOptions();values=np.asarray(list(asdict(options).values()),float)
    if not np.isfinite(values).all() or (values<=0).any():raise ValueError('Positive finite refinement scales required.')
    rgb=np.asarray(rgb,np.float32);original=np.asarray(original_mask,bool);valid=np.asarray(valid_mask,bool)
    pixel=np.asarray(pixel_size_um,float)
    if rgb.shape!=(*original.shape,3) or valid.shape!=original.shape or pixel.shape!=(2,) or not np.isfinite(pixel).all() or (pixel<=0).any():
        raise ValueError('Aligned RGB/masks and positive XY pixel scale required.')
    if not np.isfinite(rgb).all() or not original.any():raise ValueError('Finite raw RGB and an observed candidate mask required.')
    spacing=(float(pixel[1]),float(pixel[0]));outside=distance_transform_edt(~original,sampling=spacing)
    ring=valid&(outside>=options.background_inner_um)&(outside<=options.background_outer_um)
    if substrate_mask is not None:ring&=np.asarray(substrate_mask,bool)
    inside=original&valid
    result={'original_mask':original.copy(),'refined_mask':original.copy(),'uncertain_mask':original.copy(),
        'status':'unresolved','flags':[],'options':asdict(options),'geometry':_geometry(original),
        'qualification':'Candidate-local RGB boundary evidence only. Original mask retained; flags are not artifact ground truth.'}
    if ring.sum()<options.minimum_support or inside.sum()<16:
        result['flags']=['insufficient_local_background_or_foreground'];return result
    background=np.median(rgb[ring],axis=0)
    if (background<=0).any():raise ValueError('Nonpositive local raw background.')
    normalized=rgb/background if prepared is None else prepared['normalized']
    # Adjacent recorded background differences estimate channel noise without
    # interpreting the desired candidate colour as a substrate label.
    pair=ring[:,1:]&ring[:,:-1]
    differences=np.diff(normalized,axis=1)[pair]
    if len(differences)<32:
        result['flags']=['insufficient_local_noise_support'];return result
    noise=(np.maximum(np.median(np.abs(differences-np.median(differences,axis=0)),axis=0)/.67448975/np.sqrt(2),1e-5)
        if prepared is None else prepared['noise'])
    # Resample XY only for the denoiser when pixel axes differ substantially.
    # The camera currently has square pixels; reject unknown anisotropic scaling
    # rather than quietly apply a different physical smoothing radius.
    if max(pixel)/min(pixel)>1.05:raise ValueError('Refinement currently requires approximately square calibrated pixels.')
    if prepared is None:
        whitened=np.asarray((normalized-1)/noise,np.float32)
        radius=max(1,int(np.ceil(2*options.denoise_um/min(pixel))))
        smooth=cv2.bilateralFilter(whitened,2*radius+1,3.,options.denoise_um/float(np.mean(pixel)))
    else:smooth=prepared['smooth']
    foreground=np.median(smooth[inside],axis=0);base=np.median(smooth[ring],axis=0)
    direction=foreground-base;snr=float(np.linalg.norm(direction))
    if snr<3:
        result['flags']=['foreground_not_separated_from_local_noise'];result['foreground_snr']=snr;return result
    score=np.einsum('ijk,k->ij',smooth-base,direction)/(snr*snr)
    residual=np.linalg.norm((smooth-base)-score[:,:,None]*direction,axis=2)
    allowed=valid&(outside<=options.maximum_growth_um)&(score>=.5)&(residual<=max(4.,snr*.6))
    seeds=inside&(score>=.65)&allowed
    if not seeds.any():
        result['flags']=['no_stable_observed_foreground_seed'];return result
    refined=binary_propagation(seeds,mask=allowed)
    # No shape prior fills holes: a measured background hole remains below the
    # foreground likelihood boundary, regardless of its enclosing polygon.
    uncertain=valid&(outside<=options.maximum_growth_um)&(np.abs(score-.5)<=max(.15,1.5/snr))
    signed=distance_transform_edt(refined,sampling=spacing)-distance_transform_edt(~refined,sampling=spacing)
    gy,gx=np.gradient(cv2.GaussianBlur(signed.astype(np.float32),(0,0),1.),*spacing)
    boundary=refined&~cv2.erode(np.uint8(refined),np.ones((3,3),np.uint8)).astype(bool)
    yy,xx=np.nonzero(boundary);step=max(1,len(xx)//2000);yy=yy[::step];xx=xx[::step]
    length=np.maximum(np.hypot(gx[yy,xx],gy[yy,xx]),1e-9)
    dx=gx[yy,xx]/length*options.boundary_probe_um/pixel[0];dy=gy[yy,xx]/length*options.boundary_probe_um/pixel[1]
    # Camera/crop limits have no observed exterior profile. Exclude them from
    # sharpness evidence instead of interpreting missing pixels as diffuse edges.
    measured=(xx+dx>=0)&(xx+dx<original.shape[1]-1)&(xx-dx>=0)&(xx-dx<original.shape[1]-1)
    measured&=(yy+dy>=0)&(yy+dy<original.shape[0]-1)&(yy-dy>=0)&(yy-dy<original.shape[0]-1)
    yy=yy[measured];xx=xx[measured];dx=dx[measured];dy=dy[measured]
    inward=map_coordinates(score,[yy+dy,xx+dx],order=1,mode='nearest')
    outward=map_coordinates(score,[yy-dy,xx-dx],order=1,mode='nearest')
    transition=inward-outward
    edge_support=float(np.mean(transition>=.45)) if len(transition) else 0.
    # A crop edge is not a measured chip edge. Only actual saved invalid-domain
    # pixels supply the domain-distance zero set.
    domain_distance=distance_transform_edt(valid,sampling=spacing) if (~valid).any() else np.full(valid.shape,np.inf)
    domain_near=float(np.mean(domain_distance[refined]<=options.background_inner_um)) if refined.any() else 1.
    darkness=1-np.mean(normalized,axis=2);candidate_dark=float(np.median(darkness[inside]))
    nearby=(outside>0)&(outside<options.background_outer_um)&valid
    dark_debris=nearby&(darkness>max(.12,3*max(candidate_dark,0)))
    debris_fraction=float(dark_debris.sum()/max(1,nearby.sum()))
    flags=[]
    if edge_support<.5:flags.append('diffuse_or_weak_boundary')
    if domain_near>.05:flags.append('near_saved_domain_boundary')
    if debris_fraction>.02 and edge_support<.6:flags.append('diffuse_boundary_near_dark_feature_possible_shadow')
    if original[0].any() or original[-1].any() or original[:,0].any() or original[:,-1].any():flags.append('candidate_cut_by_crop_or_camera')
    ratio=float(refined.sum()/original.sum())
    if not .5<=ratio<=2.:flags.append('large_area_change_requires_review')
    if not refined.any():flags.append('no_supported_refined_pixels')
    median_transition=float(np.median(transition)) if len(transition) else None
    likely_artifact=bool(edge_support<.5 and debris_fraction>.08 and median_transition is not None and median_transition<.45)
    contour_flags=contour_blockers(flags)
    result.update(refined_mask=refined,uncertain_mask=uncertain,status='review_required' if contour_flags else 'edge_supported_proposal',
        flags=flags,likely_artifact=likely_artifact,
        artifact_disposition='likely_artifact' if likely_artifact else ('review_only' if flags else 'no_artifact_flag'),
        artifact_qualification='Heuristic conjunction of diffuse boundary and substantial nearby dark feature, not validated artifact ground truth. Darkness or failed material colour alone never rejects a candidate.',
        geometry=_geometry(refined),foreground_snr=snr,
        metrics={'original_area_um2':float(original.sum()*np.prod(pixel)),'refined_area_um2':float(refined.sum()*np.prod(pixel)),
            'area_ratio':ratio,'boundary_support_fraction':edge_support,'median_boundary_transition':median_transition,
            'domain_near_fraction':domain_near,'nearby_dark_feature_fraction':debris_fraction,
            'original_retained_fraction':float((original&refined).sum()/original.sum()),
            'background_pixels':int(ring.sum()),'noise_rgb_relative':noise.tolist()})
    result['metrics'].update(measured_boundary_profiles=len(transition),camera_boundary_excluded=True,
        extent_incomplete='candidate_cut_by_crop_or_camera' in flags)
    # A local, dimensionless interior-uniformity observation for material-specific
    # review screens. This is not a chemical test or a generic rejection rule.
    rx,ry=np.ceil(.6/pixel).astype(int)
    core=cv2.erode(np.uint8(refined),cv2.getStructuringElement(cv2.MORPH_ELLIPSE,(2*rx+1,2*ry+1)),
        borderType=cv2.BORDER_CONSTANT,borderValue=0).astype(bool)
    core_values=smooth[core];variation=None
    if len(core_values)>=16:
        variation=float(np.median(np.linalg.norm(core_values-np.median(core_values,axis=0),axis=1))/snr)
    result['metrics'].update(interior_pixels=int(core.sum()),interior_relative_variation=variation,
        interior_erosion_um=.6,interior_uniformity_basis='Median noise-whitened RGB deviation / local foreground contrast norm; descriptive.')
    return result


def contour_blockers(flags):
    # These observations qualify the object, not the validity of its measured contour.
    diagnostic={'candidate_cut_by_crop_or_camera','diffuse_boundary_near_dark_feature_possible_shadow'}
    return [flag for flag in flags if flag not in diagnostic]


def review_assessment(region,result,pixel_size_um):
    """Rank geometry/edge evidence; optical-model compatibility is not a term."""
    area=float(region['area_um2']);metrics=dict(result.get('metrics',{}));flags=list(result['flags'])
    edge=float(metrics.get('boundary_support_fraction',0));step=max(0.,float(metrics.get('median_boundary_transition') or 0))
    snr=float(result.get('foreground_snr',0));near=float(metrics.get('nearby_dark_feature_fraction',0))
    diffuse=edge<.35 and step<.3
    if diffuse and 'diffuse_or_weak_boundary' not in flags:flags.append('diffuse_or_weak_boundary')
    # Strong material colour or sheer area cannot promote an out-of-focus halo.
    likely=bool(result.get('likely_artifact') or diffuse and near>.04)
    shape=result['refined_mask'] if result['status']=='edge_supported_proposal' else result['original_mask']
    diameter=float(2*distance_transform_edt(np.pad(shape,1),sampling=(pixel_size_um[1],pixel_size_um[0])).max())
    yy,xx=np.nonzero(shape)
    fill=float(len(xx)/((np.ptp(xx)+1)*(np.ptp(yy)+1))) if len(xx) else 0.
    geometric=area>=25.
    rank=float(np.log1p(min(area,1000)/25)*(.1+.9*edge**2)*(.25+.75*min(step,1.))*min(1.,snr/5))
    if diffuse:rank*=.15
    if likely:rank*=.05
    if metrics.get('domain_near_fraction',0)>.05:rank*=.6
    rank*=float(np.sqrt(max(.15,fill)))
    if 'candidate_cut_by_crop_or_camera' in flags:rank*=.75
    if result['status']=='unresolved':rank=0.
    old=list(region.get('filter_reasons',[]));reasons=[r for r in old if r!='below_useful_size' or not geometric]
    metrics.update(geometric_area_um2=area,inscribed_diameter_um=diameter,bounding_box_occupancy=fill,edge_quality_rank=rank,
        optical_class_used_for_ranking=False,old_size_flag_removed=geometric and 'below_useful_size' in old)
    return {'rank':rank,'likely_artifact':likely,'artifact_flags':flags,'metrics':metrics,
        'useful_size':geometric,'filter_reasons':reasons,'original_filter_reasons':old,
        'size_assessment':{'area_qualified':geometric,'minimum_area_um2':25.,'inscribed_diameter_um':diameter,
            'width_is_diagnostic_not_rejection':True,'material_color_independent':True},
        'ranking_qualification':'Geometric review priority, not graphene probability. Diffuse/shadow penalties are heuristic and reversible.'}
