"""Versioned, isolated retry of failed fields. No production/data mutations."""
from observed_normalization_v2 import normalize_observed
from audit_saved_outputs import *
import argparse,time,ctypes,shutil
from dataclasses import asdict
from scipy.ndimage import find_objects,distance_transform_edt
from flakefinder.analysis import tile_inputs,focus_provenance,_refine_objects
from flakefinder.colors import AnalysisOptions
from flakefinder.staged_features import StagedFeatureOptions,_geometry,apply_sieves,_source_identity,_deduplicate_catalog
from flakefinder.measure_existing_regions import ExistingRegionOptions,_measure_one
from flakefinder.model import atomic_json

DEST=OUT/'recovery-v2'

def masked_gaussian(values,valid,sigma):
    weight=cv2.GaussianBlur(valid.astype(np.float32),(0,0),sigma)
    numerator=cv2.GaussianBlur(np.where(valid[:,:,None],values,0).astype(np.float32),(0,0),sigma)
    return numerator/np.maximum(weight[:,:,None],1e-8)

def prepare_observed(frame,item,options):
    path=CHIP/'boundary-labels'/(item['tile_id']+'.npz');recorded=np.load(path)['labels']
    contrast,background,valid,support,record=normalize_observed(frame,item['manifest'],recorded,sha(path),item['tile_id'],options.edge_margin_um)
    # NaN outside-domain scientific contrast is retained in saved data. Working
    # filter arrays are finite, and no outside pixel contributes to filtering.
    working=np.nan_to_num(contrast,nan=0.)
    median=np.stack([cv2.medianBlur(working[:,:,i],3) for i in range(3)],axis=2)
    median_valid=cv2.erode(np.uint8(valid),np.ones((3,3),np.uint8)).astype(bool)
    sigma=options.spatial_sigma_um/min(frame.pixel_size_um)
    smooth=masked_gaussian(median,median_valid,sigma)
    sample=smooth[support][::max(1,int(support.sum()/25000))].astype(float)
    centre=np.median(sample,axis=0);deviation=np.linalg.norm(sample-centre,axis=1)
    core=sample[deviation<=np.percentile(deviation,60)]
    covariance=np.cov(core.T)+np.eye(3)*.002**2
    residual=working-masked_gaussian(working,valid,1.)
    selected=residual[support]
    raw_noise=np.maximum(1.4826*np.median(np.abs(selected-np.median(selected,axis=0)),axis=0),.001)
    residual=smooth-masked_gaussian(smooth,valid,max(1,2*sigma));selected=residual[support]
    noise=np.maximum(1.4826*np.median(np.abs(selected-np.median(selected,axis=0)),axis=0),.001)
    delta=smooth-centre
    distance=np.einsum('...i,ij,...j->...',delta,np.linalg.inv(covariance),delta,optimize=True)
    foreground=valid&(distance>options.substrate_sigma**2)&(np.linalg.norm(delta,axis=2)>options.min_contrast)
    foreground=cv2.morphologyEx(np.uint8(foreground),cv2.MORPH_OPEN,np.ones((3,3),np.uint8)).astype(bool)
    record.update(filtering='3px median followed by domain-normalized Gaussian; invalid pixels never enter filter sums',
      noise_sample_policy='independently recorded substrate support, not all geometrically valid pixels')
    return {'contrast':contrast,'smooth':smooth,'foreground':foreground,'valid':valid,'background':background,
      'background_samples':support,'substrate_mean':centre,'substrate_covariance':covariance,
      'noise_rgb':noise,'raw_noise_rgb':raw_noise,'foreground_fraction':float(foreground.sum()/valid.sum()),'normalization':record}

def clip_instances(labels,valid,pixel_area,min_area):
    """Retain every component after mandatory observed-domain clipping."""
    output=np.zeros(labels.shape,np.int32);next_label=1;mapping={};removed=int(((labels>0)&~valid).sum())
    for old,section in enumerate(find_objects(labels),1):
        if section is None:continue
        local=np.uint8((labels[section]==old)&valid[section])
        count,parts,stats,_=cv2.connectedComponentsWithStats(local,8)
        for part in range(1,count):
            if stats[part,4]*pixel_area<min_area:continue
            output[section][parts==part]=next_label;mapping[next_label]=old;next_label+=1
    return output,{'pixels_clipped_outside_measured_domain':removed,'source_graphcut_label_by_final_label':mapping,
      'policy':'No shape or color geometry veto; domain clipping only, every surviving connected component retained.'}

def measure_with_observed_background(frame,prepared,labels,options):
    occupied=(labels>0)|prepared['foreground']
    distance=distance_transform_edt(~occupied,sampling=(frame.pixel_size_um[1],frame.pixel_size_um[0]))
    clear=prepared['valid']&prepared['background_samples']&~occupied&(distance>options.substrate_clearance_um)
    rows={}
    for label,section in enumerate(find_objects(labels),1):
        if section is None:continue
        row=_measure_one(prepared['contrast'],labels,clear,prepared['valid'],label,section,frame,options)
        row.update(background_sample_policy='recorded SUBSTRATE support within measured chip domain, excluding all source instances and initial foreground',
          observed_substrate_label_sha256=prepared['normalization']['label_sha256'],
          measured_domain_enclosure_sha256=prepared['normalization']['enclosure_sha256'])
        rows[label]=row
    return rows,clear

def qualify_mean_color(row,prepared):
    """Keep broad feature selection; do not promote whole-object mean to thin."""
    old=row['thin_reference_evidence'];measurement=row['local_contrast_measurement']
    row['mean_color_reference_evidence']=old
    row['thin_reference_evidence']={'compatible':None,'status':'not_classified',
      'mean_color_compatible':old.get('compatible'),'layer_number':None,'chemical_identity_confirmed':False,
      'reason':'Mean color is descriptive; no coherent thin terrace or material identity has been established.'}
    if old.get('compatible'):
        row['color_group']='mean_color_reference_match';row['label']='mean color match; coherent terrace unverified'
        if row['selected_for_review']:row['proposal_stage']='mean_color_match_review';row['priority']='review_required'
        row['evidence_warnings'].append('whole_object_mean_color_does_not_establish_uniform_thin_terrace')
    spread=measurement.get('interior_patch_spread_rgb')
    row['coherence_evidence']={'status':'descriptive_only','plateau_validated':False,
      'interior_patch_spread_rgb':spread,'regional_mean_standard_error_rgb':measurement.get('standard_error_rgb'),
      'raw_substrate_noise_rgb':prepared['raw_noise_rgb'].tolist(),
      'reason':'Spatial heterogeneity and uncertainty of a mean are distinct. No new arbitrary shape/uniformity threshold is applied.'}
    for sieve in row['sieve_results']:
        if sieve['name']=='thin_reference':
            sieve.update(status='descriptive_mean_only',role='Not thin classification; source geometry and broad review selection retained.')
    return row

def render_validation(frame,labels,rows,valid,tile):
    raw=preview(frame.rgb,frame.metadata['white_level']);overlay=raw.copy()
    contours,_=cv2.findContours(np.uint8(labels>0),cv2.RETR_LIST,cv2.CHAIN_APPROX_SIMPLE)
    cv2.drawContours(overlay,contours,-1,(255,100,100),2)
    for row in rows:
        if row['selected_for_review']:
            cv2.polylines(overlay,[np.int32(row['polygon_px'])],True,(100,255,120),2)
            for hole in row['holes_px']:cv2.polylines(overlay,[np.int32(hole)],True,(100,255,120),1)
    edge,_=cv2.findContours(np.uint8(valid),cv2.RETR_LIST,cv2.CHAIN_APPROX_SIMPLE)
    cv2.drawContours(overlay,edge,-1,(100,170,255),2)
    sheet=Image.new('RGB',(1536,700),'#202020')
    sheet.paste(panel(Image.fromarray(raw),tile+' raw',(768,700)),(0,0))
    sheet.paste(panel(Image.fromarray(overlay),f'{len(rows)} source masks / {sum(r["selected_for_review"] for r in rows)} useful review',(768,700)),(768,0))
    sheet.save(DEST/'validation'/(tile+'.jpg'),quality=94)

def publish(checkpoints,excluded,errors,original,fingerprint,options,state,start):
    tiles=[v['tile'] for v in checkpoints.values()]
    selected=[row for v in checkpoints.values() for row in v['rows'] if row['selected_for_review']]
    catalog=_deduplicate_catalog(selected)
    summary={'schema':2,'backend':'staged_component_features_observed_recovery_v2','dataset_id':fingerprint[:20],
      'source_folder':str(CHIP),'source_analysis_folder':str(ANALYSIS),'options':asdict(options),'tiles':tiles,
      'raw_regions':sum(t['proposal_count'] for t in tiles),'candidate_count':len(catalog),
      'selected_observation_count':len(selected),'rejected_count':sum(t['rejected_count'] for t in tiles),
      'thin_screen_count':0,'mean_color_match_count':sum(r['mean_color_reference_evidence'].get('compatible') is True for r in catalog),
      'analysis_state':state,'total_tiles':len(original),'completed_tiles':len(tiles),'excluded_tiles':list(excluded.values()),
      'failed_tiles':list(errors.values()),'original_failures':original,'created_at':time.time(),
      'validation_status':'isolated recovery of normalization failures; optical source features retained',
      'warning':'Not a monolayer classifier. Whole-object color means are descriptive; source features may be debris. No existing acquisition or analysis was modified.'}
    atomic_json(DEST/'analysis.json',summary)
    atomic_json(DEST/'flakes.json',{'flakes':catalog,'dataset_id':fingerprint[:20],'source_folder':str(CHIP),'analysis_schema':2})
    atomic_json(DEST/'observations.json',{'schema':2,'storage':'per_tile_shards','source_folder':str(CHIP),
      'shards':[{'tile_key':t['key'],'file':t['observations_file'],'count':t['proposal_count']} for t in tiles],
      'observation_count':summary['raw_regions'],'physical_unique_count':len(catalog),
      'physical_unique_count_scope':'Existing centroid-pair dedup; extended-object duplicates remain possible.'})
    atomic_json(DEST/'analysis-progress.json',{'state':state,'completed_tiles':len(tiles),'total_tiles':len(original),
      'excluded_tiles':len(excluded),'failed_tiles':len(errors),'elapsed_seconds':time.perf_counter()-start,'updated_at':time.time()})
    return summary

def run(all_failures=False):
    if os.name=='nt':
        ctypes.windll.kernel32.GetCurrentProcess.restype=ctypes.c_void_p
        ctypes.windll.kernel32.SetPriorityClass.argtypes=[ctypes.c_void_p,ctypes.c_ulong]
        if not ctypes.windll.kernel32.SetPriorityClass(ctypes.windll.kernel32.GetCurrentProcess(),0x4000):
            raise OSError('Could not set below-normal CPU priority.')
    DEST.mkdir(exist_ok=True);(DEST/'fields').mkdir(exist_ok=True);(DEST/'validation').mkdir(exist_ok=True)
    snapshot=DEST/'original-failures.json'
    if not snapshot.exists():
        original=read(ANALYSIS/'analysis.json')
        atomic_json(snapshot,{'source_analysis_sha256':sha(ANALYSIS/'analysis.json'),'source_analysis_state':original['analysis_state'],
          'source_completed_tiles':original['completed_tiles'],'source_total_tiles':original['total_tiles'],'failures':original['failed_tiles']})
    original=read(snapshot)['failures'];old_options=read(ANALYSIS/'pipeline.json')['options'];options=StagedFeatureOptions(**old_options)
    files=[Path(__file__),OUT/'observed_normalization_v2.py',OUT/'audit_saved_outputs.py']+[ROOT/'flakefinder'/name for name in
      ('analysis.py','staged_features.py','measure_existing_regions.py','colors.py','imaging.py','thin_contrast.py','scan.py','model.py')]
    identities={str(p.relative_to(ROOT)):sha(p) for p in files}
    config={'method':'observed-footprint-stripe-holdout-recovery-v2','options':asdict(options),'source_code_sha256':identities,
      'original_failures_sha256':sha(snapshot),'source_folder':str(CHIP),'worker_policy':'single process, one native thread, below-normal priority',
      'geometry_policy':'unchanged accepted graph-cut extractor, followed only by observed-domain clipping with disconnected components retained',
      'thin_policy':'mean-color direction reported descriptively; no coherent-terrace/material/thickness classification'}
    fingerprint=hashlib.sha256(json.dumps(config,sort_keys=True).encode()).hexdigest()
    if (DEST/'pipeline.json').exists() and read(DEST/'pipeline.json')['fingerprint']!=fingerprint:
        raise ValueError('Recovery code/options changed; use a new output version rather than overwrite.')
    atomic_json(DEST/'pipeline.json',{**config,'fingerprint':fingerprint})
    all_items={i['key']:i for i in tile_inputs(CHIP)};original_by_key={f['tile_key']:f for f in original}
    keys=list(original_by_key) if all_failures else ['chip001/tile_'+f'{i:06d}' for i in (1,32,180,354,0,7)]
    checkpoints={};excluded={};errors={};start=time.perf_counter()
    for path in (DEST/'fields').glob('*.json'):
        saved=read(path)
        if saved['fingerprint']!=fingerprint:raise ValueError('Checkpoint fingerprint mismatch.')
        key=saved['tile_key']
        if saved['state']=='excluded':excluded[key]=saved
        elif saved['state']=='failed':errors[key]=saved
        else:
            if sha(DEST/saved['tile']['instance_mask'])!=saved['mask_sha256']:raise ValueError('Recovery mask checksum changed.')
            checkpoints[key]=saved
    for key in keys:
        if (DEST/'STOP').exists():break
        if key in checkpoints or key in excluded:continue
        if any(sha(ROOT/path)!=digest for path,digest in identities.items()):raise ValueError('Imported source changed during isolated recovery; stop and version explicitly.')
        item=all_items[key];slug=key.replace('/','__');tick=time.perf_counter()
        atomic_json(DEST/'analysis-progress.json',{'state':'analysing','processing_tile':key,'completed_tiles':len(checkpoints),
          'total_tiles':len(original),'updated_at':time.time(),'pid':os.getpid()})
        # Hash mismatches are fatal and never interpreted as off-chip exclusions.
        provenance,identity=_source_identity(item)
        frame=load_frame(item['path']);item['transform'].validate_frame(frame)
        try:
            prepared=prepare_observed(frame,item,AnalysisOptions(**options.extraction))
        except ValueError as exc:
            message=str(exc)
            expected=('No sufficient measured on-chip interior','Insufficient recorded substrate samples','Insufficient spatial support for held-out normalization validation')
            if not message.startswith(expected):raise
            path=CHIP/'boundary-labels'/(item['tile_id']+'.npz');recorded=np.load(path)['labels']
            saved={'schema':1,'fingerprint':fingerprint,'tile_key':key,'state':'excluded','reason':message,
              'original_failure':original_by_key[key],'source_identity':identity,'provenance':provenance,
              'recorded_label_sha256':sha(path),'recorded_substrate_fraction':float((recorded==1).mean()),
              'exclusion_kind':'off_chip' if not np.any(recorded==1) else 'insufficient_on_chip_interior_or_background_support',
              'elapsed_seconds':time.perf_counter()-tick}
            atomic_json(DEST/'fields'/(slug+'.json'),saved);excluded[key]=saved
            render_validation(frame,np.zeros(frame.rgb.shape[:2],np.uint16),[],np.zeros(frame.rgb.shape[:2],bool),item['tile_id'])
            publish(checkpoints,excluded,errors,original,fingerprint,options,'analysing',start)
            print(key,'EXCLUDED',message,flush=True);continue
        normalization_seconds=time.perf_counter()-tick
        extract=AnalysisOptions(**options.extraction)
        labels=_refine_objects(frame,prepared,extract,return_labels=True)
        labels,clipping=clip_instances(labels,prepared['valid'],np.prod(frame.pixel_size_um),extract.min_detection_area_um2)
        segmentation_seconds=time.perf_counter()-tick-normalization_seconds
        measurements,clear=measure_with_observed_background(frame,prepared,labels,ExistingRegionOptions(**options.measurement))
        focus,focus_limited=focus_provenance(item,frame);rows=[];white=frame.metadata['white_level']
        for label,section in enumerate(find_objects(labels),1):
            if section is None:continue
            row=_geometry(labels,label,section,frame,item['transform']);mask=labels[section]==label
            contrast=np.median(prepared['contrast'][section][mask],axis=0)
            row.update(id='r-'+hashlib.sha256((key+'|'+fingerprint+'|'+str(label)).encode()).hexdigest()[:14],source_image=item['path'].relative_to(CHIP).as_posix(),
              source_tile=item['tile_id'],tile_key=key,tile_id=item['tile_id'],chip_id=item['chip_id'],contrast_rgb=contrast.tolist(),
              source_contrast_rgb=contrast.tolist(),focus_status=focus,focus_limited=focus_limited,
              saturated_fraction=float(np.mean(np.any(frame.rgb[section][mask]>=white,axis=1))))
            rows.append(qualify_mean_color(apply_sieves(row,measurements[label],options,focus_limited),prepared))
        if labels.max()>65535:raise ValueError('Too many instances for lossless uint16 mask.')
        if np.any((labels>0)&~prepared['valid']):raise AssertionError('Source geometry escaped measured domain.')
        if np.any(clear&~prepared['background_samples']):raise AssertionError('Annulus used unverified substrate support.')
        mask_name=slug+'.instances.png';support_name=slug+'.support.npz';map_name=slug+'.contrast-rg.npz'
        cv2.imwrite(str(DEST/mask_name),np.uint16(labels))
        np.savez_compressed(DEST/support_name,valid_domain=prepared['valid'],background_samples=prepared['background_samples'],
          clear_local_substrate=clear,initial_foreground=prepared['foreground'])
        h,w=labels.shape;np.savez_compressed(DEST/map_name,contrast_rg=prepared['contrast'][::4,::4,:2],
          valid_domain=prepared['valid'][::4,::4],original_shape_hw=np.array([h,w]),stride=4)
        report={'normalization':prepared['normalization'],'clipping':clipping,'normalization_seconds':normalization_seconds,
          'segmentation_seconds':segmentation_seconds,'total_seconds':time.perf_counter()-tick,
          'foreground_fraction':prepared['foreground_fraction'],'substrate_mean':prepared['substrate_mean'].tolist(),
          'substrate_covariance':prepared['substrate_covariance'].tolist(),'raw_noise_rgb':prepared['raw_noise_rgb'].tolist(),
          'mask_geometry_unchanged_by_sieves':True,'outside_domain_instances':0,'annulus_outside_recorded_support':0,
          'support_sha256':sha(DEST/support_name),'contrast_map_sha256':sha(DEST/map_name)}
        tile={'key':key,'tile_id':item['tile_id'],'chip_id':item['chip_id'],'source':item['path'].relative_to(CHIP).as_posix(),
          'position':asdict(frame.position),'shape':[h,w],'pixel_size_um':list(frame.pixel_size_um),'sha256':hashlib.sha256(frame.rgb.tobytes()).hexdigest(),
          'acquired_at':frame.acquired_at,'footprint_um':[item['transform'].stage_xy(p,frame).tolist() for p in [[0,0],[w-1,0],[w-1,h-1],[0,h-1]]],
          'focus_status':focus,'focus_limited':focus_limited,'instance_mask':mask_name,'support_masks':support_name,'contrast_rg_file':map_name,
          'observations_file':'fields/'+slug+'.json','proposal_count':len(rows),'selected_count':sum(r['selected_for_review'] for r in rows),
          'rejected_count':sum(not r['selected_for_review'] for r in rows),'stage_counts':{s:sum(r['proposal_stage']==s for r in rows) for s in {r['proposal_stage'] for r in rows}},
          'analysis_seconds':time.perf_counter()-tick}
        saved={'schema':1,'fingerprint':fingerprint,'tile_key':key,'state':'recovered','original_failure':original_by_key[key],
          'source_identity':identity,'provenance':provenance,'mask_sha256':sha(DEST/mask_name),'tile':tile,'rows':rows,'report':report}
        atomic_json(DEST/'fields'/(slug+'.json'),saved);checkpoints[key]=saved;errors.pop(key,None)
        if True:  # Supplementary retries are few; preserve a review pair for each.
            render_validation(frame,labels,rows,prepared['valid'],item['tile_id'])
        publish(checkpoints,excluded,errors,original,fingerprint,options,'analysing',start)
        print(key,len(rows),tile['selected_count'],round(time.perf_counter()-tick,2),'seconds',flush=True)
        del frame,prepared,labels,clear,measurements,rows
    state='complete' if len(checkpoints)+len(excluded)==len(original) and not errors else 'stopped' if (DEST/'STOP').exists() else 'representatives_complete'
    summary=publish(checkpoints,excluded,errors,original,fingerprint,options,state,start)
    print(json.dumps({k:summary[k] for k in ('analysis_state','completed_tiles','raw_regions','candidate_count','mean_color_match_count')}),flush=True)
    return summary

if __name__=='__main__':
    parser=argparse.ArgumentParser();parser.add_argument('--all-failures',action='store_true');args=parser.parse_args()
    run(args.all_failures)
