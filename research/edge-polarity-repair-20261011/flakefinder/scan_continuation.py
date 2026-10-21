"""Explicit same-placement continuation contracts; no hardware construction.

The prior run is immutable. A new run keeps its original absolute XY envelope,
returns to its verified reference only after a leased position/optics check,
and performs the ordinary fresh-reference acquisition again.
"""
from datetime import datetime
from pathlib import Path
import ast
import ctypes
import hashlib
import json
import math
import os
import re
import time

from .model import Limits,Position,SafetyError

VERSION='same-placement-lower-z-extension-v1'
RAM_WATCHDOG_REASON='MemoryError: Available RAM is below the 2 GiB acquisition floor.'
FOCUS_HANDOFF_ERROR='Fresh color-reference XY differs from current readback. Supply the matching fresh discovery or a current-position reference.'
FOCUS_HANDOFF_FILES=('initial-edge-focus/focus-result.json','initial-edge-focus/recovery.json',
                     'initial-edge-focus/focused-full-field.tiff','initial-edge-focus/focused-full-field.json')


def read(path):return json.loads(Path(path).read_text(encoding='utf-8-sig'))


def digest(path):
    h=hashlib.sha256()
    with Path(path).open('rb') as stream:
        for block in iter(lambda:stream.read(1024**2),b''):h.update(block)
    return h.hexdigest()


def _run(project,identifier):
    project=Path(project).resolve(strict=True)
    if not re.fullmatch(r'scan-\d{8}-\d{6}-[a-f0-9]{8}',identifier):raise SafetyError('Invalid prior run identity.')
    folder=(project/'data'/identifier).resolve(strict=True)
    if folder.parent!=(project/'data').resolve():raise SafetyError('Prior run redirects outside the project.')
    return folder


def process_alive_strict(pid):
    """Access denial is unknown, never proof that the old worker exited."""
    if type(pid) is not int or pid<=0:raise SafetyError('Prior worker PID is unavailable.')
    if os.name!='nt':
        try:os.kill(pid,0);return True
        except ProcessLookupError:return False
        except OSError as exc:raise SafetyError('Cannot establish prior worker exit.') from exc
    api=ctypes.WinDLL('kernel32',use_last_error=True)
    api.OpenProcess.argtypes=[ctypes.c_ulong,ctypes.c_int,ctypes.c_ulong];api.OpenProcess.restype=ctypes.c_void_p
    api.GetExitCodeProcess.argtypes=[ctypes.c_void_p,ctypes.POINTER(ctypes.c_ulong)]
    api.CloseHandle.argtypes=[ctypes.c_void_p]
    handle=api.OpenProcess(0x1000,False,pid)
    if not handle:
        if ctypes.get_last_error() in (87,1168):return False
        raise SafetyError('Cannot establish prior worker exit; process query was denied or failed.')
    try:
        code=ctypes.c_ulong()
        if not api.GetExitCodeProcess(handle,ctypes.byref(code)):raise SafetyError('Prior worker exit query failed.')
        return code.value==259
    finally:api.CloseHandle(handle)


def _resource_cancellation(folder,status):
    """Exact diagnosed cancellation, not a general exception retry policy."""
    if not (status.get('state')=='failed' and status.get('error_type')=='FutureCancelledError'
            and status.get('error')=='' and status.get('watchdog_reason')==RAM_WATCHDOG_REASON):return False
    try:
        watchdog=read(folder/'watchdog.json');batch=read(folder/'batch.json')
        stop=(folder/'STOP').read_text(encoding='utf-8-sig').strip()
        triggered=datetime.fromisoformat(watchdog['triggered_at'].replace('Z','+00:00')).timestamp()
        started=datetime.fromisoformat(status['started_at'].replace('Z','+00:00')).timestamp()
        finished=datetime.fromisoformat(status['finished_at'].replace('Z','+00:00')).timestamp()
    except (OSError,ValueError,KeyError,TypeError):return False
    return (stop==RAM_WATCHDOG_REASON and watchdog.get('reason')==RAM_WATCHDOG_REASON
        and watchdog.get('pid')==status.get('pid') and watchdog.get('automatic_restart') is False
        and started<=triggered<=finished and batch.get('state')=='failed' and batch.get('phase')=='stopped'
        and batch.get('current_chip') is None and batch.get('error')=='')


def _focused_handoff_failure(folder,status):
    """Known software handoff refusal with a separately confirmed focus image."""
    if not (status.get('state')=='failed' and status.get('error_type')=='SafetyError'
            and status.get('error')==FOCUS_HANDOFF_ERROR and status.get('watchdog_reason') is None
            and status.get('stop_errors')==[]):return False
    try:
        for name in FOCUS_HANDOFF_FILES:
            path=folder/name
            if not path.is_file() or not path.resolve().is_relative_to(folder):return False
        result=read(folder/FOCUS_HANDOFF_FILES[0]);recovery=read(folder/FOCUS_HANDOFF_FILES[1])
        frame=read(folder/FOCUS_HANDOFF_FILES[3]);batch=read(folder/'batch.json')
        z=result['z_um'];at=frame['acquired_at'];actual=frame['position']['z']
        confirmation=recovery['confirmation']['z_um']
        finished=datetime.fromisoformat(status['finished_at'].replace('Z','+00:00')).timestamp()
        if not all(type(v) in (int,float) and math.isfinite(v) for v in (z,actual,confirmation,at)):return False
        return (result.get('success') is True and result.get('outcome')=='resolved_peak'
            and recovery.get('state')=='focused' and recovery.get('result',{}).get('success') is True
            and abs(actual-z)<=.25 and abs(confirmation-z)<=.25 and 0<at<=finished
            and batch.get('state')=='failed' and batch.get('phase')=='stopped'
            and batch.get('current_chip') is None and batch.get('error')==FOCUS_HANDOFF_ERROR)
    except (OSError,ValueError,KeyError,TypeError):return False


def _preview_persistence_failure(folder,status):
    """Diagnosed post-acquisition thumbnail copy failure, with originals intact.

    This does not admit arbitrary missing files. The missing destination must
    be the vendor thumbnail belonging to a durable, completed/unloaded capture
    receipt. Both its review JPG and the original vendor files are hash-pinned.
    No vendor acknowledgement, cleanup, or hardware operation is performed.
    """
    if not (status.get('state')=='failed' and status.get('error_type')=='FileNotFoundError'
            and status.get('watchdog_reason') is None and status.get('stop_errors')==[]
            and not any(status.get(k) for k in ('cleanup_error','stop_error','requires_attention'))):return None
    prefix='[Errno 2] No such file or directory: '
    if not str(status.get('error','')).startswith(prefix) or (folder/'watchdog.json').exists():return None
    try:
        literal=ast.literal_eval(status['error'][len(prefix):])
        if not isinstance(literal,str):return None
        destination=Path(literal)
        if not destination.is_absolute():return None
        destination=destination.resolve()
        if not destination.is_relative_to(folder/'discovery') or destination.suffix.lower()!='.png':return None
        capture_id=destination.parent.name.removesuffix('.vendor-previews')
        if not re.fullmatch(r'[a-f0-9]{32}',capture_id) or destination.parent.name!=capture_id+'.vendor-previews':return None
        observations=destination.parent.parent
        if observations.name!='observations':return None
        receipt_path=observations/(capture_id+'.json');jpg=observations/(capture_id+'.jpg')
        if receipt_path.stat().st_size>2*1024**2 or not receipt_path.resolve().is_relative_to(folder):return None
        receipt=read(receipt_path);capture=receipt['capture'];preview=receipt['preview']
        if not (receipt.get('schema')==1 and receipt.get('kind')=='persisted_vendor_observation'
                and capture.get('capture_id')==capture_id and capture.get('job_completed') is True
                and capture.get('job_unloaded') is True and receipt['metadata']['zen_vendor_result']==capture
                and re.fullmatch(r'[a-f0-9]{64}',str(capture.get('raw_rgb_sha256','')))):return None
        if (Path(preview['path']).resolve()!=jpg.resolve() or not jpg.resolve().is_relative_to(folder)
                or jpg.stat().st_size!=preview['bytes'] or preview['bytes']<=0 or digest(jpg)!=preview['sha256']):return None
        finished=datetime.fromisoformat(status['finished_at'].replace('Z','+00:00')).timestamp()
        started=datetime.fromisoformat(status['started_at'].replace('Z','+00:00')).timestamp()
        if not all(type(v) in (int,float) and math.isfinite(v) for v in (receipt['acquired_at'],receipt['created_at'])):return None
        if not started<=receipt['acquired_at']<=receipt['created_at']<=finished:return None
        config=read(folder/'scan-config.json');batch=read(folder/'batch.json')
        if not (batch.get('state')=='failed' and batch.get('phase')=='stopped'
                and batch.get('current_chip') is None and batch.get('error')==status['error']):return None
        project=folder.parent.parent;output=Path(config['zen']['job_output_dir']).resolve(strict=True)
        vendor=Path(capture['result_folder']).resolve(strict=True)
        if not output.is_relative_to(project/'data') or vendor!=output/capture_id:return None
        originals=sorted(vendor.glob('*.czi'))
        if len(originals)!=1 or destination.name!=originals[0].name+'.thumb.png':return None
        thumbnail=vendor/destination.name
        pinned=[]
        for path in [originals[0],thumbnail]:
            if path.resolve(strict=True)!=path.absolute() or not path.is_file() or path.stat().st_size<=0:return None
            pinned.append({'path':path.relative_to(project).as_posix(),'bytes':path.stat().st_size,'sha256':digest(path)})
        return {'capture_id':capture_id,'failed_destination':destination.relative_to(folder).as_posix(),
            'receipt':receipt_path.relative_to(folder).as_posix(),'receipt_sha256':digest(receipt_path),
            'preview':jpg.relative_to(folder).as_posix(),'preview_sha256':preview['sha256'],
            'original_vendor_files':pinned,'job_completed':True,'job_unloaded':True,
            'qualification':'Post-acquisition vendor-thumbnail persistence failure; original vendor data remains intact. No deletion or retry was performed.'}
    except (OSError,ValueError,KeyError,TypeError,SyntaxError,AttributeError):return None


def _eligible(folder,process_checker):
    status=read(folder/'scan-status.json');worker=read(folder/'worker-start.json')
    resource_cancel=_resource_cancellation(folder,status)
    focus_handoff=_focused_handoff_failure(folder,status)
    preview_failure=_preview_persistence_failure(folder,status)
    if (status.get('state')!='stopped' and not (resource_cancel or focus_handoff or preview_failure)) or status.get('hardware_closed') is not True:
        raise SafetyError('Continuation requires a cooperative stop and confirmed hardware closure.')
    if any(status.get(key) for key in ('cleanup_error','stop_error','stop_errors','requires_attention')):
        raise SafetyError('Prior cleanup/STOP error needs review; no continuation.')
    reason=status.get('error') or ''
    allowed=reason.startswith(('STOP requested:','External STOP:','Sparse edge STOP requested.','Focus recovery STOP requested.'))
    allowed|=reason in ('Absolute acquisition deadline reached.','MemoryError: Available RAM is below the 2 GiB acquisition floor.')
    allowed=(allowed and status.get('error_type')=='Cancelled') or resource_cancel or focus_handoff or bool(preview_failure)
    # The campaign normally handles run-local STOP itself and returns stopped,
    # so the outer worker can correctly have no exception. Require its exact
    # recorded stop path and an explicit non-fault supervisor quality hold.
    if not reason and status.get('error_type') is None and (folder/'STOP').is_file():
        batch=read(folder/'batch.json')
        try:stop=read(folder/'STOP')
        except (ValueError,UnicodeError):stop={}
        supervisor=(stop.get('issued_by')=='assistant_supervisor' and stop.get('hardware_fault') is False
            and stop.get('clear_stop_automatically') is False and str(stop.get('reason','')).startswith('Supervisor cooperative stop:'))
        operator=stop.get('reason')=='Operator pressed Stop'
        allowed=(batch.get('state')=='stopped' and batch.get('current_chip') is None
            and batch.get('error')=='STOP requested: '+str(folder/'STOP') and (supervisor or operator))
    if not allowed:raise SafetyError('Prior stop reason is not a recognized cooperative stop; no ambiguous-error restart.')
    if worker.get('pid')!=status.get('pid') or worker.get('id')!=folder.name or status.get('id')!=folder.name:
        raise SafetyError('Prior worker/status identities differ.')
    if worker.get('request_sha256')!=digest(folder/'scan-request.json') or status.get('request_sha256')!=worker['request_sha256']:
        raise SafetyError('Prior request identity changed.')
    if process_checker(status['pid']):raise SafetyError('Prior acquisition worker has not exited.')
    launch=read(folder/'launch.json') if (folder/'launch.json').is_file() else None
    if launch and launch.get('pid')!=status['pid'] and process_checker(launch.get('pid')):
        raise SafetyError('Prior acquisition launcher has not exited.')
    return status


def continuation_settings(project,contract):
    """Preserve sample/XY settings and the actual fixed optics from the prior run."""
    folder=_run(project,contract['prior_run_id']);settings=dict(read(folder/'scan-request.json')['settings'])
    optics=contract['last_capture']['optics'];origin=contract['reference_origin_um'];bounds=contract['search_bounds_um']
    settings.update(width_mm=(bounds[1]-bounds[0])/1000,height_mm=(bounds[3]-bounds[2])/1000,
        exposure_ms=optics['camera_resources']['ff_exposure_ms'],light_percent=optics['illumination_percent'],auto_exposure=False)
    settings['focus_travel_policy']=contract.get('focus_travel_policy','explicit')
    if settings['focus_travel_policy']=='explicit':
        settings.update(z_below_um=origin['z']-contract['limits']['z_min'],
            z_above_um=contract['limits']['z_max']-origin['z'])
    # Controller travel is resolved from leased hardware, not represented by
    # the inactive explicit-window form fields (which have their own UI range).
    return settings


def latest_saved_capture(folder):
    """Use captured timestamps/physical XYZ, including durable AF observations.

    Filesystem mtime cannot establish acquisition order. Identical latest
    timestamps with conflicting physical positions are rejected.
    """
    captures=[]
    for path in folder.rglob('*.json'):
        if path.stat().st_size>2*1024**2:continue
        value=read(path)
        if not isinstance(value,dict):continue
        if not all(key in value for key in ('position','pixel_size_um','metadata','acquired_at','sequence')):continue
        at=value['acquired_at'];metadata=value['metadata']
        if not isinstance(metadata,dict) or isinstance(at,bool) or not isinstance(at,(int,float)) or not math.isfinite(at) or at<=0:continue
        physical=metadata.get('physical_stage_position',value['position'])
        if not isinstance(physical,dict) or any(not isinstance(physical.get(a),(int,float)) or not math.isfinite(physical[a]) for a in 'xyz'):
            raise SafetyError('Saved capture lacks finite physical stage XYZ.')
        if not path.resolve().is_relative_to(folder):raise SafetyError('Capture evidence redirects outside prior run.')
        captures.append({'path':path.relative_to(folder).as_posix(),'at':float(at),'position':{a:float(physical[a]) for a in 'xyz'},
            'optics':{k:metadata.get(k) for k in ('objective_position','objective','camera_job_template','camera_resources','white_level','illumination_percent')},
            'pixel_size_um':value['pixel_size_um'],'sha256':digest(path)})
    if not captures:raise SafetyError('No durable last captured position; cannot prove placement continuity.')
    newest=max(r['at'] for r in captures);last=[r for r in captures if r['at']==newest]
    if any(r['position']!=last[0]['position'] or r['optics']!=last[0]['optics'] for r in last):
        raise SafetyError('Conflicting latest capture identities; no placement inference.')
    return sorted(last,key=lambda r:r['path'])[0]


def settled_motion_checkpoint(project,latest,finished,limits,*,cooperative_stop=None):
    """Embed a hash-pinned log snapshot; never change the log or prior images.

    Shared logs have no run ID. A continuous sequence from the captured XYZ,
    inside the prior envelope and ending before its confirmed closure, is
    therefore required. Any later movement invalidates this recovery plan.
    """
    path=Path(project).resolve()/'data/motion.jsonl'
    if not path.is_file() or path.resolve()!=path.absolute():raise SafetyError('Resource recovery requires the original motion log.')
    digestor=hashlib.sha256();count=0;events=[];pending=None;retry=None;position=dict(latest['position']);at=latest['at'];last_time=at;cancelled_at_target=False
    def near(a,b):return all(abs(a[k]-b[k])<=(.5 if k!='z' else .25) for k in 'xyz')
    def checked(value):
        if not isinstance(value,dict) or any(type(value.get(k)) not in (int,float) or not math.isfinite(value[k]) for k in 'xyz'):
            raise SafetyError('Motion checkpoint has invalid physical XYZ.')
        Limits(**limits).check(Position(**value));return value
    with path.open('rb') as stream:
        for raw in stream:
            if not raw.endswith(b'\n'):raise SafetyError('Motion log ends with an incomplete record.')
            digestor.update(raw);count+=len(raw)
            try:event=json.loads(raw);stamp=event['time']
            except (ValueError,KeyError,TypeError):raise SafetyError('Motion log has an unreadable record.')
            if type(stamp) not in (int,float) or not math.isfinite(stamp):raise SafetyError('Motion timestamp is invalid.')
            if stamp<=latest['at']:continue
            if stamp>finished:raise SafetyError('Motion was logged after prior hardware closure; no unchanged-placement recovery.')
            if stamp<last_time:raise SafetyError('Motion checkpoint timestamps are out of order.')
            last_time=stamp;kind=event.get('event');observed=checked(event.get('position'));target=checked(event.get('target'))
            if cancelled_at_target:raise SafetyError('Motion was logged after the cooperative stop checkpoint.')
            if kind=='failed':
                stop=cooperative_stop or {}
                if not (pending is not None and retry is None and near(observed,target) and near(target,pending['target'])
                        and event.get('error')==stop.get('error') and type(stop.get('at')) in (int,float)
                        and pending['time']<=stop['at']<=stamp):
                    raise SafetyError('Failed motion after last capture prevents resource recovery.')
                # STOP arrived after the commanded target was read back, before
                # the ordinary settle event. Require fresh stationary readbacks
                # in the next sole worker; never treat arbitrary failure as this.
                position=dict(observed);at=stamp;pending=None;cancelled_at_target=True
            elif kind=='request':
                if pending is not None or not near(observed,position):raise SafetyError('Motion chain is unfinished or disconnected from the last capture.')
                pending=event
            elif kind=='settled':
                if pending is None or retry is not None or not near(target,pending['target']) or not near(observed,target) or event.get('error'):
                    raise SafetyError('Settled motion does not close its matching request.')
                position=dict(observed);at=stamp;pending=None
            elif kind=='vendor_position_rejected':
                # A recovered rejection is an intermediate event, not a new
                # move or proof of arrival. Require the exact stationary XY
                # retry evidence and later successful response + settlement.
                axis=event.get('axis');elapsed=event.get('rpc_elapsed_s')
                if (pending is None or retry is not None or axis not in ('x','y') or event.get('attempt')!=1
                        or type(elapsed) not in (int,float) or not math.isfinite(elapsed) or not 0<=elapsed<=1.
                        or not isinstance(event.get('error'),str) or not event['error']
                        or abs(observed[axis]-pending['position'][axis])>=.05
                        or abs(observed['z']-pending['position']['z'])>=.05
                        or target[axis]!=pending['target'][axis]
                        or any(abs(target[a]-observed[a])>=.05 for a in 'xyz' if a!=axis)):
                    raise SafetyError('Rejected XY motion lacks a stationary recovery chain.')
                retry={'rejection':event,'issued':False}
            elif kind=='stationary_xy_retry':
                samples=event.get('stationary_readbacks')
                rejection=retry['rejection'] if retry else {}
                if (pending is None or retry is None or retry['issued'] or event.get('attempt')!=2
                        or event.get('axis')!=rejection.get('axis') or target!=rejection.get('target')
                        or event.get('error')!=rejection.get('error')
                        or event.get('rpc_elapsed_s')!=rejection.get('rpc_elapsed_s')
                        or not isinstance(samples,list) or len(samples)!=5):
                    raise SafetyError('Stationary XY retry does not match its rejection.')
                for sample in [observed,*samples]:
                    sample=checked(sample)
                    if any(abs(sample[a]-rejection['position'][a])>=.05 for a in 'xyz'):
                        raise SafetyError('XY retry readbacks were not stationary.')
                retry['issued']=True
            elif kind in ('vendor_motion_response','vendor_tolerance_warning'):
                if pending is None:raise SafetyError('Motion response has no matching request.')
                if kind=='vendor_motion_response' and event.get('error'):raise SafetyError('Motion response reports an error.')
                if retry is not None:
                    rejection=retry['rejection']
                    if (not retry['issued'] or event.get('attempt')!=2 or event.get('axis')!=rejection['axis']
                            or target!=rejection['target']):
                        raise SafetyError('XY retry lacks its matching successful response.')
                    if kind=='vendor_tolerance_warning' and not near(observed,target):
                        raise SafetyError('XY retry tolerance warning did not reach its target.')
                    retry=None
            else:raise SafetyError('Unknown motion event prevents resource recovery.')
            events.append(event)
            if len(events)>2048:raise SafetyError('Motion checkpoint exceeds its bounded evidence size.')
    if pending is not None:raise SafetyError('Unfinished motion after last capture prevents resource recovery.')
    return {'kind':'cooperative_stop_at_target' if cancelled_at_target else 'settled_motion' if events else 'last_capture_with_no_later_motion','position':position,'at':at,
        'source_log':'data/motion.jsonl','prefix_bytes':count,'prefix_sha256':digestor.hexdigest(),'events':events,
        'after_capture_sha256':latest['sha256'],'closure_at':finished}


def build_contract(project,prior_run_id,z_min_um,*,z_max_um=None,process_checker=None,focus_travel_policy='explicit'):
    folder=_run(project,prior_run_id);status=_eligible(folder,process_checker or process_alive_strict)
    request=read(folder/'scan-request.json');config=read(folder/'scan-config.json');batch=read(folder/'batch.json')
    validation=read(folder/'reference-validation.json');profile=read(folder/'camera-profile-used.json')
    if validation.get('accepted') is not True or not batch.get('coordinate_frame_id'):
        raise SafetyError('Prior reference/frame was not verified by this campaign.')
    bounds=batch['search_bounds_um'];origin=batch['reference_position_um'];limits=config['limits']
    if len(bounds)!=4 or not all(math.isfinite(v) for v in bounds) or not bounds[0]<bounds[1] or not bounds[2]<bounds[3]:
        raise SafetyError('Prior absolute XY bounds are invalid.')
    if limits['x_min']!=bounds[0]-30. or limits['x_max']!=bounds[1] or limits['y_min']!=bounds[2]-30. or limits['y_max']!=bounds[3]:
        raise SafetyError('Prior limits do not match its recorded search envelope/backlash margin.')
    high=limits['z_max'] if z_max_um is None else z_max_um
    if not all(isinstance(v,(int,float)) and not isinstance(v,bool) and math.isfinite(v) for v in (z_min_um,high)):
        raise ValueError('Explicit finite absolute Z bounds required.')
    resource_cancel=_resource_cancellation(folder,status)
    focus_handoff=_focused_handoff_failure(folder,status)
    preview_failure=_preview_persistence_failure(folder,status)
    same_resource_interval=(resource_cancel or focus_handoff or preview_failure) and z_min_um==limits['z_min']
    if focus_travel_policy not in ('explicit','controller'):raise ValueError('Unknown focus travel policy.')
    controller_policy=focus_travel_policy=='controller'
    if controller_policy and (z_min_um!=limits['z_min'] or high!=limits['z_max']):
        raise SafetyError('Controller policy retains the prior checkpoint envelope; live controller range is read before arming.')
    if not controller_policy and (high!=limits['z_max'] or not (origin['z']-1000<=z_min_um<limits['z_min'] or same_resource_interval)):
        raise SafetyError('This continuation permits only an explicit lower-Z extension up to1000um from the original origin; upper Z is unchanged.')
    reference=folder/'calibration/substrate.tiff';sidecar=reference.with_suffix('.json');ref=read(sidecar)
    if validation['raw_sha256']!=digest(reference) or validation['metadata_sha256']!=digest(sidecar) or ref['position']!=origin:
        raise SafetyError('Prior verified reference hash or origin changed.')
    if config['camera_transform']!=profile['camera_transform']:
        raise SafetyError('Prior measured camera transform changed.')
    latest=latest_saved_capture(folder);optics=latest['optics'];reference_optics={k:ref['metadata'].get(k) for k in optics}
    if optics!=reference_optics:raise SafetyError('Last captured optics differ from the verified reference.')
    finished=datetime.fromisoformat(status['finished_at'].replace('Z','+00:00')).timestamp()
    if latest['at']>finished+1:raise SafetyError('Capture evidence was acquired after confirmed closure.')
    cooperative_stop=None
    if controller_policy and (folder/'STOP').is_file():
        stop=read(folder/'STOP')
        if stop.get('issued_by')=='assistant_supervisor' and stop.get('hardware_fault') is False:
            cooperative_stop={'error':'STOP requested: '+str(folder/'STOP'),'at':stop.get('at')}
    checkpoint=settled_motion_checkpoint(project,latest,finished,limits,cooperative_stop=cooperative_stop) if resource_cancel or focus_handoff or preview_failure or controller_policy else None
    started=datetime.fromisoformat(status['started_at'].replace('Z','+00:00')).timestamp()
    deadline=request.get('continuation',{}).get('deadline_at',started+request['settings']['hours']*3600)
    if not math.isfinite(deadline) or deadline<=time.time():raise SafetyError('Original acquisition deadline has passed; a Z-only continuation does not extend time authorization.')
    updated_limits={**limits,'z_min':float(z_min_um)};Limits(**updated_limits).check(Position(**latest['position']))
    Limits(**updated_limits).check(Position(**origin))
    files=['scan-request.json','scan-status.json','worker-start.json','scan-config.json','batch.json',
        'reference-validation.json','camera-profile-used.json','calibration/substrate.tiff','calibration/substrate.json',latest['path']]
    files.extend(name for name in ('STOP','launch.json','watchdog.json') if (folder/name).is_file())
    if focus_handoff:files.extend(FOCUS_HANDOFF_FILES)
    if preview_failure:
        files.extend([preview_failure['receipt'],preview_failure['preview']])
        if (folder/'initial-focus-reused.json').is_file():files.append('initial-focus-reused.json')
    result={'schema':1,'version':VERSION,'prior_run_id':prior_run_id,'prior_coordinate_frame_id':batch['coordinate_frame_id'],
        'search_bounds_um':bounds,'reference_origin_um':origin,'limits':updated_limits,'last_capture':latest,
        'prior_files_sha256':{name:digest(folder/name) for name in dict.fromkeys(files)},
        'prior_stop_preserved':True,'return_to_reference_before_fresh_capture':True,
        'deadline_at':deadline,'deadline_policy':'Original absolute worker deadline remains an upper bound; no implicit time extension.',
        'placement_assertion':'Same specimen placement and coordinate origin; current physical checkpoint must still match.',
        'xy_tolerance_um':.5,'z_tolerance_um':.25,'lamp_tolerance_percent':.05}
    if resource_cancel:result.update(continuation_kind='diagnosed_ram_watchdog_recovery',motion_checkpoint=checkpoint)
    if controller_policy:result.update(focus_travel_policy='controller',motion_checkpoint=checkpoint,
        focus_travel_policy_note='Prior Z envelope validates the stationary checkpoint only. Read and preserve live controller limits before arming; no fixed search window about the starting Z.')
    if preview_failure:result.update(continuation_kind='diagnosed_vendor_preview_persistence_recovery',
        motion_checkpoint=checkpoint,preview_persistence_evidence=preview_failure)
    if focus_handoff:result.update(continuation_kind='confirmed_focus_handoff_recovery',motion_checkpoint=checkpoint,
        initial_focus_reuse={'result':FOCUS_HANDOFF_FILES[0],'frame':FOCUS_HANDOFF_FILES[2],
            'position':read(folder/FOCUS_HANDOFF_FILES[3])['position'],'qualification':'Confirmed edge focus in the unchanged prior optical profile; no chip-plane inference.'})
    return result


def verify_contract(project,contract,*,process_checker=None):
    if contract.get('schema')!=1 or contract.get('version')!=VERSION:raise SafetyError('Unsupported continuation contract.')
    folder=_run(project,contract['prior_run_id'])
    for name,expected in contract['prior_files_sha256'].items():
        path=(folder/name).resolve(strict=True)
        if not path.is_relative_to(folder) or digest(path)!=expected:raise SafetyError('Pinned prior evidence changed: '+name)
    current=build_contract(project,contract['prior_run_id'],contract['limits']['z_min'],
        z_max_um=contract['limits']['z_max'],process_checker=process_checker,
        focus_travel_policy=contract.get('focus_travel_policy','explicit'))
    if current!=contract:raise SafetyError('Continuation contract differs from independently revalidated evidence.')
    return read(folder/'scan-config.json'),read(folder/'camera-profile-used.json')


def verify_stationary_checkpoint(scope,position,profile,config,contract):
    """Read-only checks on the caller-owned leased scope, before arming motion."""
    last=contract['last_capture'];checkpoint=contract.get('motion_checkpoint');expected=checkpoint['position'] if checkpoint else last['position'];optics=last['optics']
    if any(abs(getattr(position,a)-expected[a])>contract['xy_tolerance_um'] for a in 'xy'):
        raise SafetyError('Current XY does not match the durable physical checkpoint; coordinate continuity is unproved.')
    if checkpoint and checkpoint['kind']=='cooperative_stop_at_target':
        for _ in range(5):
            scope.check_cancel();sample=scope.position()
            if any(abs(getattr(sample,a)-expected[a])>(contract['z_tolerance_um'] if a=='z' else contract['xy_tolerance_um']) for a in 'xyz'):
                raise SafetyError('Cooperative stop target has not remained stationary; no continuation motion.')
            time.sleep(.1)
    # Cooperative STOP can finish a commanded Z move after its last image.
    # Read actual Z; retain the prior armed envelope for this initial check.
    # Z displacement alone is not evidence of an XY re-zero or specimen move.
    prior_config=read(_run(Path(config['zen']['job_output_dir']).parent.parent,contract['prior_run_id'])/'scan-config.json')
    previous_limits=prior_config['limits']
    if not previous_limits['z_min']<=position.z<=previous_limits['z_max']:
        raise SafetyError('Current Z is outside the previous controller-checked acquisition interval.')
    # CameraTransform normalizes pixel_size_um to a tuple; JSON uses lists.
    from dataclasses import asdict
    from .model import CameraTransform
    if asdict(CameraTransform(**config['camera_transform']))!=asdict(CameraTransform(**profile['camera_transform'])) or config['zen']['camera_resources']!=optics['camera_resources']:
        raise SafetyError('Configured camera calibration/resources differ from prior fixed settings.')
    if config['zen']['job_template']!=optics['camera_job_template'] or config['zen']['camera_white_level']!=optics['white_level']:
        raise SafetyError('Configured camera job/white level changed.')
    controls=scope.hardware_controls();state=controls.objectives()
    controller_range=controls.focus_travel_range() if hasattr(controls,'focus_travel_range') else None
    if controller_range and not controller_range['min_um']<=contract['limits']['z_min']<contract['limits']['z_max']<=controller_range['max_um']:
        raise SafetyError('Requested focus search exceeds the controller-reported travel range.')
    selected=next((r for r in state['objectives'] if r['position']==state['position']),None)
    if state['position']!=optics['objective_position'] or selected!=optics['objective']:
        raise SafetyError('Manual objective differs from last captured optical profile.')
    lamp=controls.lamp_percent()
    if not math.isfinite(lamp) or abs(lamp-optics['illumination_percent'])>contract['lamp_tolerance_percent']:
        raise SafetyError('Illumination differs from the last captured optical profile.')
    return {'position_um':{a:getattr(position,a) for a in 'xyz'},'objective':selected,'lamp_percent':lamp,
        'controller_focus_range':controller_range,'last_capture_z_delta_um':position.z-last['position']['z'],
        'physical_checkpoint_kind':checkpoint['kind'] if checkpoint else 'last_capture','physical_checkpoint_position_um':expected,
        'verified_before_arm':True,'camera_settings_verification':'Pinned camera-only job resources; fresh frame checked again after return to reference.'}
