"""Auditable restart planning from immutable, same-coordinate campaign evidence.

No connections or hardware calls occur during planning. Imported outlines are
derived records in a new directory, never modifications to a saved discovery.
The runner accepts an already owned/armed adapter and does no background work.
"""
from dataclasses import asdict
from pathlib import Path
import hashlib,json,time,uuid
import cv2,numpy as np

from .chip_discovery import file_hash
from .full_chip import build_full_chip_plan,FullChipOptions,run_full_chip
from .imaging import load_frame
from .model import SafetyError,Cancelled,atomic_json,CameraTransform
from .layout import SubstrateReference


def read(path):return json.loads(Path(path).read_text(encoding='utf-8'))
def digest(value):return hashlib.sha256(json.dumps(value,sort_keys=True).encode()).hexdigest()
def checked_path(root,relative):
    path=(Path(root)/relative).resolve()
    if not path.is_relative_to(Path(root).resolve()):raise SafetyError('Evidence path escapes its source campaign.')
    return path


def relocated_reference(campaign,state):
    """Resolve the recorded reference inside the relocated campaign only."""
    old=str(state['reference_path']).replace('\\','/')
    marker='/'+Path(campaign).name+'/'
    if marker not in old:raise SafetyError('Reference is not inside the recorded source campaign; supply a reviewed mapping.')
    path=checked_path(campaign,old.split(marker,1)[1])
    if file_hash(path)!=state['reference_sha256']:raise SafetyError('Relocated reference differs from recorded raw hash.')
    return path


def import_validated_outline(campaign,attempt,replay_path,output,reference,transform,options,chip_id):
    """Materialize a derived view of an existing reviewed v2 replay, no reimaging.

    Every referenced raw TIFF, metadata and label file is verified against its
    original hash before reuse. Original discovery identity is retained verbatim
    in lineage; the derived object cannot resume sparse acquisition.
    """
    campaign=Path(campaign).resolve();output=Path(output).resolve();replay=read(replay_path)
    source=checked_path(campaign,'discovery/'+attempt);old=read(source/'discovery.json')
    if file_hash(source/'discovery.json')!=replay['discovery_sha256'].get(attempt):raise SafetyError('Reviewed discovery identity changed.')
    fit=replay.get('fits',{}).get(attempt)
    if not fit or fit.get('status')!='estimated' or not fit.get('registered_consistency',{}).get('accepted'):
        raise SafetyError('Only independently validated complete half-plane models can be imported.')
    if old['transform']!=json.loads(json.dumps(asdict(transform))) or old['reference']['context']!=SubstrateReference.from_frame(reference).context:
        raise SafetyError('Imported outline has a different transform/optical profile.')
    if output.exists() and any(output.iterdir()):raise SafetyError('Derived evidence output must be empty.')
    accepted={row['image']:row for row in replay['rows']if row['attempt']==attempt and row.get('edge')}
    records=[]
    for original in old['records']:
        record=json.loads(json.dumps(original));hashes={}
        for relative,expected in original['hashes'].items():
            path=checked_path(source,relative)
            if file_hash(path)!=expected:raise SafetyError('Imported raw/label evidence changed: '+str(path))
            hashes[str(path)]=expected
        record['image']=str(checked_path(source,original['image']))
        record['labels_file']=str(checked_path(source,original['labels_file']))
        record['hashes']=hashes;record['original_edge']=record.pop('edge',None)
        row=accepted.get(record['name']);record['edge']=None
        if row:
            record['edge']={**row['edge'],'image':record['image']}
            record['edge_validation']=row['validation']
        record['source_record_name']=original['name'];records.append(record)
    for line in fit['lines']:
        if any(not any(row['edge'] and row['source_record_name']+'.tiff'==Path(image).name for row in records)for image in line['source_images']):
            raise SafetyError('Model line refers to an unvalidated source image.')
    lineage={'source_discovery':str(source/'discovery.json'),'source_discovery_sha256':file_hash(source/'discovery.json'),
        'source_fingerprint':old['fingerprint'],'replay':str(Path(replay_path).resolve()),'replay_sha256':file_hash(replay_path),
        'source_algorithm_hashes':replay['staged_sources'],'original_evidence_unchanged':True}
    derived={key:old[key]for key in ('seed_xy_um','options','context','transform','source_shape','reference','reference_rgb','reference_pixels_sha256','limits')}
    derived.update(schema=2,algorithm='immutable_saved_evidence_import_v2',state='estimated',lineage=lineage,
        records=records,support_records=[],estimated_outline=fit,all_chips_found=False,acquisition_resume_allowed=False)
    derived['fingerprint']=digest(derived)
    output.mkdir(parents=True,exist_ok=True)
    atomic_json(output/'discovery.json',derived)
    # Preserve the original registered tri-state raster; no pixels/labels change.
    (output/'evidence.npz').write_bytes((source/'evidence.npz').read_bytes())
    atomic_json(output/'estimated-outline.json',fit)
    plan=build_full_chip_plan(output,reference,transform,options,chip_id,estimated_outline=fit)
    return plan,lineage


def same_chip_models(a,b,min_iou=.8):
    """Deduplicate well-overlapping convex models, not nearby seed points."""
    aa=np.asarray(a,np.float32);bb=np.asarray(b,np.float32)
    intersection,_=cv2.intersectConvexConvex(aa,bb)
    union=cv2.contourArea(aa)+cv2.contourArea(bb)-intersection
    return union>0 and intersection/union>=min_iou


def create_restart_plan(campaign,output,replay_path,*,expected_coordinate_frame_id,attempts=('candidate010','candidate015')):
    campaign=Path(campaign).resolve();out=Path(output).resolve();state=read(campaign/'batch.json')
    if state['coordinate_frame_id']!=expected_coordinate_frame_id:raise SafetyError('Source coordinate frame differs; old XY must not be reused.')
    if state.get('current_chip') or state.get('state') not in ('failed','stopped','storage_budget_reached','time_budget_reached','search_sampling_complete','chip_budget_reached','chip_complete_discovery_pending'):
        raise SafetyError('Restart planning requires a terminal source with no active chip.')
    if out.exists() and any(out.iterdir()):raise SafetyError('Use a new empty restart lineage directory.')
    reference_path=relocated_reference(campaign,state);reference=load_frame(reference_path)
    config_path=checked_path(campaign,state.get('configuration_file','configuration.json'));config=read(config_path)
    transform=CameraTransform(**config['camera_transform']);transform.validate_frame(reference)
    from .chip_campaign import restore_campaign_coverage
    # Replay registered labels/sidecars before reusing any completed-chip
    # identity. This does not decode1386raw images or rely on a bounding box alone.
    _,_,_,coverage_audit=restore_campaign_coverage(campaign,state,transform)
    options=FullChipOptions(**state['full_chip_options']);replay=read(replay_path)
    parent={'campaign':str(campaign),'batch_sha256':file_hash(campaign/'batch.json'),'layout_sha256':file_hash(campaign/'layout.json'),
        'coordinate_frame_id':state['coordinate_frame_id'],'state':state['state'],'error':state.get('error'),
        'configuration_sha256':file_hash(config_path),'source_reference_sha256':state['reference_sha256']}
    tasks=[];completed=[]
    for key,chip in state['chip_states'].items():
        manifest=read(campaign/key/'manifest.json');plan=read(campaign/'plans'/(key+'.json'))
        if not manifest.get('planned_area_acquired') or len(manifest['completed'])!=plan['tile_count']:
            raise SafetyError('An incomplete source chip needs a dedicated resume plan: '+key)
        unsigned={k:v for k,v in plan.items()if k!='fingerprint'}
        if digest(unsigned)!=plan['fingerprint'] or manifest['coverage_plan_fingerprint']!=plan['fingerprint']:
            raise SafetyError('Saved full-chip plan/manifest identity differs: '+key)
        item={'chip_id':key,'source_folder':str(campaign/key),'manifest_sha256':file_hash(campaign/key/'manifest.json'),
            'plan_sha256':file_hash(campaign/'plans'/(key+'.json')),'polygon_um':plan['scan_plan']['polygon_um'],
            'completed_tiles':len(manifest['completed']),'chip_extent_validated':manifest.get('chip_extent_validated',False),
            'policy':'Do not rediscover this already scanned chip interior. Extent gaps remain separate explicit tasks; no new completeness claim.'}
        completed.append(item)
        if not item['chip_extent_validated']:
            tasks.append({'id':'repair-'+key,'kind':'perimeter_validation_required','source_chip':item,
                'status':'requires_bounded_perimeter_plan','reason':manifest.get('boundary_validation',{}).get('acquired_raster_evidence',{}).get('reason'),
                'coverage_complete':False})
    imported=[]
    for attempt in attempts:
        fit=replay.get('fits',{}).get(attempt)
        if not fit or fit.get('status')!='estimated':continue
        duplicate=next((p for p in completed+imported if same_chip_models(fit['polygon_um'],p['polygon_um'])),None)
        if duplicate:
            tasks.append({'id':'alias-'+attempt,'kind':'duplicate_saved_geometry','source_attempt':attempt,'same_chip':duplicate['chip_id'],'status':'no_acquisition'})
            continue
        key='remaining-'+attempt
        plan,lineage=import_validated_outline(campaign,attempt,replay_path,out/'derived'/attempt,reference,transform,options,key)
        atomic_json(out/'plans'/(key+'.json'),plan)
        item={'chip_id':key,'polygon_um':fit['polygon_um']};imported.append(item)
        tasks.append({'id':key,'kind':'scan_imported_outline','plan':'plans/'+key+'.json','plan_fingerprint':plan['fingerprint'],
            'status':'ready_after_coordinate_and_storage_preflight','lineage':lineage,'tile_count':plan['tile_count'],
            'coverage_complete':False,'discovery_capture_count_required':0})
    search=read(campaign/'search-plan.json');seen={r['index']for r in state['seed_records']if r.get('index') is not None}
    pending=sorted(set(range(search['probe_count']))-seen)
    if set(pending)!=set(state.get('remaining_lattice_indices',pending)):raise SafetyError('Source search indices disagree.')
    result={'schema':1,'kind':'same_coordinate_frame_restart','restart_id':str(uuid.uuid4()),'created_at':time.time(),'parent':parent,
        'coordinate_frame_id':state['coordinate_frame_id'],'reference_path':str(reference_path),'reference_sha256':file_hash(reference_path),
        'transform':asdict(transform),'optical_context':SubstrateReference.from_frame(reference).context,
        'search_bounds_um':state['search_bounds_um'],'completed_chip_exclusions':completed,'tasks':tasks,
        'registered_coverage_replay':coverage_audit,
        'remaining_search':{'indices':pending,'points_um':[search['probe_centres_um'][i]for i in pending],
            'previous_observations':len(seen),'uncertain_prior_observations_remain_unknown':True,
            'repeated_empty_search_frames_required':0,'source_search_plan_sha256':file_hash(campaign/'search-plan.json')},
        'unresolved_discoveries':[a for a in state['boundary_attempts']if a['state']=='unresolved' and a['id'] not in attempts],
        'execution_policy':{'caller_owns_adapter':True,'coordinate_frame_must_be_reconfirmed':True,'no_automatic_restart':True,
            'placement_status':'historical_unverified',
            'placement_note':'Sample removal/replacement invalidates these XY positions. Fresh discovery/registration is required; an old frame ID alone is not proof.',
            'fresh_storage_preflight_required':True,'task_order':'Verified saved outline first; explicit perimeter repair; unresolved geometry; only then remaining search.',
            'limits_or_budget_extension_implied':False},'all_chips_found':False}
    result['fingerprint']=digest(result);atomic_json(out/'restart-plan.json',result);return result


def run_imported_task(scope,restart_folder,task_id,*,config,confirmed_coordinate_frame_id,context=None,callback=None,check_cancel=None):
    """Execute ONE reviewed direct scan with an owned adapter; never reconnect.

    Caller explicitly reconfirms unchanged stage origin/sample and performs fresh
    storage preflight. Existing full-chip acquisition enforces optical profile,
    XYZ limits, all autofocus/readback checks, durable raw saves and STOP.
    """
    out=Path(restart_folder).resolve();restart=read(out/'restart-plan.json')
    if digest({k:v for k,v in restart.items()if k!='fingerprint'})!=restart['fingerprint']:raise SafetyError('Restart plan identity changed.')
    if restart.get('execution_policy',{}).get('placement_status')!='verified_current':
        raise SafetyError('Historical placement is not verified for execution. Acquire fresh placement/discovery evidence; an old coordinate-frame ID is insufficient.')
    if (out/'PLACEMENT_INVALIDATED.json').exists():raise SafetyError('Prepared XY queue was invalidated by sample removal/replacement.')
    if confirmed_coordinate_frame_id!=restart['coordinate_frame_id']:raise SafetyError('Stage coordinate continuity was not confirmed.')
    if not getattr(scope,'armed',False):raise SafetyError('Caller must own and arm the adapter before task execution.')
    source=Path(restart['parent']['campaign'])
    if file_hash(source/'batch.json')!=restart['parent']['batch_sha256']:raise SafetyError('Source campaign changed after restart planning.')
    task=next((item for item in restart['tasks']if item['id']==task_id and item['kind']=='scan_imported_outline'),None)
    if task is None:raise ValueError('Only reviewed imported-outline tasks can run through this entry point.')
    if file_hash(restart['reference_path'])!=restart['reference_sha256']:raise SafetyError('Restart reference changed.')
    plan=read(checked_path(out,task['plan']))
    if plan['fingerprint']!=task['plan_fingerprint']:raise SafetyError('Restart task geometry changed.')
    original=scope.check_cancel
    def check():
        original()
        if check_cancel:check_cancel()
        if (out/'STOP').exists() or (source/'STOP').exists():raise Cancelled('Restart/source STOP requested; neither file is cleared.')
    check();scope.check_cancel=check
    try:
        # Deliberately require existing storage estimator before even focus motion.
        from .chip_campaign import estimate_campaign_storage
        reference=load_frame(restart['reference_path'])
        if config.get('camera_transform')!=restart['transform']:raise SafetyError('Execution transform differs from the saved coordinate frame.')
        storage=estimate_campaign_storage(config,plan,int(reference.rgb.nbytes),out/task_id)
        atomic_json(out/'preflight'/(task_id+'-storage.json'),storage)
        if not storage['sufficient']:raise SafetyError('Restart task storage allowance is insufficient.')
        return run_full_chip(scope,plan,reference,out/task_id,context=context or config.get('context'),callback=callback,check_cancel=check)
    finally:scope.check_cancel=original
