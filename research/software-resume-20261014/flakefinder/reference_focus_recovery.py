"""One classified reference-focus refusal and a newly measured reference height.

No connection or retries. The caller owns the one leased scope; all reference
captures and autofocus share one time/capture budget. Old evidence is immutable.
"""
from dataclasses import asdict
from datetime import datetime
from pathlib import Path
import copy,hashlib,json,math,re
import numpy as np
from .model import SafetyError,AcquisitionError,atomic_json

KIND='diagnosed_controls_pass_reference_refocus_v1'
ERROR='Focused reference failed fresh focus confirmation or independent chip/backing controls.'
MAX_SECONDS=600.;MAX_CAPTURES=64

def recognized(folder,status):
    from .scan_continuation import read,digest
    from .startup_continuation import KIND as STARTUP_KIND
    if not (status.get('state')=='failed' and status.get('hardware_closed') is True
            and status.get('error_type')=='AcquisitionError' and status.get('error')==ERROR
            and status.get('failed_phase')=='reference_exposure' and status.get('stop_errors')==[]
            and status.get('watchdog_reason') is None):return False
    if any((folder/n).exists() for n in ('STOP','watchdog.json','scan-config.json','batch.json','reference-validation.json')):return False
    try:
        request=read(folder/'scan-request.json');worker=read(folder/'worker-start.json');failure=read(folder/'recovery-needed.json')
        contract=request['continuation'];proof=read(folder/'continuation-verification.json');quality=read(folder/'reference-correction-verification.json')
        controls=quality['controls'];focus=quality['focus']
        return (contract.get('continuation_kind')==STARTUP_KIND and not contract.get('reference_focus_recovery')
            and proof.get('continuation')==contract and proof['checkpoint'].get('verified_before_arm') is True
            and proof['checkpoint'].get('stationary_readbacks_required')==5 and len(proof['checkpoint'].get('stationary_readbacks_um',[]))==5
            and quality.get('accepted') is False and len(controls)==4 and all(c.get('accepted') is True for c in controls)
            and focus.get('accepted') is False and focus.get('minimum_score_fraction')==.95
            and request.get('source_release')==status.get('source_release')==worker.get('source_release')==failure.get('source_release')
            and worker.get('worker_sha256')==request['source_release']['files']['scan_worker.py']
            and failure.get('request_sha256')==digest(folder/'scan-request.json') and failure.get('run_id')==folder.name
            and failure.get('error')==ERROR and failure.get('error_type')=='AcquisitionError'
            and failure.get('hardware_closed') is True)
    except (OSError,KeyError,TypeError,ValueError):return False

def validate_refusal(folder,donor_folder,plan):
    from .scan_continuation import read,digest
    from .imaging import load_frame
    from .reference_repair import check_controls
    exposure=read(folder/'exposure-adjustment.json')
    if exposure.get('enabled') is not False or len(exposure.get('attempts',[]))!=1:
        raise SafetyError('Refocus requires the one fixed-exposure reference attempt.')
    attempt=exposure['attempts'][0];raw=folder/'calibration/reference-attempt-01.tiff';sidecar=raw.with_suffix('.json')
    if (Path(attempt['raw_path'])!=raw or attempt['raw_sha256']!=digest(raw)
            or attempt['metadata_sha256']!=digest(sidecar)):
        raise SafetyError('Rejected reference raw evidence changed.')
    frame=load_frame(raw);receipt=attempt['vendor_receipt']
    validate_raw_receipt(frame,raw,receipt)
    archive=raw.with_suffix('.zen-vendor.json')
    from .reference_repair import PLAN
    quality=check_controls(frame,(donor_folder/PLAN).parent,plan)
    if quality!=read(folder/'reference-correction-verification.json'):
        raise SafetyError('Saved reference rejection differs from independent raw/control recomputation.')
    if quality['accepted'] or not all(c['accepted'] for c in quality['controls']) or quality['focus']['accepted']:
        raise SafetyError('This recovery requires only the unchanged focus gate to have failed.')
    return [raw.relative_to(folder).as_posix(),sidecar.relative_to(folder).as_posix(),archive.relative_to(folder).as_posix(),
            'exposure-adjustment.json','reference-correction-verification.json']

def validate_raw_receipt(frame,raw,receipt):
    from .scan_continuation import read,digest
    sidecar=raw.with_suffix('.json');capture=frame.metadata['zen_vendor_result']
    run=next((p for p in raw.parents if p.name.startswith('scan-') and p.parent.name=='data'),None)
    identifier=capture.get('capture_id');session=capture.get('adapter_session_id')
    if (run is None or not isinstance(identifier,str) or not re.fullmatch('[0-9a-f]{32}',identifier)
            or not isinstance(session,str) or not re.fullmatch('[0-9a-f]{32}',session)
            or Path(capture.get('result_folder',''))!=run.parent/'zen-captures'/identifier
            or receipt.get('vendor_result_folder')!=capture['result_folder']
            or capture.get('raw_shape')!=list(frame.rgb.shape) or capture.get('raw_dtype')!=str(frame.rgb.dtype)):
        raise SafetyError('Reference raw acknowledgement lacks its exact vendor job/session identity.')
    archive=raw.with_suffix('.zen-vendor.json');saved=read(archive)
    if (receipt.get('status')!='removed' or receipt.get('raw_tiff')!=str(raw)
            or receipt.get('archive')!=str(archive) or receipt.get('archive_sha256')!=digest(archive)
            or saved.get('status')!='removed_after_verified_raw_save' or saved.get('capture')!=capture
            or saved.get('raw_tiff')!=str(raw) or saved.get('raw_sidecar')!=str(sidecar)
            or saved.get('raw_tiff_sha256')!=digest(raw) or saved.get('raw_sidecar_sha256')!=digest(sidecar)
            or capture.get('job_completed') is not True or capture.get('job_unloaded') is not True
            or hashlib.sha256(np.ascontiguousarray(frame.rgb).tobytes()).hexdigest()!=capture.get('raw_rgb_sha256')):
        raise SafetyError('Rejected reference lacks the verified full-raw vendor acknowledgement.')
    if Path(receipt['vendor_result_folder']).exists():raise SafetyError('Acknowledged vendor result unexpectedly exists.')

def build(project,folder,status,z_min_um,z_max_um,process_checker,focus_travel_policy,consumer_run_id=None,*,historical_contract=None):
    from .scan_continuation import read,digest,_run,_eligible,latest_saved_capture,settled_motion_checkpoint
    from .startup_continuation import build as rebuild_startup,donor_contract
    from .storage import operator_holds
    if focus_travel_policy!='controller':raise SafetyError('Reference refocus retains controller travel policy.')
    request=read(folder/'scan-request.json');historical=request['continuation'];donor=donor_contract(historical)
    if (z_min_um,z_max_um)!=(donor['limits']['z_min'],donor['limits']['z_max']):raise SafetyError('Refocus checkpoint limits changed.')
    parent=_run(project,historical['prior_run_id']);parent_status=_eligible(parent,process_checker)
    rebuilt=rebuild_startup(project,parent,parent_status,z_min_um,z_max_um,process_checker,'controller',folder.name,historical_contract=historical)
    if rebuilt!=historical:raise SafetyError('Consumed historical startup contract changed.')
    donor_folder=_run(project,donor['prior_run_id']);files=validate_refusal(folder,donor_folder,donor['reference_correction'])
    auth=read(folder/'acquisition-authorization.json')
    if [{k:h[k] for k in ('path','sha256')} for h in operator_holds()]!=auth['holds']:
        raise SafetyError('Operator holds changed since reference refusal.')
    for pid in (status.get('memory_cleanup_launch',{}).get('pid'),read(folder/'memory-guard/status.json').get('guard_pid')):
        if pid and process_checker(pid):raise SafetyError('Prior reference owner helper has not exited.')
    consumed=folder/'supervision/adaptive-focus-continuation.json'
    if consumed.exists():
        value=read(consumed)
        if not consumer_run_id or value.get('new_run')!=consumer_run_id:raise SafetyError('Reference refocus continuation was already consumed.')
        child=_run(project,consumer_run_id)
        if (value.get('new_request_sha256')!=digest(child/'scan-request.json') or value.get('parent_request_sha256')!=digest(folder/'scan-request.json')
                or read(child/'scan-request.json').get('continuation',{}).get('prior_run_id')!=folder.name):
            raise SafetyError('Reference-refocus consumption does not match the exact child.')
    elif consumer_run_id:raise SafetyError('Reference-refocus child has no exclusive receipt.')
    historical_prefix=None
    if historical_contract is not None:
        if not consumer_run_id:raise SafetyError('Historical refocus requires its exact consumed child.')
        child=_run(project,consumer_run_id)
        if (historical_contract.get('continuation_kind')!=KIND or historical_contract.get('prior_run_id')!=folder.name
                or read(child/'scan-request.json').get('continuation')!=historical_contract
                or read(child/'continuation-verification.json').get('continuation')!=historical_contract):
            raise SafetyError('Historical refocus prefix is not bound to the verified consumed child.')
        historical_prefix=historical_contract['motion_checkpoint']['prefix_bytes']
    latest=latest_saved_capture(folder);finished=datetime.fromisoformat(status['finished_at'].replace('Z','+00:00')).timestamp()
    if latest['at']>finished:raise SafetyError('Reference capture was acquired after closure.')
    checkpoint=settled_motion_checkpoint(project,latest,finished,donor['limits'],log_prefix_bytes=historical_prefix)
    files.extend(['scan-request.json','scan-status.json','worker-start.json','recovery-needed.json','heartbeat.json','launch.json',
                  'acquisition-authorization.json','continuation-verification.json','camera-profile-used.json','session-instruction.json','focus-travel.json'])
    plan=donor['reference_correction'];selection=read(donor_folder/Path('supervision/reference-repair')/plan['selection'])
    result=copy.deepcopy(donor)
    result.update(prior_run_id=folder.name,continuation_kind=KIND,last_capture=latest,motion_checkpoint=checkpoint,
                  prior_files_sha256={n:digest(folder/n) for n in files},
                  reference_focus_recovery={'donor_contract':donor,'verified_historical_startup':historical,
                    'historical_consumption_sha256':digest(parent/'supervision/adaptive-focus-continuation.json'),
                    'fixed_roi_box_px':selection['box_px'],'maximum_seconds':MAX_SECONDS,'maximum_captures':MAX_CAPTURES,
                    'initial_step_um':2.,'growth':2.,'minimum_prior_focus_score_fraction':.95,
                    'new_measured_focus_required':True,'old_focus_reuse_for_campaign':False})
    return result

def acquire_reference(scope,config,settings,run,position,check,update,capture_reference,contract,validate_final):
    """One physical capture counter spans initial frame, AF, and final full frame."""
    from .acquisition_budget import AcquisitionBudget,BudgetScope
    from .full_chip import FocusROIAdapter
    from .model import CameraTransform
    from .focus_recovery import recover_focus
    from .imaging import save_frame
    from .vendor_lifecycle import acknowledge_raw
    from .startup_continuation import donor_contract
    from .scan_continuation import digest
    policy=contract['reference_focus_recovery'];budget=AcquisitionBudget(MAX_SECONDS,MAX_CAPTURES)
    full=BudgetScope(scope,budget);folder=run/'initial-edge-focus';folder.mkdir(parents=True,exist_ok=True);budget_saved=False
    try:
        initial,audit,_=capture_reference(full,config,settings,run,position,check,update)
        update('reference_refocus',reference_focus_budget=budget.report())
        roi=FocusROIAdapter(scope,64.,CameraTransform(**config['camera_transform']),box=policy['fixed_roi_box_px'],reference_frame=initial,edge=True)
        roi.selection={'method':'original_hash_pinned_reference_roi','box_px':policy['fixed_roi_box_px']}
        counted_roi=BudgetScope(roi,budget)
        result=recover_focus(counted_roi,scope.limits.z_min,scope.limits.z_max,output=folder,
                             initial_step_um=2.,growth=2.,max_elapsed_s=MAX_SECONDS,max_captures=MAX_CAPTURES,
                             callback=lambda _f,row:update('reference_refocus',reference_focus_budget=budget.report(),focus_sample=row))
        if not result.success or result.outcome!='resolved_peak':raise AcquisitionError('Reference same-ROI autofocus unresolved: '+result.reason)
        full.check_cancel();frame=full.capture()
        save_frame(folder,'focused-full-field',frame);path=folder/'focused-full-field.tiff'
        receipt=acknowledge_raw(scope,frame,path);full.check_cancel()
        if abs(frame.position.z-result.z_um)>.25:raise SafetyError('Final reference frame differs from independently confirmed focus height.')
        operation_budget=budget.report();atomic_json(run/'reference-refocus-budget.json',operation_budget)
        proof={'schema':1,'kind':'independently_measured_reference_focus_v1','run_id':run.name,
               'contract_sha256':hashlib.sha256(json.dumps(contract,sort_keys=True).encode()).hexdigest(),
               'original_position_um':donor_contract(contract)['reference_correction']['position_um'],
               'measured_position_um':asdict(frame.position),'fixed_roi_box_px':policy['fixed_roi_box_px'],
               'result_sha256':digest(folder/'focus-result.json'),'recovery_sha256':digest(folder/'recovery.json'),
               'full_frame_sha256':digest(path),'full_metadata_sha256':digest(path.with_suffix('.json')),
               'vendor_receipt':receipt,'budget':operation_budget,'budget_sha256':digest(run/'reference-refocus-budget.json'),
               'independent_confirmation_required':True,'acceptance_state':'measurement_only'}
        atomic_json(run/'reference-refocus-proof.json',proof)
        # Reload/raw hashes, same-ROI controls, and the generic fresh-reference
        # gate are part of this same operation, not trusted after a stale timer.
        corrected=validate_final(frame,proof)
        if corrected.get('accepted') is not True:raise AcquisitionError('Final corrected reference was not accepted.')
        full.check_cancel()
        operation_budget=budget.report();atomic_json(run/'reference-refocus-budget.json',operation_budget)
        proof.update(budget=operation_budget,budget_sha256=digest(run/'reference-refocus-budget.json'),acceptance_state='accepted_reference')
        atomic_json(run/'reference-refocus-proof.json',proof)
        full.check_cancel()
        corrected['measured_focus_proof_sha256']=digest(run/'reference-refocus-proof.json')
        atomic_json(run/'reference-correction-verification.json',corrected);full.check_cancel();budget_saved=True
        audit={**audit,'final_reference_choice':'independently_measured_refocus_full_field',
               'final_reference_raw':str(path),'refocus_proof_sha256':digest(run/'reference-refocus-proof.json')}
        return frame,audit,receipt,proof
    finally:
        if not budget_saved:atomic_json(run/'reference-refocus-budget.json',budget.report())

def validate_measured_proof(frame,parent,run,proof):
    """Return a measured plan only after checking bound result, confirmation/raw."""
    from .scan_continuation import read,digest
    from .reference_repair import validate_plan
    contract=read(run/'scan-request.json')['continuation'];policy=contract.get('reference_focus_recovery')
    if (contract.get('continuation_kind')!=KIND or not policy or proof!=read(run/'reference-refocus-proof.json')
            or policy.get('maximum_seconds')!=MAX_SECONDS or policy.get('maximum_captures')!=MAX_CAPTURES
            or policy.get('minimum_prior_focus_score_fraction')!=.95):raise SafetyError('Measured reference proof is missing or changed.')
    folder=run/'initial-edge-focus';result=read(folder/'focus-result.json');recovery=read(folder/'recovery.json')
    expected={'result_sha256':folder/'focus-result.json','recovery_sha256':folder/'recovery.json',
              'full_frame_sha256':folder/'focused-full-field.tiff','full_metadata_sha256':folder/'focused-full-field.json',
              'budget_sha256':run/'reference-refocus-budget.json'}
    if any(proof.get(key)!=digest(path) for key,path in expected.items()):raise SafetyError('Measured reference files changed.')
    plan,_=validate_plan(parent)
    if (proof.get('kind')!='independently_measured_reference_focus_v1' or proof.get('run_id')!=run.name
            or proof.get('contract_sha256')!=hashlib.sha256(json.dumps(contract,sort_keys=True).encode()).hexdigest()
            or proof.get('fixed_roi_box_px')!=policy['fixed_roi_box_px'] or proof.get('original_position_um')!=plan['position_um']
            or proof.get('measured_position_um')!=asdict(frame.position) or proof.get('independent_confirmation_required') is not True
            or result.get('success') is not True
            or result.get('outcome')!='resolved_peak' or recovery.get('state')!='focused'
            or recovery.get('result',{}).get('success') is not True or not recovery.get('confirmation')
            or abs(frame.position.z-result['z_um'])>.25 or abs(frame.position.z-recovery['confirmation']['z_um'])>.25):
        raise SafetyError('Measured reference lacks consistent independent focus confirmation.')
    from .imaging import load_frame
    saved=load_frame(folder/'focused-full-field.tiff')
    if (saved.metadata!=frame.metadata or saved.position!=frame.position or saved.pixel_size_um!=frame.pixel_size_um
            or not np.array_equal(saved.rgb,frame.rgb)):raise SafetyError('Final measured reference differs from persisted full raw.')
    validate_raw_receipt(frame,folder/'focused-full-field.tiff',proof['vendor_receipt'])
    b=proof['budget']
    if (b!=read(run/'reference-refocus-budget.json') or b.get('max_seconds')!=MAX_SECONDS or b.get('max_captures')!=MAX_CAPTURES
            or type(b.get('captures_started')) is not int or not 2<=b['captures_started']<=MAX_CAPTURES
            or not math.isfinite(b['elapsed_seconds']) or not 0<=b['elapsed_seconds']<MAX_SECONDS):
        raise SafetyError('Measured reference exceeded its total budget.')
    measured=copy.deepcopy(plan);measured['position_um']['z']=result['z_um']
    return measured

def campaign_seed(run,proof):
    """The typed successful measurement supplies the seed; never the old height."""
    from .scan_continuation import read,digest
    folder=run/'initial-edge-focus';result=read(folder/'focus-result.json')
    quality=read(run/'reference-correction-verification.json')
    evidence={'result_sha256':folder/'focus-result.json','recovery_sha256':folder/'recovery.json',
              'full_frame_sha256':folder/'focused-full-field.tiff','full_metadata_sha256':folder/'focused-full-field.json',
              'budget_sha256':run/'reference-refocus-budget.json'}
    if (proof.get('acceptance_state')!='accepted_reference' or proof!=read(run/'reference-refocus-proof.json')
            or quality.get('accepted') is not True or quality.get('measured_focus_proof_sha256')!=digest(run/'reference-refocus-proof.json')
            or any(proof.get(key)!=digest(path) for key,path in evidence.items()) or result.get('success') is not True):
        raise SafetyError('Measured reference campaign seed changed.')
    return {'seed_focus_z_um':result['z_um'],'seed_focus_evidence':folder/'focus-result.json','seed_focus_frame':folder/'focused-full-field.tiff'}
