"""One exact failed pre-capture continuation startup, with an explicit donor.

The immediate failed run is lineage, not a fabricated campaign checkpoint.
The original donor supplies unchanged reference/focus/progress evidence.
"""
from datetime import datetime
from pathlib import Path
import copy,re
from .model import SafetyError

KIND='diagnosed_pre_job_vendor_backlog_startup_recovery_v1'
ERROR=re.compile(r'Unacknowledged vendor output limit: (\d+) jobs, (\d+) bytes\. Persist/review pending work before another capture; no files were deleted\.')

def donor_contract(contract):
    if contract.get('reference_correspondence'):return contract['reference_correspondence']['donor_contract']
    return contract.get('reference_focus_recovery',{}).get('donor_contract',
        contract.get('startup_recovery',{}).get('donor_contract',contract))

def recognized(folder,status):
    from .scan_continuation import read,digest
    if not (status.get('state')=='failed' and status.get('hardware_closed') is True
            and status.get('error_type')=='AcquisitionError' and ERROR.fullmatch(status.get('error',''))
            and status.get('failed_phase')=='reference_exposure' and status.get('exposure_attempt')==1
            and status.get('watchdog_reason') is None and status.get('stop_errors')==[]):return False
    if any((folder/name).exists() for name in ('STOP','watchdog.json','scan-config.json','batch.json','reference-validation.json','exposure-adjustment.json','observations','calibration')):return False
    try:
        request=read(folder/'scan-request.json');recovery=read(folder/'recovery-needed.json')
        proof=read(folder/'continuation-verification.json');worker=read(folder/'worker-start.json')
        continuation=request['continuation'];heartbeat=read(folder/'heartbeat.json')
        start=datetime.fromisoformat(status['started_at'].replace('Z','+00:00')).timestamp()
        end=datetime.fromisoformat(status['finished_at'].replace('Z','+00:00')).timestamp()
        trace=recovery['traceback']
        return (continuation.get('continuation_kind')=='supervised_reference_correction'
            and not continuation.get('startup_recovery') and proof.get('continuation')==continuation
            and proof.get('checkpoint',{}).get('verified_before_arm') is True
            and request.get('source_release')==status.get('source_release')==worker.get('source_release')==recovery.get('source_release')
            and worker.get('worker_sha256')==request['source_release']['files']['scan_worker.py']
            and recovery.get('run_id')==folder.name and recovery.get('request_sha256')==digest(folder/'scan-request.json')
            and recovery.get('hardware_closed') is True and recovery.get('failed_phase')==status['failed_phase']
            and recovery.get('error_type')==status['error_type'] and recovery.get('error')==status['error']
            and status.get('exposure_ms')==request['settings']['exposure_ms']
            and heartbeat.get('phase')=='reference_exposure' and heartbeat.get('pid')==status['pid']
            and start<=heartbeat.get('at',0)<=end
            and all(value in trace for value in ('_capture_reference','_capture_job','_check_pending_vendor_output','vendor_lifecycle.py','in check_pending'))
            and not list(folder.rglob('*.tiff')))
    except (OSError,KeyError,TypeError,ValueError):return False

def build(project,folder,status,z_min_um,z_max_um,process_checker,focus_travel_policy,consumer_run_id=None,*,historical_contract=None):
    from .scan_continuation import read,digest,_run,build_contract,settled_motion_checkpoint
    from .vendor_backlog import validated_reviews
    from .storage import operator_holds
    if focus_travel_policy!='controller':raise SafetyError('Failed startup recovery retains controller focus travel.')
    request=read(folder/'scan-request.json');donor=request['continuation'];parent=_run(project,donor['prior_run_id'])
    if z_min_um!=donor['limits']['z_min'] or z_max_um!=donor['limits']['z_max']:
        raise SafetyError('Failed startup recovery cannot change its checkpoint limits.')
    # A consumed donor is permitted only for the exact failed child under review.
    receipt=read(parent/'supervision/adaptive-focus-continuation.json')
    if receipt.get('new_run')!=folder.name or receipt.get('new_request_sha256')!=digest(folder/'scan-request.json'):
        raise SafetyError('Original donor continuation was not consumed by this failed startup.')
    if receipt.get('parent_request_sha256')!=donor['prior_files_sha256']['scan-request.json']:
        raise SafetyError('Original donor receipt request differs.')
    original=build_contract(project,parent.name,donor['limits']['z_min'],z_max_um=donor['limits']['z_max'],
                            process_checker=process_checker,focus_travel_policy='controller',
                            _motion_prefix=donor['motion_checkpoint']['prefix_bytes'])
    if original!=donor:raise SafetyError('Original verified donor contract changed.')
    consumed=folder/'supervision/adaptive-focus-continuation.json'
    if consumed.exists():
        item=read(consumed)
        if not consumer_run_id or item.get('new_run')!=consumer_run_id:
            raise SafetyError('Failed startup continuation has already been consumed.')
        child=_run(project,consumer_run_id);child_request=read(child/'scan-request.json')
        if (item.get('new_request_sha256')!=digest(child/'scan-request.json')
                or item.get('parent_request_sha256')!=digest(folder/'scan-request.json')
                or child_request.get('continuation',{}).get('prior_run_id')!=folder.name):
            raise SafetyError('Consumed startup receipt does not match its exact child.')
    elif consumer_run_id:raise SafetyError('Startup child has no exclusive continuation receipt.')
    historical_prefix=None
    if historical_contract is not None:
        if not consumer_run_id:raise SafetyError('Historical startup requires its exact consumed child.')
        child=_run(project,consumer_run_id)
        if (historical_contract.get('continuation_kind')!=KIND or historical_contract.get('prior_run_id')!=folder.name
                or read(child/'scan-request.json').get('continuation')!=historical_contract
                or read(child/'continuation-verification.json').get('continuation')!=historical_contract):
            raise SafetyError('Historical startup prefix is not bound to the verified consumed child.')
        historical_prefix=historical_contract['motion_checkpoint']['prefix_bytes']
    authorization=read(folder/'acquisition-authorization.json')
    actual=operator_holds()
    if [{k:h[k] for k in ('path','sha256')} for h in actual]!=authorization.get('holds'):
        raise SafetyError('Operator holds changed since the authorized failed startup.')
    for name,key in [('memory-guard-launch.json','pid')]:
        if (folder/name).exists() and process_checker(read(folder/name)[key]):raise SafetyError('Prior guard remains alive.')
    cleanup=status.get('memory_cleanup_launch',{})
    if cleanup.get('pid') and process_checker(cleanup['pid']):raise SafetyError('Prior cleanup wrapper remains alive.')
    if (folder/'memory-guard/status.json').is_file():
        guard=read(folder/'memory-guard/status.json')
        if process_checker(guard['guard_pid']):raise SafetyError('Prior memory guard remains alive.')
    reviews=validated_reviews(Path(project)/'data/zen-captures')
    if not reviews:raise SafetyError('Pending vendor backlog has no independently verified retained review.')
    from .vendor_lifecycle import PENDING_JOBS,PENDING_BYTES,NEXT_JOB_BYTES,redirected
    root=Path(project)/'data/zen-captures';pending=[]
    for child in root.iterdir():
        if child.is_dir() and not redirected(child):
            pending.extend((child.name,p.stat().st_size) for p in child.glob('*.czi'))
    match=ERROR.fullmatch(status['error'])
    if (len(pending),sum(n for _,n in pending))!=tuple(map(int,match.groups())):
        raise SafetyError('Retained backlog no longer matches the exact pre-job failure.')
    remaining=[n for identifier,n in pending if identifier not in reviews]
    if len(remaining)>=PENDING_JOBS or sum(remaining)+NEXT_JOB_BYTES>PENDING_BYTES:
        raise SafetyError('Reviewed retention has not resolved the bounded pending-output failure.')
    review_pins={'data/vendor-retained-reviews/'+identifier+'.json':digest(Path(project)/'data/vendor-retained-reviews'/(identifier+'.json')) for identifier in reviews}
    finished=datetime.fromisoformat(status['finished_at'].replace('Z','+00:00')).timestamp()
    checkpoint=settled_motion_checkpoint(project,donor['last_capture'],finished,donor['limits'],log_prefix_bytes=historical_prefix)
    target=donor['reference_correction']['position_um']
    if any(abs(checkpoint['position'][a]-target[a])>(.25 if a=='z' else .5) for a in 'xyz'):
        raise SafetyError('Failed startup did not settle at the exact correction reference.')
    names=['scan-request.json','scan-status.json','worker-start.json','recovery-needed.json','heartbeat.json',
           'launch.json','acquisition-authorization.json','continuation-verification.json','camera-profile-used.json',
           'session-instruction.json','focus-travel.json']
    names.extend(n for n in ('memory-guard-launch.json',) if (folder/n).is_file())
    result=copy.deepcopy(donor)
    result.update(prior_run_id=folder.name,continuation_kind=KIND,motion_checkpoint=checkpoint,
                  prior_files_sha256={n:digest(folder/n) for n in names},
                  startup_recovery={'donor_contract':donor,'retained_review_files_sha256':review_pins,
                                    'original_consumption_receipt_sha256':digest(parent/'supervision/adaptive-focus-continuation.json'),
                                    'stationary_readbacks_required':5,'prior_captures_committed':0})
    return result
