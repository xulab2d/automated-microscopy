"""Attach conditional hBN evidence and image-bound operator annotations."""
from dataclasses import dataclass
import copy
import re
import numpy as np

from .hbn_thin_evidence import (compile_optical_model, score_thin_evidence,
                                MATCH_RADIUS2, MINIMUM_LOG_RATIO)


@dataclass(frozen=True)
class CompiledRegionEvidence:
    hbn: object
    alternatives: tuple
    annotations: tuple


def compile_region_evidence(model_bundle):
    """Compile root hBN curve model + alternative_models/operator_annotations."""
    hbn=compile_optical_model(model_bundle)
    if hbn.material!='hBN': raise ValueError('hBN model bundle required.')
    alternatives=tuple(compile_optical_model(m) for m in model_bundle.get('alternative_models',()))
    annotations=tuple(copy.deepcopy(model_bundle.get('operator_annotations',())))
    return CompiledRegionEvidence(hbn,alternatives,annotations)


def _tile(value):
    if isinstance(value,int) and not isinstance(value,bool) and value>=0:
        return f'tile_{value:06d}'
    value=str(value).split('/')[-1]
    match=re.fullmatch(r'tile_?(\d+)',value)
    return f'tile_{int(match[1]):06d}' if match else None


def attach_region_evidence(regions, labels, *, field, source_preview_sha256=None,
                           reduction, compiled, source_hashes=None, pixel_size_um=None):
    """Return (new regions, binding report); no source arrays/rows are mutated.

    ``reduction`` is analyzer metadata containing source_pixel_from_analysis_pixel
    (2x3 affine), source_shape_hw and analysis_shape_hw. Only exact tile+preview
    digest matches can bind operator points. Prefer source_raw_sha256 bound to
    source_hashes['tiles/<tile>.tiff']; legacy preview-only points need that digest.
    Conflicting labels fail closed for the affected region, while preserving it.
    """
    if not isinstance(compiled,CompiledRegionEvidence): raise TypeError('Compile the model bundle first.')
    labels=np.asarray(labels)
    if labels.ndim!=2 or not np.issubdtype(labels.dtype,np.integer) or np.any(labels<0):
        raise ValueError('Nonnegative integer region labels required.')
    affine=np.asarray(reduction['source_pixel_from_analysis_pixel'],float)
    source_shape=np.asarray(reduction['source_shape_hw'],int)
    if (affine.shape!=(2,3) or not np.isfinite(affine).all() or source_shape.shape!=(2,)
            or np.any(source_shape<=0) or tuple(reduction['analysis_shape_hw'])!=labels.shape
            or abs(np.linalg.det(affine[:,:2]))<1e-12):
        raise ValueError('Explicit invertible source-pixel affine and matching shapes required.')
    tile=_tile(field)
    if tile is None: raise ValueError('Unambiguous tile identity required.')
    row_ids=[int(r['instance_label']) for r in regions]
    if len(set(row_ids))!=len(row_ids) or any(i<=0 for i in row_ids):
        raise ValueError('Unique positive region instance labels required.')
    row_id_set=set(row_ids);bindings={};report=[]
    inverse=np.linalg.inv(affine[:,:2])
    for index,annotation in enumerate(compiled.annotations):
        if _tile(annotation.get('tile'))!=tile: continue
        item={'annotation_index':index,'tile':tile,'point_px':annotation.get('point_px')}
        expected_raw=annotation.get('source_raw_sha256')
        if expected_raw:
            actual_raw=(source_hashes or {}).get(f'tiles/{tile}.tiff')
            matched=bool(actual_raw and actual_raw==expected_raw)
            source_kind='raw'
        else:
            matched=bool(source_preview_sha256 and annotation.get('source_preview_sha256')==source_preview_sha256)
            source_kind='preview'
        if not matched:
            item['status']=f'source_{source_kind}_hash_unavailable_or_mismatch';report.append(item);continue
        if annotation.get('label') not in ('thin_positive','bulk') or not annotation.get('source'):
            item['status']='invalid_operator_annotation';report.append(item);continue
        try: point=np.asarray(annotation.get('point_px'),float)
        except (TypeError,ValueError): point=np.asarray([],float)
        if (point.shape!=(2,) or not np.isfinite(point).all() or
                np.any(point<-.5) or np.any(point>=source_shape[::-1]-.5)):
            item['status']='source_point_outside_image';report.append(item);continue
        reduced=inverse@(point-affine[:,2]);nearest=np.floor(reduced+.5).astype(int)
        if np.any(nearest<0) or nearest[0]>=labels.shape[1] or nearest[1]>=labels.shape[0]:
            item['status']='point_outside_analyzed_extent';report.append(item);continue
        # A click exactly on a cell boundary has no unique pixel assignment.
        if np.any(np.isclose(reduced-np.floor(reduced),.5,rtol=0,atol=1e-9)):
            item['status']='ambiguous_pixel_boundary';report.append(item);continue
        region=int(labels[nearest[1],nearest[0]])
        if region==0 or region not in row_id_set:
            item['status']='point_has_no_measured_region';report.append(item);continue
        bound={**copy.deepcopy(annotation),'bound_instance_label':region,
               'analysis_pixel_xy':nearest.tolist(),'field':field}
        bindings.setdefault(region,[]).append(bound)
        item.update(status='bound',instance_label=region,analysis_pixel_xy=nearest.tolist());report.append(item)
    output=[]
    for row in regions:
        number=int(row['instance_label']);bound=bindings.get(number,[])
        conflict=len({a['label'] for a in bound})>1
        annotation=None if not bound or conflict else {**bound[0],'binding_count':len(bound)}
        evidence=score_thin_evidence(row['contrast_rgb'],row['contrast_covariance'],compiled.hbn,
            alternative_models=compiled.alternatives,operator_annotation=annotation)
        evidence['annotation_bindings']=bound
        evidence['measurement_source']={'field':field,'instance_label':number,
            'source_preview_sha256':source_preview_sha256,'source_hashes':copy.deepcopy(source_hashes or {}),
            'source_pixel_from_analysis_pixel':affine.tolist()}
        if conflict:
            evidence.update(passes=False,thin_supported=False,operator_supported=False)
            evidence['reasons'].append('conflicting_operator_annotations')
        output.append({**row,'hbn_screen':evidence})
    context_report=None
    pixel_size_um=pixel_size_um if pixel_size_um is not None else reduction.get('boundary_resolution_um')
    if pixel_size_um is not None:
        output,context_report=apply_bulk_context(output,labels,pixel_size_um)
    return output,{'schema':1,'field':field,'model_sha256':compiled.hbn.digest,
                   'source_preview_sha256':source_preview_sha256,'annotations':report,
                   'bulk_context':context_report,
                   'bound_annotations':sum(x['status']=='bound' for x in report)}


def apply_bulk_context(regions, labels, pixel_size_um, *, max_distance_um=2.,
                       minimum_support_area_um2=25.):
    """Flag proximity to substantial regions favoring a thicker explanation.

    This is an ambiguity diagnostic, not a material identity or shape veto.
    A context anchor can have poor absolute fit: that fact is retained explicitly.
    No propagation through neighbors, mask filling or source-region deletion.
    Pixel sizes are (x,y); distances use measured mask pixel centers.
    """
    from scipy.ndimage import distance_transform_edt, minimum, find_objects
    labels=np.asarray(labels);pixel=np.asarray(pixel_size_um,float)
    if (labels.ndim!=2 or not np.issubdtype(labels.dtype,np.integer) or np.any(labels<0)
            or pixel.shape!=(2,) or not np.isfinite(pixel).all() or np.any(pixel<=0)
            or not np.isfinite([max_distance_um,minimum_support_area_um2]).all()
            or max_distance_um<0 or minimum_support_area_um2<=0):
        raise ValueError('Valid labels, physical pixel sizes and context thresholds required.')
    ids=[int(row['instance_label']) for row in regions]
    if len(ids)!=len(set(ids)) or any(i<=0 for i in ids):raise ValueError('Unique positive region labels required.')
    counts=np.bincount(labels.ravel());anchors={}
    for row in regions:
        number=int(row['instance_label']);screen=row.get('hbn_screen',{});metrics=screen.get('metrics',{})
        area=float(counts[number]*np.prod(pixel)) if number<len(counts) else 0.
        if area<minimum_support_area_um2:continue
        operator_bulk=any(a.get('label')=='bulk' for a in screen.get('annotation_bindings',[]))
        operator_bulk=operator_bulk or (screen.get('operator_annotation') or {}).get('label')=='bulk'
        if screen.get('operator_supported') and not operator_bulk:continue
        ratio=metrics.get('target_vs_bulk_log_ratio');best=metrics.get('best_bulk') or {}
        favored=bool(ratio is not None and np.isfinite(ratio) and ratio<=-MINIMUM_LOG_RATIO)
        if not (operator_bulk or favored):continue
        distance2=best.get('distance2')
        anchors[number]={'instance_label':number,'measured_area_um2':area,
            'basis':'operator_bulk' if operator_bulk else 'relative_thicker_optical_explanation',
            'absolute_model_match':bool(distance2 is not None and np.isfinite(distance2) and distance2<=MATCH_RADIUS2),
            'bulk_distance2':distance2,'conditional_best_bulk_nm':best.get('conditional_best_nm'),
            'target_vs_bulk_log_ratio':ratio,'chemical_identity_confirmed':False}
    distances=None;nearest=None;minima={};sections=None
    if anchors:
        support=np.isin(labels,list(anchors))
        distances,indices=distance_transform_edt(~support,sampling=pixel[::-1],return_indices=True)
        nearest=labels[indices[0],indices[1]]
        minima=dict(zip(ids,minimum(distances,labels,index=ids)))
        sections=find_objects(labels)
    output=[];flagged=[]
    for row in regions:
        number=int(row['instance_label']);new={**row};screen=copy.deepcopy(row.get('hbn_screen',{}))
        if not screen:output.append(new);continue
        # Context anchors already carry their own evidence; don't self-flag.
        ambiguous=bool(number not in anchors and minima.get(number,float('inf'))<=max_distance_um)
        evidence=None
        if ambiguous:
            section=sections[number-1];local=labels[section]==number
            eligible=np.where(local,distances[section],np.inf)
            point=np.unravel_index(np.argmin(eligible),eligible.shape)
            anchor=int(nearest[section][point]);evidence={**anchors[anchor],
                'distance_um':float(eligible[point])}
            screen['passes']=False;screen['thin_supported']=False
            if 'nearby_thicker_explanation_requires_review' not in screen['reasons']:
                screen['reasons'].append('nearby_thicker_explanation_requires_review')
            flagged.append(number)
        screen['bulk_context_ambiguous']=ambiguous
        screen['bulk_context']={'policy_version':'hbn-adjacent-thicker-context-v1',
            'max_distance_um':float(max_distance_um),'minimum_support_area_um2':float(minimum_support_area_um2),
            'minimum_log_ratio':MINIMUM_LOG_RATIO,'nearest_context':evidence,
            'qualification':'Adjacent measured feature favors a thicker explanation; mixed-stack context needs review. '
                'Poor absolute model fit remains unknown, not identified bulk. Operator-positive retrieval is retained.'}
        new['hbn_screen']=screen;output.append(new)
    return output,{'policy_version':'hbn-adjacent-thicker-context-v1','anchor_count':len(anchors),
                   'flagged_region_ids':flagged,'max_distance_um':float(max_distance_um),
                   'minimum_support_area_um2':float(minimum_support_area_um2)}
