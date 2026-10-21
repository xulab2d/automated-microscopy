"""Explicit immutable same-placement algorithm upgrade, without a new bootstrap.

The old run and STOP remain untouched. Scientific files are copied to a new
invocation; measured evidence keeps its hashes and original coordinate frame.
"""
from pathlib import Path
from datetime import datetime
import errno,hashlib,json,math,os,re,shutil,time

from .model import SafetyError,Limits,atomic_json
from .vendor_lifecycle import file_hash

KIND='same_run_rectangle_algorithm_upgrade_v1'
SCIENCE_FILES={'batch.json','layout.json','configuration.json','search-plan.json','reference-validation.json'}
SCIENCE_DIRS={'search','discovery','plans','reference','calibration','configuration-revisions'}
OWNER_FILES={'scan-request.json','scan-status.json','worker-start.json','scan-config.json',
             'camera-profile-used.json','reference-validation.json','STOP'}

def read(path,*,check=None):
    return json.loads(_retry_read(lambda:Path(path).read_text(encoding='utf-8-sig'),check=check))

def _checked(root,name):
    path=root/name
    if path.resolve()!=path or not path.is_relative_to(root):raise SafetyError('Handoff evidence is redirected.')
    return path

def _science(name):
    parts=Path(name).parts
    return name in SCIENCE_FILES or parts[0] in SCIENCE_DIRS or bool(re.fullmatch(r'chip\d+',parts[0]))


def _checkpoint(check,deadline=None):
    if check:check()
    if deadline is not None and time.time()>=deadline:raise SafetyError('Original acquisition deadline passed during handoff file processing.')


def _retry_read(action,*,check=None):
    """Retry only transient provider read/open errors, without progress credit."""
    for attempt in range(5):
        _checkpoint(check)
        try:return action()
        except OSError as exc:
            if exc.errno not in (errno.EINVAL,errno.ENOENT) or attempt==4:raise
            remaining=min(2.,.25*2**attempt)
            while remaining>0:
                _checkpoint(check);step=min(.1,remaining);time.sleep(step);remaining-=step
            _checkpoint(check)


def _hash_file(path,*,check=None,progress=None,operation='hash'):
    """A transient read restarts hashing from byte zero; only complete files report progress."""
    def once():
        digest=hashlib.sha256();total=0
        with Path(path).open('rb') as stream:
            while True:
                _checkpoint(check);block=stream.read(4*1024*1024)
                if not block:break
                digest.update(block);total+=len(block);_checkpoint(check)
        return digest.hexdigest(),total
    digest,total=_retry_read(once,check=check)
    if progress:progress({'operation':operation,'file':str(path),'bytes_completed':total,'file_complete':True})
    _checkpoint(check)
    return digest


def _copy_file(source,target,*,check=None,progress=None):
    """Resume a transient source read at its exact byte offset; destination stays exclusive."""
    total=0;reader=None;_checkpoint(check)
    def block_at_offset():
        nonlocal reader
        try:
            if reader is None:
                reader=Path(source).open('rb');reader.seek(total)
            return reader.read(4*1024*1024)
        except OSError:
            if reader is not None:reader.close();reader=None
            raise
    try:
        with Path(target).open('xb') as writer:
            while True:
                block=_retry_read(block_at_offset,check=check)
                if not block:break
                writer.write(block);total+=len(block);_checkpoint(check)
            writer.flush();os.fsync(writer.fileno())
    finally:
        if reader is not None:reader.close()
    _retry_read(lambda:shutil.copystat(source,target),check=check);_checkpoint(check)
    if progress:progress({'operation':'copy','file':str(target),'bytes_completed':total,'file_complete':True})
    _checkpoint(check)


def prepared_focus_retries(donor,batch,*,check=None,progress=None):
    """Explicit upgrade retry only for the diagnosed prepared-plane/ROI refusal."""
    retries=[]
    for key,chip in batch.get('chip_states',{}).items():
        _checkpoint(check)
        if chip.get('state')!='focus_unresolved':continue
        path=donor/key/'manifest.json';plan_path=donor/'plans'/f'{key}.json'
        if not path.exists() or not plan_path.exists():continue
        manifest=read(path);plan=read(plan_path);points=manifest.get('focus_points_um',[])
        if (manifest.get('anchors_prepared') is not True or len(points)<4
                or manifest.get('state')!='failed' or manifest.get('finalization_errors')
                or manifest.get('error')!=chip.get('error')
                or not re.fullmatch(r'No substrate-supported focus ROI or bounded local focus point for tile_\d+; nearest is [\d.]+ um away\.',chip.get('error',''))
                or plan.get('focus_anchor_mode')!='chip_edges'
                or manifest.get('coverage_plan_fingerprint')!=plan.get('fingerprint')):continue
        if any(len(point)!=3 or not all(type(v) in (int,float) and math.isfinite(v) for v in point) for point in points):continue
        source=_hash_file(Path(__file__).with_name('full_chip.py'),check=check,progress=progress)
        if source==manifest.get('algorithm_source_sha256'):continue
        retries.append({'chip_id':key,'manifest_sha256':_hash_file(path,check=check,progress=progress),'plan_sha256':_hash_file(plan_path,check=check,progress=progress),
            'prepared_focus_points_retained':len(points),'completed_tiles_retained':len(manifest.get('completed',{})),
            'seed_focus_provenance_unchanged':True,'previous_algorithm_source_sha256':manifest.get('algorithm_source_sha256'),
            'upgrade_algorithm_source_sha256':source,
            'reason':'Explicit new algorithm invocation repairs the prepared edge-plane extrapolation refusal; reuse existing anchors and tiles.'})
    return retries

def build_contract(project,prior_run_id,*,process_checker=None,check=None,progress=None,reference_completed=False):
    """Only a closed, absent owner can be pinned for the explicitly authorized upgrade."""
    from .scan_continuation import process_alive_strict
    if type(reference_completed) is not bool:raise ValueError('Completed-chip reference mode must be an explicit boolean.')
    project=Path(project).resolve();owner_alive=process_checker or process_alive_strict
    deadline=None
    def checkpoint():_checkpoint(check,deadline)
    checkpoint()
    if not re.fullmatch(r'scan-\d{8}-\d{6}-[a-f0-9]{8}',prior_run_id):raise SafetyError('Invalid handoff donor.')
    donor=_checked(project,'data/'+prior_run_id)
    status=read(donor/'scan-status.json',check=checkpoint);worker=read(donor/'worker-start.json',check=checkpoint);request=read(donor/'scan-request.json',check=checkpoint)
    if (status.get('hardware_closed') is not True or status.get('state') not in ('stopped','failed','search_sampling_complete','chip_budget_reached','time_budget_reached')
            or status.get('id')!=prior_run_id or worker.get('id')!=prior_run_id
            or status.get('pid')!=worker.get('pid') or owner_alive(worker.get('pid'))
            or status.get('request_sha256')!=_hash_file(donor/'scan-request.json',check=checkpoint,progress=progress)
            or worker.get('request_sha256')!=status.get('request_sha256')):
        raise SafetyError('Handoff requires the exact closed and absent acquisition owner.')
    batch=read(donor/'batch.json',check=checkpoint);config=read(donor/'scan-config.json',check=checkpoint);validation=read(donor/'reference-validation.json',check=checkpoint)
    if not batch.get('coordinate_frame_id') or validation.get('accepted') is not True:
        raise SafetyError('Handoff requires the original accepted reference and coordinate frame.')
    reference=Path(batch['reference_path'])
    inherited_reference=request.get('rectangle_handoff',{}).get('reference_path')==str(reference)
    if (reference.resolve()!=reference or not reference.is_relative_to(project/'data')
            or not (reference.is_relative_to(donor) or inherited_reference) or _hash_file(reference,check=checkpoint,progress=progress)!=batch['reference_sha256']
            or _hash_file(reference,check=checkpoint,progress=progress)!=validation['raw_sha256']):raise SafetyError('Handoff reference changed.')
    started=datetime.fromisoformat(worker['started_at'].replace('Z','+00:00')).timestamp()
    deadline=request.get('rectangle_handoff',request.get('continuation',{})).get('deadline_at',started+request['settings']['hours']*3600)
    if not math.isfinite(deadline) or deadline<=time.time():raise SafetyError('Original acquisition deadline has passed.')
    completed={}
    if reference_completed:
        from .completed_artifacts import build_completed_chip_sources
        completed=build_completed_chip_sources(donor,batch,check=checkpoint)
    def inventory():
        names=[]
        for child in donor.iterdir():
            checkpoint()
            if child.name in completed:continue
            paths=child.rglob('*') if child.is_dir() else (child,)
            for path in paths:
                checkpoint()
                if path.is_file():
                    name=path.relative_to(donor).as_posix()
                    if _science(name) or path.name in OWNER_FILES and path.parent==donor:names.append(name)
        return names
    names=_retry_read(inventory,check=checkpoint)
    names=sorted(set(names))
    hashes={name:_hash_file(_checked(donor,name),check=checkpoint,progress=progress) for name in names}
    result={'schema':1,'kind':KIND,'prior_run_id':prior_run_id,'same_placement_authorized':True,
        'prior_files_sha256':hashes,'coordinate_frame_id':batch['coordinate_frame_id'],
        'search_bounds_um':batch['search_bounds_um'],'limits':config['limits'],'deadline_at':deadline,'reference_path':str(reference),
        'settings':request['settings'],'source_release':request['source_release'],
        'prepared_focus_retry_chips':prepared_focus_retries(donor,batch,check=checkpoint,progress=progress),
        'policy':'Copy original scientific evidence; resume saved candidates before pending lattice. No bootstrap or old-data overwrite.',
        'supplementary_candidate_budget':{
            'method':'remaining_rectangle_ray_and_quick_focus_work_v1',
            'geometry':'Remaining bounded rays, measured brackets, centering/lateral evidence and one gap ray; actual camera field determines refinement.',
            'focus':'At most one total quick-focus attempt for a measured broad transition; historical attempts count as consumed.',
            'seconds':'Remaining original absolute user deadline',
            'prior_budget_policy':'Preserve previous records and consumed capture counts; new derived allowance is recorded separately.'}}
    if reference_completed:
        result.update(reference_completed_chips=True,completed_chip_sources=completed,
            policy='Completed chips remain immutable at their hash-pinned original paths; copy pending discovery only. No bootstrap or old-data overwrite.')
    checkpoint()
    return result

def verify_contract(project,contract,*,process_checker=None,check=None,progress=None):
    if not isinstance(contract,dict) or contract!=build_contract(project,contract.get('prior_run_id',''),process_checker=process_checker,check=check,progress=progress,
            reference_completed=contract.get('reference_completed_chips',False)):
        raise SafetyError('Pinned rectangle handoff changed.')
    return contract

def source_config(project,contract,*,check=None,progress=None):
    verify_contract(project,contract,check=check,progress=progress)
    donor=Path(project)/'data'/contract['prior_run_id']
    return read(donor/'scan-config.json',check=check),read(donor/'camera-profile-used.json',check=check)

def prepare_run(project,out,contract,*,check=None,progress=None):
    """Copy committed data only; invocation STOP/control files stay at their original paths."""
    def checkpoint():_checkpoint(check,contract['deadline_at'])
    verify_contract(project,contract,check=checkpoint,progress=progress);out=Path(out).resolve();donor=Path(project).resolve()/'data'/contract['prior_run_id']
    if out==donor or out.parent!=donor.parent:raise SafetyError('Handoff needs a separate managed invocation.')
    if (out/'batch.json').exists():raise SafetyError('Handoff destination already contains campaign state.')
    copied=[];preserved_stops=[]
    for name,digest in contract['prior_files_sha256'].items():
        checkpoint()
        if not _science(name):continue
        if Path(name).name=='STOP':preserved_stops.append(name);continue
        source=_checked(donor,name);target=_checked(out,name)
        if target.exists():raise SafetyError('Handoff would replace existing evidence: '+name)
        target.parent.mkdir(parents=True,exist_ok=True);_copy_file(source,target,check=checkpoint,progress=progress)
        if _hash_file(target,check=checkpoint,progress=progress,operation='verify_copy')!=digest:raise SafetyError('Handoff copy hash differs: '+name)
        copied.append(name)
    checkpoint();batch=read(out/'batch.json',check=checkpoint)
    if contract.get('reference_completed_chips'):
        batch['completed_chip_sources']=contract['completed_chip_sources']
    # Do not rewrite raw frame metadata, chip plans, focus results or their fingerprints.
    for attempt in batch.get('boundary_attempts',[]):
        folder=Path(attempt['folder'])
        if folder.is_relative_to(donor):attempt['folder']=str(out/folder.relative_to(donor))
        if not attempt.get('chip_id'):
            attempt['pre_upgrade_state']=attempt['state'];attempt['state']='discovering'
            attempt['rectangle_reanalysis']=True
    for chip in batch.get('chip_states',{}).values():
        folder=Path(chip.get('discovery_folder',donor))
        if folder.is_relative_to(donor):chip['discovery_folder']=str(out/folder.relative_to(donor))
    queue=[item['chip_id'] for item in contract.get('prepared_focus_retry_chips',[])]
    if batch.get('current_chip') and batch['current_chip'] not in queue:queue.append(batch['current_chip'])
    if queue:
        batch['handoff_resume_queue']=queue
        batch['current_chip']=queue[0]
    batch.update(state='stopped',phase='prepared_rectangle_handoff',rectangle_handoff={
        'kind':KIND,'prior_run_id':donor.name,'deadline_at':contract['deadline_at'],
        'prior_batch_sha256':contract['prior_files_sha256']['batch.json']})
    atomic_json(out/'batch.json',batch)
    proof={'schema':1,'kind':KIND,'contract':contract,'copied_files':copied,
           'prior_stop_preserved':True,'copied_stop_controls_omitted':preserved_stops,
           'original_scientific_files_modified':False,'fresh_bootstrap':False,
           'completed_chip_sources':contract.get('completed_chip_sources',{})}
    atomic_json(out/'rectangle-handoff-proof.json',proof)
    return batch

def verify_live(scope,profile,config,prior_profile,contract):
    """Under the new worker lease, validate actual pose and unchanged live optics/ranges."""
    if json.dumps(profile,sort_keys=True)!=json.dumps(prior_profile,sort_keys=True):
        raise SafetyError('Installed measured camera profile changed before handoff.')
    scope.check_cancel();position=scope.position();Limits(**config['limits']).check(position)
    controls=scope.hardware_controls();xy=controls.stage_travel_range();z=controls.focus_travel_range()
    limits=config['limits']
    values=[xy[k] for k in ('x_min_um','x_max_um','y_min_um','y_max_um')]+[z['min_um'],z['max_um']]
    if not all(type(v) in (int,float) and math.isfinite(v) for v in values):raise SafetyError('Invalid live controller travel.')
    if (limits['x_min']<values[0] or limits['x_max']>values[1] or limits['y_min']<values[2] or limits['y_max']>values[3]
            or limits['z_min']<values[4] or limits['z_max']>values[5]):raise SafetyError('Original envelope exceeds live controller limits.')
    if any(abs(xy.get(axis+'_position_um',float('inf'))-getattr(position,axis))>2 for axis in 'xy'):
        raise SafetyError('Live controller and adapter XY coordinate frames disagree.')
    optics=controls.objectives();selected=next((o for o in optics['objectives'] if o['position']==optics['position']),{})
    if optics['position']!=config['zen']['expected_objective_position'] or selected.get('magnification')!=20:
        raise SafetyError('Handoff requires the same manually engaged 20x objective.')
    return {'position_um':{a:getattr(position,a) for a in 'xyz'},'stage_range':xy,'focus_range':z,
            'position_policy':'Actual leased readback; last saved image is not asserted to be the final physical pose.'}

def run_handoff(scope,project,out,contract,config,profile,prior_profile,check,update,*,campaign_runner=None,file_progress=None):
    from .config import arm
    from .chip_campaign import CampaignOptions,run_chip_campaign,validate_campaign_envelope
    runner=campaign_runner or run_chip_campaign
    proof=verify_live(scope,profile,config,prior_profile,contract);validate_campaign_envelope(config,contract['search_bounds_um'])
    arm(scope,config);check()
    lamp=scope.hardware_controls().set_lamp_percent(config['context']['illumination_percent'])
    if not math.isfinite(lamp) or abs(lamp-config['context']['illumination_percent'])>1:raise SafetyError('Handoff lamp differs from the saved reference.')
    scope.set_exposure_ms(config['context']['exposure_ms']);check()
    def report_file(info):
        check()
        if file_progress:file_progress(info)
        else:update('resuming_saved_rectangle_evidence',file_progress=info)
    batch=prepare_run(project,out,contract,check=check,progress=report_file);atomic_json(Path(out)/'rectangle-live-verification.json',proof)
    atomic_json(Path(out)/'scan-config.json',config)
    def progress(phase,frame,info):check();update(phase,progress={k:v for k,v in info.items() if k in ('index','total','state','reason','anchor_index','z_um')})
    common=dict(config=config,reference_tiff=batch['reference_path'],output=out,search_bounds_um=batch['search_bounds_um'],
        options=CampaignOptions(**batch['options']),full_chip_options=batch['full_chip_options'],scope=scope,callback=progress,allow_source_change=True)
    while batch.get('current_chip'):
        result=runner(**common,resume=True,rectangle_mode=True)
        if result.get('current_chip') or result.get('state')!='chip_complete_discovery_pending':return result
        queue=batch.get('handoff_resume_queue',[])
        queue=queue[1:] if queue else []
        if queue:
            batch=read(Path(out)/'batch.json')
            batch.update(current_chip=queue[0],handoff_resume_queue=queue,state='stopped')
            atomic_json(Path(out)/'batch.json',batch)
        else:
            if (Path(out)/'batch.json').exists():
                batch=read(Path(out)/'batch.json');batch['handoff_resume_queue']=[]
                atomic_json(Path(out)/'batch.json',batch)
            break
    check();remaining=contract['deadline_at']-time.time()
    if remaining<=0:raise SafetyError('Original acquisition deadline passed before campaign handoff.')
    return runner(**common,continue_discovery=True,continuation_walltime_s=remaining,rectangle_mode=True)
