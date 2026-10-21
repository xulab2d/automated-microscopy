"""Broad optical features followed by independent, reviewable measurement sieves.

The accepted component-repair graph-cut implementation owns geometry. Neither
color measurement nor a literature prior can add/delete pixels in these masks.
All derived files live outside the acquisition-hashed tiles directory.
"""
from dataclasses import dataclass,asdict,field
from pathlib import Path
import hashlib,json,time

import cv2
import numpy as np
from scipy.ndimage import find_objects,center_of_mass

from .analysis import tile_inputs,tile_region,focus_provenance,_refine_objects
from .colors import AnalysisOptions,prepare_contrast
from .imaging import load_frame
from .measure_existing_regions import ExistingRegionOptions,measure_existing_regions
from .model import AcquisitionError,Cancelled,atomic_json
from .scan import deduplicate
from .thin_contrast import THIN_PRIOR,thin_contrast_evidence


@dataclass
class StagedFeatureOptions:
    extraction: dict = field(default_factory=lambda:asdict(AnalysisOptions()))
    measurement: dict = field(default_factory=lambda:asdict(ExistingRegionOptions()))
    minimum_local_signal_z: float = 3.
    publish_every: int = 1

    def __post_init__(self):
        AnalysisOptions(**self.extraction);ExistingRegionOptions(**self.measurement)
        if not np.isfinite(self.minimum_local_signal_z) or self.minimum_local_signal_z<=0:
            raise ValueError('Local contrast significance must be positive.')
        if not isinstance(self.publish_every,int) or self.publish_every<1:raise ValueError('Invalid publication interval.')


def file_hash(path):
    digest=hashlib.sha256()
    with Path(path).open('rb') as stream:
        for block in iter(lambda:stream.read(1024*1024),b''):digest.update(block)
    return digest.hexdigest()


def pipeline_identity(options):
    sources={name:file_hash(Path(__file__).with_name(name)) for name in
        ('staged_features.py','analysis.py','colors.py','imaging.py','measure_existing_regions.py','thin_contrast.py')}
    config={'algorithm':'accepted-component-repair-separate-sieves-v1','options':asdict(options),'sources':sources,
        'thin_prior':THIN_PRIOR,'color_reference_policy':'No transfer of another optical profile; color categories are descriptive.'}
    return config,hashlib.sha256(json.dumps(config,sort_keys=True).encode()).hexdigest()


def _source_identity(item):
    hashes={suffix:file_hash(item['path'].with_suffix(suffix)) for suffix in ('.tiff','.json')}
    completed=item['manifest']['completed'][item['tile_id']]
    for suffix,digest in hashes.items():
        declared=completed.get('hashes',{}).get('tiles/'+item['tile_id']+suffix)
        if declared is not None and declared!=digest:raise AcquisitionError('Acquisition-hashed source changed: '+str(item['path'].with_suffix(suffix)))
    provenance={'source_files_sha256':hashes,'source_position':completed.get('position'),
        'tile_acquisition':completed,'transform':asdict(item['transform']),'plan':item['manifest']['plan'],
        'context':item['manifest'].get('context'),'acquisition_fingerprint':item['manifest'].get('fingerprint')}
    return provenance,hashlib.sha256(json.dumps(provenance,sort_keys=True).encode()).hexdigest()


def _geometry(labels,label,section,frame,transform):
    ys,xs=section;x,y=xs.start,ys.start;mask=np.uint8(labels[ys,xs]==label)
    contours,hierarchy=cv2.findContours(mask,cv2.RETR_CCOMP,cv2.CHAIN_APPROX_SIMPLE)
    roots=[i for i,h in enumerate(hierarchy[0]) if h[3]<0]
    outer=max(roots,key=lambda i:cv2.contourArea(contours[i]))
    poly=lambda c:(cv2.approxPolyDP(c,.5,True)[:,0,:]+[x,y]).tolist()
    polygon=poly(contours[outer]);holes=[poly(c) for i,c in enumerate(contours) if hierarchy[0,i,3]==outer]
    distance=cv2.distanceTransform(np.pad(mask,1),cv2.DIST_L2,5)[1:-1,1:-1]
    cy,cx=center_of_mass(mask);centre=[x+float(cx),y+float(cy)]
    contour_area=max(cv2.contourArea(contours[outer]),1.)
    return {'instance_label':int(label),'bbox_px':[x,y,xs.stop-x,ys.stop-y],'centroid_px':centre,
        'area_um2':float(mask.sum()*np.prod(frame.pixel_size_um)),
        'usable_width_um':2*float(distance.max())*min(frame.pixel_size_um),
        'polygon_px':polygon,'holes_px':holes,'stage_xy_um':transform.stage_xy(centre,frame).tolist(),
        'stage_polygon_um':[transform.stage_xy(p,frame).tolist() for p in polygon],
        'touches_border':bool(x==0 or y==0 or xs.stop==labels.shape[1] or ys.stop==labels.shape[0]),
        'solidity':float(contour_area/max(cv2.contourArea(cv2.convexHull(contours[outer])),1.)),
        'mask_area_pixels':int(mask.sum()),'geometry_method':'accepted component-repair graph cut; disconnected pieces separate; holes retained'}


def _color_category(contrast,prior):
    r,g,b=contrast
    if -.8<=r<=-.5 and -.8<=g<=-.5:return 'bulk_positive_reference'
    if prior.get('compatible'):return 'thin_reference_compatible'
    if r>0 and g>r:return 'unresolved_thin_direction'
    if r>0 and g>0:return 'dark_optical_feature'
    if r>0 and g<0:return 'cyan_optical_feature'
    if r<0 and g<0:return 'bright_optical_feature'
    return 'other_optical_color'


def apply_sieves(row,measurement,options,focus_limited=False):
    """Append decisions without modifying the source geometry or its area."""
    extraction=AnalysisOptions(**options.extraction);area=row['area_um2'];width=row['usable_width_um']
    size_ok=bool(area>=extraction.min_area_um2 and width>=extraction.min_width_um)
    reasons=[]
    if area<extraction.min_area_um2:reasons.append('below_useful_area')
    if width<extraction.min_width_um:reasons.append('below_useful_width')
    measured=measurement.get('measurement_status')=='measured'
    prior={'compatible':None,'status':'not_measured','score':0.,'reason':measurement.get('reason'),
           'model_id':THIN_PRIOR['model_id'],'layer_number':None,'chemical_identity_confirmed':False}
    signal=None
    if measured:
        contrast=np.asarray(measurement['contrast_rgb']);cov=np.asarray(measurement['contrast_covariance'])
        signal=float(np.sqrt(max(0,contrast@np.linalg.solve(cov,contrast))))
        prior=thin_contrast_evidence(contrast,cov)
        row['contrast_rgb']=contrast.tolist()
    separated=None if signal is None else signal>=options.minimum_local_signal_z
    if separated is False:reasons.append('local_contrast_not_resolved_from_substrate')
    selected=bool(size_ok and separated is not False)
    category=_color_category(row['contrast_rgb'],prior)
    warnings=[]
    if not measured:warnings.append('local_color_measurement_unavailable')
    if row['touches_border']:warnings.append('source_field_clips_object')
    if area>extraction.max_area_um2:warnings.append('very_large_mixed_region_review_required')
    if focus_limited:warnings.append('recorded_focus_not_verified')
    if row.get('saturated_fraction',0)>0:warnings.append('some_object_pixels_saturated')
    stage='below_useful_size' if not size_ok else 'background_unresolved' if separated is False else 'thin_contrast_candidate' if prior.get('compatible') else 'bulk_reference' if category=='bulk_positive_reference' else 'feature_review'
    row.update(proposal_stage=stage,color_group=category,label=category.replace('_',' '),
        disposition='candidate' if selected else 'rejected',selected_for_review=selected,filter_reasons=reasons,
        evidence_warnings=warnings,local_contrast_measurement=measurement,thin_reference_evidence=prior,
        local_contrast_signal_z=signal,review_role='bulk_positive_control' if category=='bulk_positive_reference' else 'faint_unvalidated',
        priority='thin_screen' if selected and prior.get('compatible') else 'review_required' if selected else 'rejected',
        quality_score=min(signal or 0.,50.)/50.,useful_area_um2=area if size_ok else 0.,
        layer_number=None,chemical_identity_confirmed=False,
        sieve_results=[
            {'name':'feature_extraction','status':'pass','reason':'Connected foreground refined with accepted component-repair geometry.'},
            {'name':'useful_size','status':'pass' if size_ok else 'reject','minimum_area_um2':extraction.min_area_um2,'minimum_width_um':extraction.min_width_um},
            {'name':'local_substrate_contrast','status':'unmeasured' if separated is None else 'pass' if separated else 'reject',
             'signal_z':signal,'minimum_signal_z':options.minimum_local_signal_z,'reason':measurement.get('reason')},
            {'name':'optical_color','status':'descriptive','category':category,'material_identity_confirmed':False},
            {'name':'thin_reference','status':'compatible' if prior.get('compatible') else 'unmeasured' if not measured else 'outside_or_unresolved',
             'role':'separate prioritization; does not remove source features','layer_number':None}])
    return row


def analyze_feature_frame(item,frame,options,fingerprint):
    start=time.perf_counter();extract=AnalysisOptions(**options.extraction)
    prepared=prepare_contrast(frame,extract,tile_region(item,frame));prepared_time=time.perf_counter()-start
    labels=_refine_objects(frame,prepared,extract,return_labels=True)
    if labels.max()>65535:raise AcquisitionError('More than65535 source instances cannot fit the exact label image.')
    segmentation_time=time.perf_counter()-start-prepared_time
    measurements,report=measure_existing_regions(frame,prepared['contrast'],labels,prepared['valid'],
        ExistingRegionOptions(**options.measurement),additional_foreground=prepared['foreground'])
    focus,focus_limited=focus_provenance(item,frame);rows=[];white=float(frame.metadata.get('white_level',65535))
    for label,section in enumerate(find_objects(labels),1):
        if section is None:continue
        row=_geometry(labels,label,section,frame,item['transform']);mask=labels[section]==label
        contrast=np.median(prepared['contrast'][section][mask],axis=0)
        row.update(id='f-'+hashlib.sha256((item['key']+'|'+fingerprint+'|'+str(label)).encode()).hexdigest()[:14],
            source_image=item['path'].relative_to(item['path'].parent.parent).as_posix(),source_tile=item['tile_id'],
            tile_key=item['key'],tile_id=item['tile_id'],chip_id=item['chip_id'],contrast_rgb=contrast.tolist(),
            source_contrast_rgb=contrast.tolist(),focus_status=focus,focus_limited=focus_limited,
            saturated_fraction=float(np.mean(np.any(frame.rgb[section][mask]>=white,axis=1))))
        rows.append(apply_sieves(row,measurements[label],options,focus_limited))
    report.update(normalization_seconds=prepared_time,segmentation_seconds=segmentation_time,
        total_seconds=time.perf_counter()-start,foreground_fraction=prepared['foreground_fraction'],
        substrate_mean=prepared['substrate_mean'].tolist(),substrate_covariance=prepared['substrate_covariance'].tolist(),
        raw_noise_rgb=prepared['raw_noise_rgb'].tolist(),mask_geometry_unchanged_by_sieves=True)
    return rows,labels,prepared,focus,focus_limited,report


def _deduplicate_catalog(rows):
    """Use the existing pair rule, but inspect only nearby spatial buckets."""
    accepted=[];buckets={}
    for row in sorted(rows,key=lambda r:(r['touches_border'],-r['area_um2'])):
        xy=np.asarray(row['stage_xy_um']);cell=tuple(np.floor(xy/5).astype(int));matched=False
        for dx in (-1,0,1):
            if matched:break
            for dy in (-1,0,1):
                if matched:break
                for index in buckets.get((row.get('chip_id'),cell[0]+dx,cell[1]+dy),[]):
                    merged=deduplicate([accepted[index],row])
                    if len(merged)==1:accepted[index]=merged[0];matched=True;break
        if not matched:
            buckets.setdefault((row.get('chip_id'),*cell),[]).append(len(accepted));accepted.append(row.copy())
    return accepted


def _publish(out,folder,config,fingerprint,checkpoints,total,state,errors,started,reused):
    tiles=[saved['tile'] for saved in checkpoints.values()]
    # The browser reads per-field rows lazily; no source observation is hidden
    # behind the size-filtered compact catalog or overlap deduplication.
    selected=[row for saved in checkpoints.values() for row in saved['rows'] if row['selected_for_review']]
    catalog=_deduplicate_catalog(selected)
    summary={'schema':2,'backend':'staged_component_features','dataset_id':fingerprint[:20],
        'source_folder':str(folder),'created_at':time.time(),'options':config['options'],'tiles':tiles,
        'raw_regions':sum(t['proposal_count'] for t in tiles),'candidate_count':len(catalog),
        'selected_observation_count':len(selected),'rejected_count':sum(t['rejected_count'] for t in tiles),
        'thin_screen_count':sum(r['thin_reference_evidence'].get('compatible') is True for r in catalog),
        'bulk_reference_count':sum(r['proposal_stage']=='bulk_reference' for r in catalog),
        'color_model':{'groups':[],'method':'Descriptive RGB directions; no transferred material whitelist.'},
        'stage_counts':{stage:sum(t['stage_counts'].get(stage,0) for t in tiles) for stage in
            ('below_useful_size','background_unresolved','thin_contrast_candidate','bulk_reference','feature_review')},
        'analysis_state':state,'total_tiles':total,'completed_tiles':len(tiles),'failed_tiles':errors,
        'validation_status':'broad_optical_features_with_reviewable_sieves',
        'warning':'Optical proposals include debris. Separate useful-size, local-contrast, and color sieves preserve all exact source masks. Literature compatibility does not confirm material or thickness. Tilt may leave regions outside local focus.'}
    atomic_json(out/'analysis.json',summary)
    atomic_json(out/'flakes.json',{'flakes':catalog,'dataset_id':fingerprint[:20],'source_folder':str(folder),'analysis_schema':2})
    atomic_json(out/'observations.json',{'schema':2,'storage':'per_tile_shards','source_folder':str(folder),
        'shards':[{'tile_key':t['key'],'file':t['observations_file'],'count':t['proposal_count']} for t in tiles],
        'observation_count':summary['raw_regions'],'physical_unique_count':len(catalog),
        'physical_unique_count_scope':'size/local-contrast review catalog only; all source masks retained in shards'})
    atomic_json(out/'analysis-progress.json',{'state':state,'completed_tiles':len(tiles),'total_tiles':total,
        'error':errors[-1]['error'] if errors else None,'errors':errors,'reused_fields':reused,
        'elapsed_seconds':time.perf_counter()-started,'updated_at':time.time()})
    return summary


def analyze_staged_scan(folder,output=None,options=None,tile_ids=None,max_new_tiles=None,callback=None,check_cancel=None):
    """Incrementally analyze committed raw fields with single-frame memory use.

    Resume requires exact raw/sidecar checksums, acquisition identity, code,
    options and saved mask checksum. No acquisition file is modified.
    """
    from .storage import managed_output_path
    folder=Path(folder).resolve();out=managed_output_path(output if output else folder/'analysis-staged-features')
    if out==folder or out.is_relative_to(folder/'tiles'):raise ValueError('Derived output cannot replace acquisition files.')
    options=options or StagedFeatureOptions();config,fingerprint=pipeline_identity(options)
    # Dataset identity is stable as additional immutable fields are appended.
    fingerprint=hashlib.sha256((str(folder)+'|'+fingerprint).encode()).hexdigest()
    out.mkdir(parents=True,exist_ok=True);(out/'fields').mkdir(exist_ok=True)
    atomic_json(out/'pipeline.json',{**config,'fingerprint':fingerprint})
    items=tile_inputs(folder);selected_items=[item for item in items if tile_ids is None or item['tile_id'] in tile_ids]
    available={item['key']:item for item in items};checkpoints={};errors=[];reused=0;new=0;started=time.perf_counter()
    # Include already committed compatible shards in every partial publication.
    for path in (out/'fields').glob('*.json'):
        saved=json.loads(path.read_text(encoding='utf-8'))
        if saved.get('fingerprint')==fingerprint and saved['tile']['key'] in available:checkpoints[saved['tile']['key']]=saved
    def check():
        if check_cancel:check_cancel()
        if (out/'STOP').exists():raise Cancelled('Offline analysis STOP requested.')
    summary=None
    try:
        for item in selected_items:
            check();key=item['key'];slug=key.replace('/','__');tick=time.perf_counter()
            atomic_json(out/'analysis-progress.json',{'state':'analysing','completed_tiles':len(checkpoints),'total_tiles':len(items),
                'processing_tile':key,'updated_at':time.time(),'error':None})
            try:
                provenance,identity=_source_identity(item);saved=checkpoints.get(key)
                if saved and saved.get('source_identity')==identity and file_hash(out/saved['tile']['instance_mask'])==saved['mask_sha256']:
                    reused+=1;continue
                if max_new_tiles is not None and new>=max_new_tiles:break
                frame=load_frame(item['path']);item['transform'].validate_frame(frame)
                rows,labels,prepared,focus,focus_limited,report=analyze_feature_frame(item,frame,options,fingerprint)
                h,w=labels.shape;mask_name=slug+'.instances.png';map_name=slug+'.contrast-rg.npz'
                temporary=out/(slug+'.tmp.png')
                if not cv2.imwrite(str(temporary),np.uint16(labels)):raise OSError('Could not write instance labels.')
                temporary.replace(out/mask_name)
                np.savez_compressed(out/map_name,contrast_rg=cv2.resize(prepared['contrast'][:,:,:2],(max(1,w//4),max(1,h//4)),interpolation=cv2.INTER_AREA),original_shape_hw=np.array([h,w]))
                tile={'key':key,'tile_id':item['tile_id'],'chip_id':item['chip_id'],'source':item['path'].relative_to(folder).as_posix(),
                    'position':asdict(frame.position),'shape':[h,w],'pixel_size_um':list(frame.pixel_size_um),
                    'sha256':hashlib.sha256(frame.rgb.tobytes()).hexdigest(),'acquired_at':frame.acquired_at,
                    'footprint_um':[item['transform'].stage_xy(p,frame).tolist() for p in [[0,0],[w-1,0],[w-1,h-1],[0,h-1]]],
                    'focus_status':focus,'focus_limited':focus_limited,'instance_mask':mask_name,'contrast_rg_file':map_name,
                    'observations_file':'fields/'+slug+'.json','proposal_count':len(rows),
                    'selected_count':sum(r['selected_for_review'] for r in rows),'rejected_count':sum(not r['selected_for_review'] for r in rows),
                    'stage_counts':{stage:sum(r['proposal_stage']==stage for r in rows) for stage in {r['proposal_stage'] for r in rows}},
                    'analysis_seconds':time.perf_counter()-tick}
                saved={'schema':1,'fingerprint':fingerprint,'source_identity':identity,'provenance':provenance,
                    'mask_sha256':file_hash(out/mask_name),'tile':tile,'rows':rows,'report':report}
                atomic_json(out/tile['observations_file'],saved);checkpoints[key]=saved;new+=1
                del frame,prepared,labels,rows
            except (AcquisitionError,OSError,ValueError,cv2.error) as exc:
                checkpoints.pop(key,None);errors.append({'tile_key':key,'error':str(exc)})
            if new%options.publish_every==0:
                summary=_publish(out,folder,config,fingerprint,checkpoints,len(items),'analysing',errors,started,reused)
            if callback:callback(key,new,len(items),checkpoints.get(key,{}).get('tile',{}))
        state='complete' if len(checkpoints)==len(items) and not errors else 'partial_with_errors' if errors else 'partial'
        summary=_publish(out,folder,config,fingerprint,checkpoints,len(items),state,errors,started,reused)
    except Cancelled:
        summary=_publish(out,folder,config,fingerprint,checkpoints,len(items),'stopped',errors,started,reused)
    return summary
