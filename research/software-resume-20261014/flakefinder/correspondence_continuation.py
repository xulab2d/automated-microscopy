"""One closed refocus failure; two new stationary empirical confirmations."""
from pathlib import Path
from datetime import datetime
from dataclasses import asdict
import copy,hashlib,json,math,re
import numpy as np
from .model import SafetyError,AcquisitionError,atomic_json
from .scan_continuation import read,digest
from .reference_focus_recovery import ERROR,MAX_SECONDS,MAX_CAPTURES,validate_raw_receipt

KIND='diagnosed_fixed_pixel_reference_correspondence_v1'
PARENT='scan-20261011-151236-afdc042d'
PARENT_REQUEST_SHA='2ded18d1fd20a0496a3bbf0e800ea78a9914a0a5e3a87afe30e728ed99c0aa18'

def recognized(folder,status):
    from .reference_focus_recovery import KIND as PRIOR_KIND
    if (folder.name!=PARENT or status.get('state')!='failed' or status.get('hardware_closed') is not True
            or status.get('error_type')!='AcquisitionError' or status.get('error')!=ERROR
            or status.get('failed_phase')!='reference_refocus' or status.get('stop_errors')!=[]
            or status.get('watchdog_reason') is not None):return False
    if any((folder/n).exists() for n in ('STOP','watchdog.json','scan-config.json','batch.json','reference-validation.json','progress-reused.json')):return False
    try:
        request=read(folder/'scan-request.json');worker=read(folder/'worker-start.json');failure=read(folder/'recovery-needed.json')
        verified=read(folder/'continuation-verification.json');quality=read(folder/'reference-correction-verification.json')
        return (digest(folder/'scan-request.json')==PARENT_REQUEST_SHA and request['continuation'].get('continuation_kind')==PRIOR_KIND
            and verified.get('continuation')==request['continuation'] and verified['checkpoint'].get('verified_before_arm') is True
            and verified['checkpoint'].get('stationary_readbacks_required')==5 and len(verified['checkpoint'].get('stationary_readbacks_um',[]))==5
            and request.get('source_release')==status.get('source_release')==worker.get('source_release')==failure.get('source_release')
            and worker.get('worker_sha256')==request['source_release']['files']['scan_worker.py']
            and failure.get('request_sha256')==PARENT_REQUEST_SHA and failure.get('run_id')==folder.name
            and failure.get('hardware_closed') is True and failure.get('error')==ERROR and failure.get('failed_phase')=='reference_refocus'
            and quality.get('accepted') is False and len(quality['controls'])==4 and all(c.get('accepted') is True for c in quality['controls'])
            and quality['focus'].get('accepted') is False and quality['focus'].get('minimum_score_fraction')==.95)
    except (OSError,KeyError,ValueError,TypeError):return False

def prior_measurement(folder,donor):
    """Admit resolved Z only; never promote the old measurement_only proof."""
    from .imaging import load_frame
    from .reference_repair import validate_plan,check_controls,PLAN
    plan,_=validate_plan(donor);proof=read(folder/'reference-refocus-proof.json')
    result=read(folder/'initial-edge-focus/focus-result.json');recovery=read(folder/'initial-edge-focus/recovery.json')
    final=read(folder/'reference-refocus-budget.json');embedded=proof.get('budget',{})
    if (proof.get('acceptance_state')!='measurement_only' or proof.get('original_position_um')!=plan['position_um']
            or proof.get('independent_confirmation_required') is not True or result.get('success') is not True
            or result.get('outcome')!='resolved_peak' or recovery.get('state')!='focused'
            or recovery.get('result',{}).get('success') is not True
            or proof.get('result_sha256')!=digest(folder/'initial-edge-focus/focus-result.json')
            or proof.get('recovery_sha256')!=digest(folder/'initial-edge-focus/recovery.json')):
        raise SafetyError('Prior refocus has no immutable independently resolved measurement.')
    for b in (embedded,final):
        if (b.get('max_seconds')!=600. or b.get('max_captures')!=64 or b.get('captures_started')!=14
                or not math.isfinite(b['elapsed_seconds']) or not 0<=b['elapsed_seconds']<600.):raise SafetyError('Prior refocus budget is invalid.')
    if (final['elapsed_seconds']<embedded['elapsed_seconds'] or
            hashlib.sha256(json.dumps(embedded,indent=2,allow_nan=False).replace('\n','\r\n').encode()).hexdigest()!=proof.get('budget_sha256')):
        raise SafetyError('Prior provisional/final budget records disagree.')
    exposure=read(folder/'exposure-adjustment.json')
    if exposure.get('enabled') is not False or len(exposure.get('attempts',[]))!=1:raise SafetyError('Prior initial exposure was not one fixed capture.')
    attempt=exposure['attempts'][0];captures=[];external={}
    pairs=[(folder/'calibration/reference-attempt-01.tiff',attempt['vendor_receipt']),
           (folder/'initial-edge-focus/focused-full-field.tiff',proof['vendor_receipt'])]
    for raw,ack in pairs:
        frame=load_frame(raw);validate_raw_receipt(frame,raw,ack);captures.append(frame.metadata['zen_vendor_result'])
    if (proof.get('full_frame_sha256')!=digest(pairs[1][0]) or proof.get('full_metadata_sha256')!=digest(pairs[1][0].with_suffix('.json'))
            or attempt.get('raw_path')!=str(pairs[0][0]) or attempt.get('raw_sha256')!=digest(pairs[0][0])
            or attempt.get('metadata_sha256')!=digest(pairs[0][0].with_suffix('.json'))):raise SafetyError('Prior full raw changed.')
    measured=asdict(frame.position)
    if (measured!=proof['measured_position_um'] or abs(measured['z']-result['z_um'])>.25
            or abs(measured['z']-recovery['confirmation']['z_um'])>.25
            or any(abs(measured[a]-plan['position_um'][a])>.5 for a in 'xy')):raise SafetyError('Prior resolved pose/confirmation changed.')
    plan=copy.deepcopy(plan);plan['position_um']['z']=result['z_um']
    quality=check_controls(frame,donor/Path(PLAN).parent,plan)
    quality['measured_focus_proof_sha256']=digest(folder/'reference-refocus-proof.json')
    if quality!=read(folder/'reference-correction-verification.json'):raise SafetyError('Prior fixed-pixel refusal no longer recomputes.')
    observation_records=[read(p) for p in sorted((folder/'observations').glob('*.json')) if not p.name.endswith('.vendor.json')]
    for archive in sorted((folder/'observations').glob('*.vendor.json')):
        saved=read(archive);path=Path(saved['evidence_path']);observation=read(path);capture=observation['capture']
        if (path.parent!=folder/'observations' or saved.get('status')!='retired_after_persisted_observation'
                or saved.get('capture')!=capture or saved.get('evidence_sha256')!=digest(path)
                or capture.get('job_completed') is not True or capture.get('job_unloaded') is not True
                or observation.get('metadata',{}).get('zen_vendor_result')!=capture
                or not re.fullmatch('[0-9a-f]{32}',str(capture.get('capture_id','')))
                or not re.fullmatch('[0-9a-f]{32}',str(capture.get('adapter_session_id','')))
                or Path(capture['result_folder'])!=folder.parent/'zen-captures'/capture['capture_id']
                or Path(capture['result_folder']).exists()):raise SafetyError('Prior focus observation provenance changed.')
        for ref in [observation['preview'],*observation.get('selected_raw_preserved',[]),*saved.get('vendor_previews_preserved',[])]:
            p=Path(ref['path']).resolve(strict=True)
            actual_sha=digest(p);actual_bytes=p.stat().st_size
            matches=actual_bytes==ref['bytes'] and actual_sha==ref['sha256']
            if not matches and p.parent==folder/'initial-edge-focus' and p.name in ('best_observed.tiff','best_observed.json'):
                # This existing AF output is an evolving best snapshot. Its
                # supersession must be proved by a later durable observation;
                # every observation/JPG/archive is still verified below.
                current_capture=read(p.with_suffix('.json'))['metadata']['zen_vendor_result']
                matches=any(later.get('acquired_at',0)>observation['acquired_at'] and later.get('capture')==current_capture
                    and any(Path(r['path'])==p and r['bytes']==actual_bytes and r['sha256']==actual_sha
                            for r in later.get('selected_raw_preserved',[])) for later in observation_records)
            if not p.is_relative_to(folder.parent) or not matches:
                raise SafetyError('Prior observation raw/preview changed.')
            if not p.is_relative_to(folder):external[p.relative_to(folder.parent.parent).as_posix()]=ref['sha256']
        captures.append(capture)
    if len(captures)!=14 or len({c['capture_id'] for c in captures})!=14 or len({c['adapter_session_id'] for c in captures})!=1:
        raise SafetyError('Prior refocus requires fourteen distinct durable captures in one session.')
    files=['reference-refocus-proof.json','reference-refocus-budget.json','reference-correction-verification.json','exposure-adjustment.json']
    for name in ('calibration','initial-edge-focus','observations'):
        files.extend(p.relative_to(folder).as_posix() for p in (folder/name).rglob('*') if p.is_file())
    if len(files)>256:raise SafetyError('Prior correspondence evidence exceeds its fixed bound.')
    return measured,files,external

def build(project,folder,status,z_min_um,z_max_um,process_checker,focus_travel_policy,consumer_run_id=None):
    from .scan_continuation import _run,_eligible,latest_saved_capture,settled_motion_checkpoint
    from .startup_continuation import donor_contract
    from .reference_focus_recovery import build as historical_build
    from .reference_correspondence import POLICY_SHA
    from .storage import operator_holds
    request=read(folder/'scan-request.json');historical=request['continuation'];donor=donor_contract(historical)
    if focus_travel_policy!='controller' or (z_min_um,z_max_um)!=(donor['limits']['z_min'],donor['limits']['z_max']):raise SafetyError('Correspondence cannot change the controller envelope.')
    parent=_run(project,historical['prior_run_id']);parent_status=_eligible(parent,process_checker)
    rebuilt=historical_build(project,parent,parent_status,z_min_um,z_max_um,process_checker,'controller',folder.name,historical_contract=historical)
    if rebuilt!=historical:raise SafetyError('Verified historical refocus contract changed.')
    if [{k:h[k] for k in ('path','sha256')} for h in operator_holds()]!=read(folder/'acquisition-authorization.json')['holds']:raise SafetyError('Authorized holds changed.')
    for pid in (status.get('memory_cleanup_launch',{}).get('pid'),read(folder/'memory-guard/status.json').get('guard_pid')):
        if pid and process_checker(pid):raise SafetyError('Prior acquisition helper remains active.')
    consumed=folder/'supervision/adaptive-focus-continuation.json'
    if consumed.exists():
        item=read(consumed)
        if not consumer_run_id or item.get('new_run')!=consumer_run_id:raise SafetyError('Correspondence continuation already consumed.')
        child=_run(project,consumer_run_id)
        if (item.get('new_request_sha256')!=digest(child/'scan-request.json') or item.get('parent_request_sha256')!=digest(folder/'scan-request.json')
                or read(child/'scan-request.json').get('continuation',{}).get('prior_run_id')!=folder.name):raise SafetyError('Correspondence consumption does not match exact child.')
    elif consumer_run_id:raise SafetyError('Correspondence child lacks its exclusive receipt.')
    measured,files,external=prior_measurement(folder,_run(project,donor['prior_run_id']))
    latest=latest_saved_capture(folder);finished=datetime.fromisoformat(status['finished_at'].replace('Z','+00:00')).timestamp()
    if latest['at']>finished:raise SafetyError('Prior capture occurred after closure.')
    checkpoint=settled_motion_checkpoint(project,latest,finished,donor['limits'])
    if any(abs(checkpoint['position'][a]-measured[a])>(.25 if a=='z' else .5) for a in 'xyz'):raise SafetyError('Latest full motion checkpoint differs from resolved pose.')
    files+=['scan-request.json','scan-status.json','worker-start.json','recovery-needed.json','heartbeat.json','launch.json','acquisition-authorization.json','continuation-verification.json','camera-profile-used.json','session-instruction.json','focus-travel.json']
    result=copy.deepcopy(donor)
    result.update(prior_run_id=folder.name,continuation_kind=KIND,last_capture=latest,motion_checkpoint=checkpoint,
        prior_files_sha256={n:digest(folder/n) for n in files},reference_correspondence={
            'donor_contract':donor,'verified_historical_refocus':historical,'historical_consumption_sha256':digest(parent/'supervision/adaptive-focus-continuation.json'),
            'measured_position_um':measured,'source_focus_result_sha256':digest(folder/'initial-edge-focus/focus-result.json'),
            'external_observation_files_sha256':external,'policy_sha256':POLICY_SHA,'maximum_seconds':600.,'maximum_captures':64,'exact_capture_attempts':2,
            'old_focus_reuse_for_campaign':False,'original_reference_remains_pinned':True})
    return result

def stationary_pose(scope,contract):
    expected=contract['reference_correspondence']['measured_position_um'];actual=scope.position();scope.limits.check(actual)
    if any(abs(getattr(actual,a)-expected[a])>(.25 if a=='z' else .5) for a in 'xyz'):raise SafetyError('Measured reference pose changed; correspondence permits no return motion.')
    return actual

def invalidate(run,reason):
    """An owned new run may never retain a reusable late successful seed."""
    for path in (run/'reference-correspondence-proof.json',run/'reference-correction-verification.json',run/'reference-validation.json',run/'initial-edge-focus/focus-result.json',run/'initial-edge-focus/recovery.json'):
        if path.exists():
            value=read(path);value.update(accepted=False,success=False,acceptance_state='invalidated',state='failed',invalidation_reason=str(reason))
            try:atomic_json(path,value)
            except BaseException:
                path.unlink(missing_ok=True) # Only this new run's own incomplete acceptance.
                raise

def acquire_reference(scope,config,settings,run,contract,update,validate_final):
    from .acquisition_budget import AcquisitionBudget,BudgetScope
    from .reference_correspondence import capacity,lock_decision,POLICY_SHA
    from .reference_repair import validate_plan,PLAN
    from .startup_continuation import donor_contract
    from .imaging import load_frame,save_frame
    from .vendor_lifecycle import acknowledge_raw
    policy=contract['reference_correspondence'];budget=AcquisitionBudget(600.,64);full=BudgetScope(scope,budget)
    if policy.get('policy_sha256')!=POLICY_SHA or policy.get('exact_capture_attempts')!=2:raise SafetyError('Unreviewed correspondence policy.')
    def guard():
        try:full.check_cancel()
        except BaseException as exc:invalidate(run,exc);raise
    donor=run.parent/donor_contract(contract)['prior_run_id'];plan,_=validate_plan(donor);prior_raw=donor/Path(PLAN).parent/plan['focused_frame']
    records=[];complete=False
    try:
        capacity(guard);prior=load_frame(prior_raw);guard();stationary_pose(scope,contract)
        for index in (1,2):
            guard();stationary_pose(scope,contract);update('reference_correspondence',correspondence_frame=index,reference_focus_budget=budget.report())
            frame=full.capture()
            raw=run/('correspondence/frame-1.tiff' if index==1 else 'initial-edge-focus/focused-full-field.tiff')
            save_frame(raw.parent,raw.stem,frame);receipt=acknowledge_raw(scope,frame,raw)
            validate_raw_receipt(frame,raw,receipt);guard();stationary_pose(scope,contract)
            expected=policy['measured_position_um']
            if any(abs(getattr(frame.position,a)-expected[a])>(.25 if a=='z' else .5) for a in 'xyz'):raise SafetyError('Fresh correspondence frame moved from measured pose.')
            decision,path=lock_decision(prior,frame,prior_raw,raw,run/'correspondence','frame-'+str(index)+'-decision',guard)
            quality=validate_final(frame,decision,path,raw,guard);guard()
            if quality.get('accepted') is not True:raise AcquisitionError('Fresh corresponding reference rejected.')
            quality_path=run/'correspondence'/('frame-'+str(index)+'-quality.json');atomic_json(quality_path,quality);guard()
            records.append({'raw':raw.relative_to(run).as_posix(),'raw_sha256':digest(raw),'metadata_sha256':digest(raw.with_suffix('.json')),
                'vendor_receipt':receipt,'capture_id':frame.metadata['zen_vendor_result']['capture_id'],'adapter_session_id':frame.metadata['zen_vendor_result']['adapter_session_id'],
                'decision':path.relative_to(run).as_posix(),'decision_sha256':digest(path),'quality':quality_path.relative_to(run).as_posix(),'quality_sha256':digest(quality_path),
                'position_um':asdict(frame.position),'center_shift':decision['geometry']['channels'][1]['full']['roi_center_shift'],
                'score':quality['focus']['fresh']['score']})
            if index==1:del frame,decision,quality
        if (records[0]['capture_id']==records[1]['capture_id'] or records[0]['adapter_session_id']!=records[1]['adapter_session_id']
                or any(abs(a-b)>1 for a,b in zip(records[0]['center_shift'],records[1]['center_shift']))):raise SafetyError('Two independent stationary confirmations disagree.')
        guard();operation=budget.report()
        if operation['captures_started']!=2:raise SafetyError('Correspondence must make exactly two physical attempts.')
        folder=run/'initial-edge-focus'
        result={'success':True,'outcome':'resolved_peak','mode':'reconfirmed_saved_peak','z_um':policy['measured_position_um']['z'],
            'score':min(r['score'] for r in records),'prominence':None,'reason':'Pinned prior independently resolved peak; two NEW corresponding stationary full-frame confirmations.',
            'source_run':contract['prior_run_id'],'source_result_sha256':policy['source_focus_result_sha256'],'samples':records,'budget':operation}
        atomic_json(folder/'focus-result.json',result);guard()
        atomic_json(folder/'recovery.json',{'state':'focused','mode':'reconfirmed_saved_peak','result':result,'confirmation':records[1]});guard()
        atomic_json(run/'reference-correspondence-budget.json',operation);guard()
        proof={'schema':1,'kind':KIND,'run_id':run.name,'request_sha256':digest(run/'scan-request.json'),
            'contract_sha256':hashlib.sha256(json.dumps(contract,sort_keys=True).encode()).hexdigest(),'policy_sha256':POLICY_SHA,
            'acceptance_state':'accepted_reference','records':records,'budget':operation,'budget_sha256':digest(run/'reference-correspondence-budget.json'),
            'result_sha256':digest(folder/'focus-result.json'),'recovery_sha256':digest(folder/'recovery.json')}
        atomic_json(run/'reference-correspondence-proof.json',proof);guard()
        quality['correspondence_proof_sha256']=digest(run/'reference-correspondence-proof.json');atomic_json(run/'reference-correction-verification.json',quality);guard()
        complete=True
        return frame,{'enabled':False,'attempts':records,'final_reference_choice':'two_fresh_corresponding_stationary_confirmations'},receipt,proof,guard
    except BaseException as exc:
        invalidate(run,exc);raise
    finally:
        if not complete:atomic_json(run/'reference-correspondence-budget.json',budget.report())

def campaign_seed(run,proof):
    folder=run/'initial-edge-focus';quality=read(run/'reference-correction-verification.json');result=read(folder/'focus-result.json')
    if (proof!=read(run/'reference-correspondence-proof.json') or proof.get('acceptance_state')!='accepted_reference'
            or proof.get('request_sha256')!=digest(run/'scan-request.json') or quality.get('accepted') is not True
            or quality.get('correspondence_proof_sha256')!=digest(run/'reference-correspondence-proof.json')
            or proof.get('result_sha256')!=digest(folder/'focus-result.json') or proof.get('recovery_sha256')!=digest(folder/'recovery.json')
            or proof.get('budget_sha256')!=digest(run/'reference-correspondence-budget.json') or result.get('success') is not True):raise SafetyError('Correspondence seed lacks final accepted evidence.')
    for r in proof['records']:
        for key,name in [('raw_sha256',r['raw']),('metadata_sha256',str(Path(r['raw']).with_suffix('.json'))),('decision_sha256',r['decision']),('quality_sha256',r['quality'])]:
            if r[key]!=digest(run/name):raise SafetyError('Correspondence seed evidence changed.')
    return {'seed_focus_z_um':result['z_um'],'seed_focus_evidence':folder/'focus-result.json','seed_focus_frame':folder/'focused-full-field.tiff'}
