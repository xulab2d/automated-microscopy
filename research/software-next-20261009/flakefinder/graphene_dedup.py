"""Conservative, reversible grouping of saved cross-tile optical observations.

This module reads no files and controls no hardware. Callers supply one verified
run/chip/coordinate frame and its pinned camera transform. Grouping is a display
proposal, never a material identity claim or a change to source segmentations.
"""
from dataclasses import asdict, dataclass
import hashlib
import json
import math
import re

import cv2
import numpy as np


@dataclass(frozen=True)
class DedupOptions:
    minimum_overlap_fraction: float = .65
    minimum_iou: float = .35
    maximum_color_delta: float = .08
    maximum_color_mahalanobis2: float = 11.35
    color_systematic_sigma: float = .01
    raster_step_um: float = .25
    maximum_raster_step_um: float = .5
    maximum_raster_pixels: int = 262144
    spatial_cell_um: float = 100.
    maximum_spatial_cells_per_observation: int = 4096
    diagnostic_pair_limit: int = 256

    def __post_init__(self):
        if not all(math.isfinite(v) and v>0 for v in asdict(self).values()):
            raise ValueError('Positive finite deduplication options required')
        if self.minimum_overlap_fraction>1 or self.minimum_iou>1 or self.maximum_raster_step_um<self.raster_step_um:
            raise ValueError('Invalid overlap or raster options')
        if type(self.maximum_raster_pixels) is not int or self.maximum_raster_pixels<64:
            raise ValueError('An integer raster capacity of at least64 pixels is required')
        for value in (self.maximum_spatial_cells_per_observation,self.diagnostic_pair_limit):
            if type(value) is not int:raise ValueError('Integer spatial/diagnostic capacities required')


def _canonical(value):
    return hashlib.sha256(json.dumps(value,sort_keys=True,allow_nan=False).encode()).hexdigest()


def _prepare(row, context, transform, options):
    for name,value in context.items():
        if row.get(name) is not None and row[name]!=value:
            raise ValueError('observation_'+name+'_mismatch')
    field=row.get('field','')
    if not re.fullmatch(re.escape(context['chip_id'])+r'/tile_\d+',field):raise ValueError('different_chip_or_invalid_field')
    if not row['id'].startswith(field+'/'):raise ValueError('observation_id_field_mismatch')
    if row.get('source_transform_sha256')!=_canonical(transform):raise ValueError('unverified_source_transform')
    if not isinstance(row.get('source_tiff_sha256'),str) or not re.fullmatch('[a-f0-9]{64}',row['source_tiff_sha256']):
        raise ValueError('missing_source_tiff_identity')
    matrix=np.asarray(transform['matrix'],float)
    shape=np.asarray(row.get('source_shape_hw'),float)
    position=row.get('source_position_um',{})
    xy=np.asarray([position.get('x'),position.get('y')],float)
    if shape.shape!=(2,) or not np.isfinite(shape).all() or np.any(shape<1) or not np.isfinite(xy).all():
        raise ValueError('invalid_source_registration')
    polygon=row.get('polygon_source_px',row.get('polygon_px'))
    holes=row.get('holes_source_px',row.get('holes_px',[]))
    if not isinstance(holes,list):raise ValueError('invalid_polygon_holes')
    rings=[];pixel_rings=[]
    for points in [polygon,*holes]:
        ring=np.asarray(points,float)
        if ring.ndim!=2 or ring.shape[1]!=2 or len(ring)<3 or not np.isfinite(ring).all():
            raise ValueError('invalid_source_polygon')
        if np.any(ring<0) or np.any(ring[:,0]>=shape[1]) or np.any(ring[:,1]>=shape[0]):
            raise ValueError('source_polygon_outside_image')
        pixel_rings.append(ring)
        rings.append((ring-[(shape[1]-1)/2,(shape[0]-1)/2])@matrix.T+xy)
    outer=rings[0];area=abs(float(cv2.contourArea(np.float32(outer-outer[0]))))
    if area<=0:raise ValueError('degenerate_polygon')
    color=np.asarray(row.get('contrast_rgb'),float);cov=np.asarray(row.get('contrast_covariance'),float)
    if color.shape!=(3,) or cov.shape!=(3,3) or not np.isfinite(color).all() or not np.isfinite(cov).all():
        raise ValueError('missing_or_invalid_color_evidence')
    if not np.allclose(cov,cov.T,atol=1e-10) or np.linalg.eigvalsh(cov).min()<-1e-12:
        raise ValueError('invalid_color_covariance')
    margin_px=float(min(pixel_rings[0].min(),(shape[1]-1-pixel_rings[0][:,0]).min(),(shape[0]-1-pixel_rings[0][:,1]).min()))
    margin_um=max(0.,margin_px*float(np.linalg.svd(matrix,compute_uv=False).min()))
    measurement=row.get('measurement',{})
    quality={'has_measured_interior':measurement.get('interior_measurement_available') is True,
        'clear_of_image_border':margin_um>options.maximum_raster_step_um,
        'image_border_margin_um':margin_um,'contrast_covariance_trace':float(np.trace(cov)),
        'material_pixel_fraction':float(row.get('material_pixel_fraction',0.)),
        'area_um2':float(row.get('area_um2',0.))}
    if not all(math.isfinite(quality[k]) for k in ('contrast_covariance_trace','material_pixel_fraction','area_um2')):
        raise ValueError('invalid_quality_evidence')
    # Prefer measured, complete interiors and lower measured color uncertainty.
    # No RGB template thickness or unverified visual crystal-quality score enters.
    order=(-int(quality['has_measured_interior']),-int(quality['clear_of_image_border']),
        quality['contrast_covariance_trace'],-quality['material_pixel_fraction'],-quality['area_um2'],row['id'])
    bounds=(outer.min(axis=0),outer.max(axis=0))
    first=np.floor(bounds[0]/options.spatial_cell_um).astype(int);last=np.floor(bounds[1]/options.spatial_cell_um).astype(int)
    if int(last[0]-first[0]+1)*int(last[1]-first[1]+1)>options.maximum_spatial_cells_per_observation:
        raise ValueError('spatial_index_capacity_insufficient_keep_singleton')
    cells=tuple((x,y) for x in range(first[0],last[0]+1) for y in range(first[1],last[1]+1))
    return {'row':row,'rings':rings,'bounds':bounds,'cells':cells,
        'color':color,'covariance':cov,'quality':quality,'order':order}


def _overlap(a,b,options):
    low=np.minimum(a['bounds'][0],b['bounds'][0]);high=np.maximum(a['bounds'][1],b['bounds'][1])
    size=high-low;step=options.raster_step_um
    while True:
        shape=np.ceil(size/step).astype(int)+5
        if int(shape[0])*int(shape[1])<=options.maximum_raster_pixels:break
        step*=1.25
        if step>options.maximum_raster_step_um:
            return {'accepted':False,'reason':'overlap_resolution_capacity','raster_step_um':step}
    ratios=[]
    # Require the decision on two grid phases; near-threshold pixel alignment
    # must not decide which physical observations are hidden by grouped display.
    for phase in (0.,.5):
        origin=low-(2+phase)*step
        masks=[]
        for item in (a,b):
            mask=np.zeros((int(shape[1]),int(shape[0])),np.uint8)
            for i,ring in enumerate(item['rings']):
                contour=np.int32(np.round((ring-origin)/step))
                cv2.fillPoly(mask,[contour],1 if i==0 else 0)
            masks.append(mask)
        aa=int(np.count_nonzero(masks[0]));bb=int(np.count_nonzero(masks[1]))
        intersection=int(np.count_nonzero(masks[0]&masks[1]));union=aa+bb-intersection
        ratios.append({'intersection_um2':intersection*step*step,
            'overlap_fraction':intersection/min(aa,bb) if min(aa,bb)>0 else 0.,
            'iou':intersection/union if union else 0.})
    fraction=min(r['overlap_fraction'] for r in ratios);iou=min(r['iou'] for r in ratios)
    accepted=fraction>=options.minimum_overlap_fraction and iou>=options.minimum_iou
    return {'accepted':accepted,'reason':'overlapping_polygons' if accepted else 'insufficient_polygon_overlap',
        'overlap_fraction':fraction,'iou':iou,'raster_step_um':step,'grid_phase_measurements':ratios}


def _pair(a,b,options):
    if a['row']['field']==b['row']['field']:return {'accepted':False,'reason':'same_source_field'}
    if np.any(np.minimum(a['bounds'][1],b['bounds'][1])<=np.maximum(a['bounds'][0],b['bounds'][0])):
        return {'accepted':False,'reason':'disjoint_stage_bounds'}
    delta=a['color']-b['color'];maximum=float(np.abs(delta).max())
    covariance=a['covariance']+b['covariance']+np.eye(3)*options.color_systematic_sigma**2
    distance=float(delta@np.linalg.solve(covariance,delta))
    color={'maximum_color_delta':maximum,'color_mahalanobis2':distance}
    if maximum>options.maximum_color_delta or distance>options.maximum_color_mahalanobis2:
        return {'accepted':False,'reason':'color_disagreement',**color}
    return {**_overlap(a,b,options),**color}


def deduplicate_observations(observations, *, run_id, chip_id, coordinate_frame_id, transform, options=None):
    """Group one verified shortlist; all observations remain available unchanged.

    A row needs id, field, source TIFF/transform hashes, source shape/position,
    source-pixel polygon/holes and RGB contrast/covariance. Optional row run_id,
    chip_id and coordinate_frame_id must match the supplied verified context.
    Unknown provenance or insufficient evidence keeps a singleton, never deletes
    a row. Duplicate IDs are invalid input. A stage-space hash grid limits pair
    checks to nearby groups; diagnostic output is capped without capping groups.

    Membership is complete-link: every pair in a group must match. This prevents
    transitive A-B-C chains and two observations from one field joining a group.
    Representatives are real observations; polygons/areas are never unioned.
    """
    options=options or DedupOptions()
    if not isinstance(options,DedupOptions):raise TypeError('Expected DedupOptions')
    context={'run_id':run_id,'chip_id':chip_id,'coordinate_frame_id':coordinate_frame_id}
    if any(not isinstance(v,str) or not v for v in context.values()):raise ValueError('Explicit run/chip/coordinate frame required')
    matrix=np.asarray(transform.get('matrix'),float)
    if matrix.shape!=(2,2) or not np.isfinite(matrix).all() or abs(np.linalg.det(matrix))<1e-12:
        raise ValueError('Finite invertible pinned camera transform required')
    rows=list(observations);ids=[r.get('id') for r in rows]
    if any(not isinstance(i,str) or not i for i in ids) or len(set(ids))!=len(ids):raise ValueError('Unique observation IDs required')
    prepared={};invalid={}
    for row in rows:
        try:prepared[row['id']]=_prepare(row,context,transform,options)
        except (ValueError,TypeError,KeyError,IndexError) as exc:invalid[row['id']]=str(exc)
    accepted_pairs={};diagnostics=[];reason_counts={};pair_count=0
    ordered=sorted(prepared,key=lambda key:prepared[key]['order'])
    groups=[];spatial={}
    def pair(x,y):
        nonlocal pair_count
        key=tuple(sorted((x,y)))
        if key in accepted_pairs:return accepted_pairs[key]
        result=_pair(prepared[x],prepared[y],options);pair_count+=1
        reason_counts[result['reason']]=reason_counts.get(result['reason'],0)+1
        if result['accepted']:accepted_pairs[key]=result
        if len(diagnostics)<options.diagnostic_pair_limit:
            diagnostics.append({'observation_ids':list(key),**result})
        return result
    for key in ordered:
        compatible=[]
        candidate_groups=set()
        for cell in prepared[key]['cells']:candidate_groups.update(spatial.get(cell,()))
        for index in sorted(candidate_groups):
            members=groups[index]
            evidence=[pair(key,other) for other in members]
            if all(p['accepted'] for p in evidence):
                compatible.append((min(p['iou'] for p in evidence),index))
        if compatible:
            _,index=max(compatible,key=lambda v:(v[0],-v[1]));groups[index].append(key)
        else:index=len(groups);groups.append([key])
        for cell in prepared[key]['cells']:spatial.setdefault(cell,set()).add(index)
    groups.extend([[key] for key in sorted(invalid)])
    by_id={r['id']:r for r in rows};results=[];representatives=[]
    for members in groups:
        representative=members[0];members=sorted(members)
        group_id='graphene-group-'+_canonical({'context':context,'observations':members})[:20]
        evidence=[{'observation_ids':[a,b],**pair(a,b)} for n,a in enumerate(members) for b in members[n+1:]]
        result={'id':group_id,'representative_id':representative,'observation_ids':members,'observation_count':len(members),
            'status':'possible_same_object' if len(members)>1 else 'single_observation',
            'representative_quality':prepared[representative]['quality'] if representative in prepared else None,
            'ineligible_reason':invalid.get(representative),'pair_evidence':evidence}
        results.append(result)
        representatives.append({**by_id[representative],'dedup_group_id':group_id,'observation_ids':members,'observation_count':len(members)})
    return {'schema':1,'method':'same_frame_polygon_overlap_color_complete_link_v1','context':context,
        'transform_sha256':_canonical(transform),'options':asdict(options),'groups':results,
        'representatives':representatives,'observations':rows,'observation_count':len(rows),'group_count':len(groups),
        'merged_observation_count':len(rows)-len(groups),'ineligible_observations':invalid,
        'pair_diagnostics':diagnostics,'pair_comparison_count':pair_count,'pair_reason_counts':reason_counts,
        'pair_diagnostics_truncated':pair_count>len(diagnostics),
        'qualification':'Reversible spatial/optical grouping proposals, not confirmed unique flakes or layers. '
            'Raster overlap and Gaussian color gates are engineering settings, not validated probabilities. '
            'Saved registration uncertainty is not a measured bound; no polygon dilation or centroid-only merge is used.'}
