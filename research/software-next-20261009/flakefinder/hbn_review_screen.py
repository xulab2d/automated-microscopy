"""Conservative, reversible morphology screen; not hBN or residue identification."""
import math
import cv2
import numpy as np
from scipy.ndimage import distance_transform_edt


POLICY_VERSION='hbn-observed-boundary-interior-v1'
THRESHOLDS={
    'minimum_area_um2':25.,'minimum_inscribed_diameter_um':3.,
    'minimum_boundary_support':.7,'minimum_boundary_transition':.55,
    'minimum_measured_boundary_profiles':24,'minimum_foreground_snr':5.,
    'minimum_solidity':.6,'polygon_tolerance_um':.5,
    'minimum_straight_segment_um':3.,'minimum_straight_fraction':.25,
    'minimum_segment_perimeter_fraction':.1,
    'minimum_nonparallel_angle_deg':20.,'minimum_interior_pixels':16,
    'maximum_interior_relative_variation':.25,
}


def morphology_screen(region,result,pixel_size_um,review_metrics=None):
    """Screen observed shape and contrast, independently of material colour.

    The caller may combine ``passes`` with optical compatibility for a review
    shortlist. Failure means uncertain morphology, never proof of tape/residue.
    No source masks, region records, or refined results are mutated.
    """
    pixel=np.asarray(pixel_size_um,float)
    if pixel.shape!=(2,) or not np.isfinite(pixel).all() or np.any(pixel<=0):
        raise ValueError('Positive finite XY pixel scale required.')
    metrics={**result.get('metrics',{}),**(review_metrics or {})}
    accepted=result.get('status')=='edge_supported_proposal'
    mask=np.asarray(result['refined_mask'] if accepted else result['original_mask'],bool)
    if mask.ndim!=2:raise ValueError('A two-dimensional observed mask is required.')
    reasons=[]
    if not accepted:reasons.append('contour_requires_review')
    if result.get('likely_artifact'):reasons.append('existing_artifact_warning')
    count,labels=cv2.connectedComponents(mask.astype(np.uint8),connectivity=4)
    if count!=2:reasons.append('disconnected_or_empty_geometry')
    contours,_=cv2.findContours(mask.astype(np.uint8),cv2.RETR_EXTERNAL,cv2.CHAIN_APPROX_NONE)
    area=float(mask.sum()*np.prod(pixel));diameter=0.;solidity=0.;compactness=0.;fraction=0.;long_count=0;nonparallel=False
    if contours:
        contour=max(contours,key=cv2.contourArea)[:,0,:].astype(np.float32)*pixel.astype(np.float32)
        perimeter=float(cv2.arcLength(contour,True));contour_area=float(cv2.contourArea(contour))
        hull_area=float(cv2.contourArea(cv2.convexHull(contour)))
        solidity=contour_area/hull_area if hull_area else 0.
        compactness=4*math.pi*contour_area/perimeter**2 if perimeter else 0.
        polygon=cv2.approxPolyDP(contour,THRESHOLDS['polygon_tolerance_um'],True)[:,0,:]
        segments=np.roll(polygon,-1,axis=0)-polygon;lengths=np.linalg.norm(segments,axis=1)
        # Polygonal approximation also tessellates smooth round blobs. Require
        # substantial sides relative to this perimeter, not any short chord.
        minimum_length=max(THRESHOLDS['minimum_straight_segment_um'],
            THRESHOLDS['minimum_segment_perimeter_fraction']*perimeter)
        long=lengths>=minimum_length;long_count=int(long.sum())
        fraction=min(1.,float(lengths[long].sum()/perimeter)) if perimeter else 0.
        directions=segments[long]/np.maximum(lengths[long,None],1e-12)
        if len(directions)>=2:
            cosines=np.abs(directions@directions.T)
            nonparallel=bool(np.any(cosines<math.cos(math.radians(THRESHOLDS['minimum_nonparallel_angle_deg']))))
        diameter=float(2*distance_transform_edt(np.pad(mask,1),sampling=(pixel[1],pixel[0])).max())
    metrics.update(screen_area_um2=area,screen_inscribed_diameter_um=diameter,
        contour_solidity=solidity,contour_compactness=compactness,
        straight_edge_fraction=fraction,long_straight_segment_count=long_count,
        nonparallel_straight_edges=nonparallel,
        foreground_snr=float(result.get('foreground_snr',0.)),
        geometry_basis='refined_observed_mask' if accepted else 'original_observed_mask')
    checks=[(area>=25.,'insufficient_review_area'),(diameter>=3.,'narrow_or_filamentary_geometry'),
        (solidity>=.6,'low_contour_solidity'),
        (fraction>=.25 and long_count>=2 and nonparallel,'insufficient_straight_edge_evidence')]
    for key,bound,reason in [('boundary_support_fraction',.7,'weak_boundary_support'),
        ('median_boundary_transition',.55,'diffuse_boundary_transition'),
        ('measured_boundary_profiles',24,'insufficient_observed_boundary_profiles'),
        ('foreground_snr',5.,'weak_foreground_separation')]:
        value=metrics.get(key);checks.append((value is not None and math.isfinite(float(value)) and float(value)>=bound,reason))
    variation=metrics.get('interior_relative_variation');core=metrics.get('interior_pixels',0)
    observed=variation is not None and math.isfinite(float(variation)) and core>=16
    checks.append((observed,'unmeasured_interior_uniformity'))
    if observed:checks.append((0<=float(variation)<=.25,'nonuniform_observed_interior'))
    for passed,reason in checks:
        if not passed:reasons.append(reason)
    return {'passes':not reasons,'reasons':reasons,'metrics':metrics,'policy_version':POLICY_VERSION,
        'thresholds':dict(THRESHOLDS),
        'qualification':'Heuristic observed-boundary and interior screen for review priority; not validated hBN identity, layer thickness, or tape/residue discrimination. Failures remain uncertain and retained; optical compatibility is a separate criterion.'}
