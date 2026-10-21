"""Single-pass, checkpointed analysis of faint connected color plateaus."""
from dataclasses import asdict
from pathlib import Path
import hashlib,json,time
import cv2
import numpy as np

from .imaging import load_frame,background_surface
from .model import AcquisitionError,atomic_json
from .scan import deduplicate,export_csv
from .thin_regions import ThinRegionOptions,propose_thin_regions
from .thin_contrast import THIN_PRIOR,thin_contrast_evidence


def _geometry(labels,label,evidence,frame,transform):
    x,y,w,h=evidence['bbox_px'];mask=np.uint8(labels[y:y+h,x:x+w]==label)
    contours,hierarchy=cv2.findContours(mask,cv2.RETR_CCOMP,cv2.CHAIN_APPROX_SIMPLE)
    roots=[i for i,h in enumerate(hierarchy[0]) if h[3]<0]
    index=max(roots,key=lambda i:cv2.contourArea(contours[i]))
    def polygon(c):return (cv2.approxPolyDP(c,.5,True)[:,0,:]+[x,y]).tolist()
    outer=polygon(contours[index]);holes=[polygon(c) for i,c in enumerate(contours) if hierarchy[0,i,3]==index and len(c)>=3]
    # Use an inscribed diameter for the useful-width screen, not a rectangle
    # spanning narrow branches or empty space between them.
    distance=cv2.distanceTransform(cv2.copyMakeBorder(mask,1,1,1,1,cv2.BORDER_CONSTANT,value=0),cv2.DIST_L2,5)
    width=2*float(distance.max())*min(frame.pixel_size_um)
    return {'polygon_px':outer,'holes_px':holes,'usable_width_um':width,
            'stage_polygon_um':[transform.stage_xy(p,frame).tolist() for p in outer],
            'stage_xy_um':transform.stage_xy(evidence['centroid_px'],frame).tolist(),
            'touches_border':bool(x==0 or y==0 or x+w>=labels.shape[1] or y+h>=labels.shape[0])}


def analyze_thin_scan(folder,options=None,output=None,callback=None,check_cancel=None):
    from .analysis import tile_inputs,tile_region,focus_provenance
    from .review import build_review
    from .storage import managed_output_path
    folder=Path(folder).resolve();out=managed_output_path(output if output else folder/'analysis-thin-contrast')
    out.mkdir(parents=True,exist_ok=True);started=time.perf_counter()
    settings=dict(options or {});settings.pop('backend',None)
    area=float(settings.pop('min_area_um2',100));width=float(settings.pop('min_width_um',5))
    margin=float(settings.pop('edge_margin_um',12));profile=settings.pop('thin_profile',None) or THIN_PRIOR
    if isinstance(profile,(str,Path)):profile=json.loads(Path(profile).read_text(encoding='utf-8'))
    region_options=dict(settings.pop('regions',{}));region_options['useful_area_um2']=area
    if settings:raise ValueError('Unknown thin-analysis settings: '+', '.join(sorted(settings)))
    if not np.isfinite([area,width,margin]).all() or min(area,width)<=0 or margin<0:raise ValueError('Invalid physical area, width or margin.')
    regions=ThinRegionOptions(**region_options);items=tile_inputs(folder)
    for item in items:
        context=item['manifest'].get('context',{})
        canonical=lambda s:str(s).lower().replace('₂','2').removeprefix('2h-')
        if (canonical(context.get('material',''))!=canonical(profile['material'])
                or context.get('oxide_nm')!=profile['nominal_oxide_nm']
                or context.get('substrate')!=profile['substrate']):
            raise AcquisitionError('The thin-contrast prior does not match the material and substrate context.')
    config={'algorithm':'connected-plateaus-rg-prior-v1','regions':asdict(regions),'prior':profile,
            'minimum_width_um':width,'edge_margin_um':margin}
    fingerprint=hashlib.sha256(json.dumps(config,sort_keys=True).encode()).hexdigest()
    atomic_json(out/'material-profile.json',profile)
    rows=[];tiles=[];failures=[];reused=0
    for i,item in enumerate(items):
        if check_cancel:check_cancel()
        key=item['key'];slug=key.replace('/','__');tick=time.perf_counter()
        try:
            # Include raw bytes, metadata, geometry and acquisition provenance.
            # A missing/unreadable sidecar is a field failure, not a batch abort.
            digest=hashlib.sha256(item['path'].read_bytes()+item['path'].with_suffix('.json').read_bytes())
            digest.update(json.dumps(item['manifest'],sort_keys=True).encode());identity=digest.hexdigest()
            cache=out/(slug+'.thin.json');saved=None
            if cache.exists():
                try:
                    candidate=json.loads(cache.read_text(encoding='utf-8'))
                    if candidate.get('source_identity')==identity and candidate.get('fingerprint')==fingerprint:
                        if all((out/candidate['tile'][k]).is_file() for k in ('instance_mask','contrast_rg_file')):saved=candidate
                except (OSError,ValueError,KeyError,TypeError):pass
            if saved is None:
                frame=load_frame(item['path']);item['transform'].validate_frame(frame)
                valid=tile_region(item,frame)
                if valid is None:valid=np.ones(frame.rgb.shape[:2],bool)
                radius=int(np.ceil(margin/min(frame.pixel_size_um)))
                if radius:valid=cv2.erode(np.uint8(valid),np.ones((2*radius+1,2*radius+1),np.uint8)).astype(bool)
                background=background_surface(frame.rgb,valid)
                contrast=(background-frame.rgb)/background
                labels,evidence,report=propose_thin_regions(frame,contrast,valid,regions)
                if len(evidence)>65535:raise AcquisitionError('Too many region labels for a lossless uint16 map.')
                focus,focus_limited=focus_provenance(item,frame);h,w=labels.shape
                tile={'key':key,'tile_id':item['tile_id'],'chip_id':item['chip_id'],
                    'source':item['path'].relative_to(folder).as_posix(),'position':asdict(frame.position),
                    'shape':[h,w],'pixel_size_um':list(frame.pixel_size_um),'sha256':hashlib.sha256(frame.rgb.tobytes()).hexdigest(),
                    'footprint_um':[item['transform'].stage_xy(p,frame).tolist() for p in [[0,0],[w-1,0],[w-1,h-1],[0,h-1]]],
                    'focus_status':focus,'focus_limited':focus_limited,'acquired_at':frame.acquired_at,
                    'radiometry_status':frame.metadata.get('radiometry_status','Per-frame camera processing unverified; literature transfer is a search prior.')}
                found=[]
                for label,e in evidence.items():
                    prior=thin_contrast_evidence(e['contrast_rgb'],e['contrast_covariance'],profile)
                    geometry=_geometry(labels,label,e,frame,item['transform'])
                    useful=e['area_um2']>=area and geometry['usable_width_um']>=width
                    eligible=bool(prior['compatible'] and useful and not focus_limited)
                    reasons=[]
                    if e['area_um2']<area:reasons.append('below_useful_area')
                    if geometry['usable_width_um']<width:reasons.append('below_useful_width')
                    if not prior['compatible']:reasons.append('outside_or_unresolved_thin_contrast_prior')
                    if focus_limited:reasons.append('focus_unverified')
                    status='material_like' if prior['compatible'] else 'uncertain'
                    row={**e,**geometry,'id':'f-'+hashlib.sha256((key+'|'+fingerprint+'|'+str(label)).encode()).hexdigest()[:14],
                        'instance_label':label,'source_image':tile['source'],'tile_key':key,'tile_id':item['tile_id'],'chip_id':item['chip_id'],
                        'disposition':'candidate' if eligible else 'rejected','filter_reasons':reasons,
                        'color_group':'thin_contrast' if prior['compatible'] else 'unresolved',
                        'label':'thin-contrast candidate' if prior['compatible'] else 'unclassified color plateau',
                        'priority':'thin_screen' if eligible else 'review_required' if useful else 'rejected',
                        'quality_score':prior['score'],'focus_limited':focus_limited,'focus_status':focus,
                        'thin_screen':{**prior,'status':'thin_screen' if eligible else 'review_required'},
                        'thin_screen_area_um2':e['area_um2'] if eligible else 0.,'useful_area_um2':e['area_um2'] if useful else 0.,
                        'material_selection':{'status':status,'class_name':'thin_reference_direction' if prior['compatible'] else 'unresolved_plateau',
                            'reason':prior['reason'],'model_id':profile['model_id'],'catalog_selected':eligible,
                            'supported_area_um2':e['area_um2'] if prior['compatible'] else 0.,
                            'largest_supported_area_um2':e['area_um2'] if prior['compatible'] else 0.,
                            'contrast_rgb':e['contrast_rgb'],'contrast_covariance':e['contrast_covariance'],
                            'measurement_method':'Separate spatial partition; local substrate annulus and sector uncertainty',
                            'layer_number':None,'chemical_identity_confirmed':False}}
                    found.append(row)
                mask_name=slug+'.instances.png';map_name=slug+'.contrast-rg.npz'
                if not cv2.imwrite(str(out/mask_name),np.uint16(labels)):raise OSError('Could not save exact instance labels.')
                np.savez_compressed(out/map_name,contrast_rg=cv2.resize(contrast[:,:,:2],(max(1,w//4),max(1,h//4)),interpolation=cv2.INTER_AREA),original_shape_hw=np.array([h,w]))
                tile.update(instance_mask=mask_name,contrast_rg_file=map_name,regions=len(found),analysis_seconds=time.perf_counter()-tick)
                saved={'fingerprint':fingerprint,'source_identity':identity,'tile':tile,'rows':found,'report':report}
                atomic_json(cache,saved)
            else:reused+=1
            tiles.append(saved['tile']);rows.extend(saved['rows'])
        except (AcquisitionError,ValueError,OSError,cv2.error) as exc:
            failures.append({'tile_key':key,'error':str(exc)})
        atomic_json(out/'progress.json',{'state':'analysing','completed':i+1,'total':len(items),'errors':failures,'reused_fields':reused})
        if callback:callback('Segmenting connected color plateaus',{'index':i+1,'total':len(items)})
    accepted=deduplicate([r for r in rows if r['disposition']=='candidate'])
    rejected=[r for r in rows if r['disposition']=='rejected'];all_rows=accepted+rejected
    catalog_ids={r['id']:r['id'] for r in accepted}
    for r in accepted:
        for observation in r.get('overlap_observations',[]):catalog_ids[observation['id']]=r['id']
    for r in rows:r['catalog_id']=catalog_ids.get(r['id'])
    dataset_id=hashlib.sha256((fingerprint+''.join(t['key']+t['sha256'] for t in tiles)).encode()).hexdigest()[:20]
    summary={'schema':2,'backend':'thin_regions','dataset_id':dataset_id,'source_folder':str(folder),'tiles':tiles,
        'options':{'min_area_um2':area,'min_width_um':width,'edge_margin_um':margin,'regions':asdict(regions)},
        'raw_regions':len(rows),'candidate_count':len(accepted),'rejected_count':len(rejected),'thin_screen_count':len(accepted),
        'distinct_images':len({t['sha256'] for t in tiles}),'failed_tiles':failures,'reused_fields':reused,
        'analysis_seconds':time.perf_counter()-started,'color_model':{'groups':[]},
        'validation_status':'unvalidated_thin_contrast_candidates','warning':profile['qualification'],
        'material_selection':{'model_id':profile['model_id'],'profile':'material-profile.json','catalog_selected_count':len(accepted),
            'catalog_status_counts':{s:sum(r['area_um2']>=area and r['material_selection']['status']==s for r in all_rows) for s in ('material_like','uncertain','excluded')},
            'qualification':profile['qualification']}}
    atomic_json(out/'analysis.json',summary)
    atomic_json(out/'flakes.json',{'flakes':all_rows,'analysis_schema':2,'dataset_id':dataset_id,'source_folder':str(folder)})
    atomic_json(out/'observations.json',{'schema':1,'observations':rows,'physical_unique_count':len(accepted),
        'source_folder':str(folder),'detection_min_area_um2':regions.minimum_region_area_um2,'catalog_min_area_um2':area,
        'physical_unique_count_scope':'selected thin-contrast catalog; thickness unconfirmed'})
    export_csv(out/'flakes.csv',all_rows)
    if tiles:
        previous_path=out/'review-data.json'
        if reused==len(items) and not failures and previous_path.is_file():
            previous=json.loads(previous_path.read_text(encoding='utf-8'))
            if previous.get('dataset_id')==dataset_id:
                from .review import _write_review
                enriched={t['key']:t for t in previous['summary']['tiles']}
                summary['tiles']=[{**enriched[t['key']],**t} for t in tiles]
                previous['summary']=summary
                _write_review(out,previous)
            else:build_review(out)
        else:build_review(out)
    summary['total_seconds']=time.perf_counter()-started
    atomic_json(out/'analysis.json',summary)
    atomic_json(out/'progress.json',{'state':'complete_with_errors' if failures else 'complete','completed':len(items),
        'total':len(items),'errors':failures,'reused_fields':reused,'candidate_count':len(accepted)})
    return summary
