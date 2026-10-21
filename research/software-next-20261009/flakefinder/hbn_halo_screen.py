"""Reversible observed rim/halo screen, independent of optical material classes.

A thin mask alone is never rejected. This heuristic requires a narrow mask
largely hugging a stronger feature, intermediate aligned contrast, and measured
decay to an exterior background. It is not artifact ground truth.
"""
import numpy as np
from scipy.ndimage import distance_transform_edt, find_objects

VERSION='hbn-observed-halo-v1'
OPTIONS={'interior_depth_um':.9,'maximum_interior_fraction':.25,
    'maximum_inradius_um':2.,'neighbor_distance_um':2.,'minimum_hugging_fraction':.75,
    'minimum_group_neighbor_hugging_fraction':.1,
    'minimum_region_area_um2':25.,'minimum_neighbor_area_um2':25.,
    'minimum_neighbor_area_ratio':.5,'minimum_neighbor_contrast_ratio':1.8,
    'minimum_contrast_alignment':.75,'maximum_outer_contrast_fraction':.7,
    'exterior_probe_um':1.2,'minimum_exterior_pixels':16}


def screen_halos(regions,labels,prepared,pixel,*,valid_mask=None,check_cancel=None):
    """Return dict[int, screen record]; never alter masks/inputs.

    prepared is graphene_useful_regions.prepare_field output; optional valid
    restricts measured support. Per-label hbn_screen.operator_supported is an
    explicit retrieval override. Raw conjunction is still reported for audit.
    """
    labels=np.asarray(labels);pixel=np.asarray(pixel,float)
    normalized=np.asarray(prepared['normalized'],float)
    smooth=np.asarray(prepared['smooth'],float)
    noise=np.asarray(prepared['noise'],float)
    valid=np.asarray(prepared.get('valid',np.ones(labels.shape,bool)) if valid_mask is None else valid_mask,bool)
    check=check_cancel or (lambda:None)
    check()
    if labels.ndim!=2 or labels.dtype.kind not in 'iu' or (labels<0).any():raise ValueError('Nonnegative integer labels required')
    if normalized.shape!=labels.shape+(3,) or smooth.shape!=normalized.shape or valid.shape!=labels.shape:raise ValueError('Aligned field arrays required')
    if pixel.shape!=(2,) or not np.isfinite(pixel).all() or (pixel<=0).any():raise ValueError('Positive calibrated XY pixels required')
    if noise.shape!=(3,) or not np.isfinite(noise).all() or (noise<=0).any():raise ValueError('Positive measured RGB noise required')
    if not np.isfinite(normalized[valid]).all() or not np.isfinite(smooth[valid]).all():raise ValueError('Finite measured RGB required')
    spacing=(pixel[1],pixel[0]);area_pixel=float(np.prod(pixel));sections=find_objects(labels)
    stats={};by_id={int(r['instance_label']):r for r in regions}
    # Robust feature medians come from all observed labels, including neighbors
    # not selected as material candidates. No model-compatibility term is used.
    for label,section in enumerate(sections,1):
        check()
        if section is None:continue
        mask=(labels[section]==label)&valid[section]
        count=int(mask.sum())
        if count*area_pixel<OPTIONS['minimum_neighbor_area_um2']:continue
        vector=np.median(smooth[section][mask],axis=0)*noise
        stats[label]={'vector':vector,'amplitude':float(np.linalg.norm(vector)),'area_um2':count*area_pixel}
    records={}
    pad=int(np.ceil((OPTIONS['neighbor_distance_um']+OPTIONS['exterior_probe_um']+2)/min(pixel)))
    for label,row in by_id.items():
        check()
        override=bool(row.get('hbn_screen',{}).get('operator_supported') or row.get('operator_supported'))
        result={'policy_version':VERSION,'likely_artifact':False,'likely_halo':False,'observed_halo_conjunction':False,
            'operator_positive_override':override,'flags':[],'metrics':{},'status':'not_flagged'}
        records[label]=result
        if label not in stats:result['status']='below_assessment_area';continue
        section=sections[label-1];sy,sx=section
        roi=(slice(max(0,sy.start-pad),min(labels.shape[0],sy.stop+pad)),slice(max(0,sx.start-pad),min(labels.shape[1],sx.stop+pad)))
        lab=labels[roi];usable=valid[roi];mask=(lab==label)&usable
        distance=distance_transform_edt(np.pad(mask,1),sampling=spacing)[1:-1,1:-1]
        interior=float(np.mean(distance[mask]>OPTIONS['interior_depth_um']));inradius=float(distance.max())
        metrics=result['metrics'];metrics.update(interior_fraction=interior,maximum_inradius_um=inradius,
            candidate_contrast_norm=stats[label]['amplitude'],candidate_area_um2=stats[label]['area_um2'])
        narrow=interior<=OPTIONS['maximum_interior_fraction'] and inradius<=OPTIONS['maximum_inradius_um']
        if not narrow:result['status']='substantial_observed_interior';continue
        candidate=stats[label];best=None;eligible=[]
        for neighbor in np.unique(lab):
            neighbor=int(neighbor)
            if neighbor==label or neighbor not in stats:continue
            other=stats[neighbor]
            if other['area_um2']<max(OPTIONS['minimum_neighbor_area_um2'],candidate['area_um2']*OPTIONS['minimum_neighbor_area_ratio']):continue
            ratio=other['amplitude']/max(candidate['amplitude'],1e-12)
            if ratio<OPTIONS['minimum_neighbor_contrast_ratio']:continue
            alignment=float(candidate['vector']@other['vector']/max(candidate['amplitude']*other['amplitude'],1e-12))
            if alignment<OPTIONS['minimum_contrast_alignment']:continue
            proximity=distance_transform_edt(~((lab==neighbor)&usable),sampling=spacing)
            if np.mean(proximity[mask]<=OPTIONS['neighbor_distance_um'])<OPTIONS['minimum_group_neighbor_hugging_fraction']:continue
            eligible.append(neighbor)
        # A single observed rim may wrap several disconnected lobes. Assess
        # their union only for distance-to-neighbor evidence; never union the
        # actual region masks or alter either lobe's geometry.
        groups=[[neighbor] for neighbor in eligible]
        if len(eligible)>1:groups.append(eligible)
        for neighbors in groups:
            check()
            total_area=sum(stats[n]['area_um2'] for n in neighbors)
            vector=sum(stats[n]['vector']*stats[n]['area_um2'] for n in neighbors)/total_area
            amplitude=float(np.linalg.norm(vector))
            ratio=amplitude/max(candidate['amplitude'],1e-12)
            alignment=float(candidate['vector']@vector/max(candidate['amplitude']*amplitude,1e-12))
            if ratio<OPTIONS['minimum_neighbor_contrast_ratio'] or alignment<OPTIONS['minimum_contrast_alignment']:continue
            nd=distance_transform_edt(~(np.isin(lab,neighbors)&usable),sampling=spacing)
            hugging=float(np.mean(nd[mask]<=OPTIONS['neighbor_distance_um']))
            if hugging<OPTIONS['minimum_hugging_fraction']:continue
            # Sample actual unlabeled exterior near the candidate, beyond the
            # stronger feature; no absent/out-of-frame pixels count as decay.
            outside=distance_transform_edt(~mask,sampling=spacing)
            exterior=(lab==0)&usable&(outside>0)&(outside<=OPTIONS['exterior_probe_um'])&(nd>np.median(nd[mask]))
            count=int(exterior.sum())
            if count<OPTIONS['minimum_exterior_pixels']:continue
            direction=vector/max(amplitude,1e-12)
            candidate_projection=float(candidate['vector']@direction)
            outer_vector=np.median(smooth[roi][exterior],axis=0)*noise
            outer_projection=float(abs(outer_vector@direction))
            decay=outer_projection/max(candidate_projection,1e-12)
            evidence={'neighbor_instance_label':neighbors[0],'neighbor_instance_labels':neighbors,'neighbor_area_um2':total_area,
                'neighbor_contrast_ratio':ratio,'rgb_direction_alignment':alignment,
                'near_stronger_neighbor_fraction':hugging,'exterior_pixels':count,
                'exterior_to_candidate_projected_contrast':decay}
            if best is None or hugging>best['near_stronger_neighbor_fraction']:best=evidence
        if best is None:result['status']='no_supported_stronger_neighbor_and_exterior';continue
        metrics.update(best)
        halo=best['exterior_to_candidate_projected_contrast']<=OPTIONS['maximum_outer_contrast_fraction']
        result['observed_halo_conjunction']=bool(halo)
        result['likely_artifact']=bool(halo and not override)
        result['likely_halo']=result['likely_artifact']
        if halo:
            result['flags']=['narrow_decay_rim_hugging_stronger_feature']
            result['status']='operator_positive_retained' if override else 'likely_halo_review_artifact'
        else:result['status']='exterior_decay_not_supported'
    return records
