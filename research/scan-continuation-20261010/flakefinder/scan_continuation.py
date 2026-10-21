"""Explicit same-placement continuation contracts; no hardware construction.

The prior run is immutable. A new run keeps its original absolute XY envelope,
returns to its verified reference only after a leased position/optics check,
and performs the ordinary fresh-reference acquisition again.
"""
from datetime import datetime
from pathlib import Path
import ctypes
import hashlib
import json
import math
import os
import re
import time

from .model import Limits,Position,SafetyError

VERSION='same-placement-lower-z-extension-v1'


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


def _eligible(folder,process_checker):
    status=read(folder/'scan-status.json');worker=read(folder/'worker-start.json')
    if status.get('state')!='stopped' or status.get('hardware_closed') is not True:
        raise SafetyError('Continuation requires a cooperative stop and confirmed hardware closure.')
    if any(status.get(key) for key in ('cleanup_error','stop_error','stop_errors','requires_attention')):
        raise SafetyError('Prior cleanup/STOP error needs review; no continuation.')
    reason=status.get('error') or ''
    allowed=reason.startswith(('STOP requested:','External STOP:','Sparse edge STOP requested.','Focus recovery STOP requested.'))
    allowed|=reason in ('Absolute acquisition deadline reached.','MemoryError: Available RAM is below the 2 GiB acquisition floor.')
    allowed=allowed and status.get('error_type')=='Cancelled'
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
        z_below_um=origin['z']-contract['limits']['z_min'],z_above_um=contract['limits']['z_max']-origin['z'],
        exposure_ms=optics['camera_resources']['ff_exposure_ms'],light_percent=optics['illumination_percent'],auto_exposure=False)
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


def build_contract(project,prior_run_id,z_min_um,*,z_max_um=None,process_checker=None):
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
    if high!=limits['z_max'] or not origin['z']-1000<=z_min_um<limits['z_min']:
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
    started=datetime.fromisoformat(status['started_at'].replace('Z','+00:00')).timestamp()
    deadline=request.get('continuation',{}).get('deadline_at',started+request['settings']['hours']*3600)
    if not math.isfinite(deadline) or deadline<=time.time():raise SafetyError('Original acquisition deadline has passed; a Z-only continuation does not extend time authorization.')
    updated_limits={**limits,'z_min':float(z_min_um)};Limits(**updated_limits).check(Position(**latest['position']))
    Limits(**updated_limits).check(Position(**origin))
    files=['scan-request.json','scan-status.json','worker-start.json','scan-config.json','batch.json',
        'reference-validation.json','camera-profile-used.json','calibration/substrate.tiff','calibration/substrate.json',latest['path']]
    files.extend(name for name in ('STOP','launch.json') if (folder/name).is_file())
    return {'schema':1,'version':VERSION,'prior_run_id':prior_run_id,'prior_coordinate_frame_id':batch['coordinate_frame_id'],
        'search_bounds_um':bounds,'reference_origin_um':origin,'limits':updated_limits,'last_capture':latest,
        'prior_files_sha256':{name:digest(folder/name) for name in dict.fromkeys(files)},
        'prior_stop_preserved':True,'return_to_reference_before_fresh_capture':True,
        'deadline_at':deadline,'deadline_policy':'Original absolute worker deadline remains an upper bound; no implicit time extension.',
        'placement_assertion':'Same specimen placement and coordinate origin; current physical checkpoint must still match.',
        'xy_tolerance_um':.5,'z_tolerance_um':.25,'lamp_tolerance_percent':.05}


def verify_contract(project,contract,*,process_checker=None):
    if contract.get('schema')!=1 or contract.get('version')!=VERSION:raise SafetyError('Unsupported continuation contract.')
    folder=_run(project,contract['prior_run_id'])
    for name,expected in contract['prior_files_sha256'].items():
        path=(folder/name).resolve(strict=True)
        if not path.is_relative_to(folder) or digest(path)!=expected:raise SafetyError('Pinned prior evidence changed: '+name)
    current=build_contract(project,contract['prior_run_id'],contract['limits']['z_min'],
        z_max_um=contract['limits']['z_max'],process_checker=process_checker)
    if current!=contract:raise SafetyError('Continuation contract differs from independently revalidated evidence.')
    return read(folder/'scan-config.json'),read(folder/'camera-profile-used.json')


def verify_stationary_checkpoint(scope,position,profile,config,contract):
    """Read-only checks on the caller-owned leased scope, before arming motion."""
    last=contract['last_capture'];expected=last['position'];optics=last['optics']
    if any(abs(getattr(position,a)-expected[a])>contract['xy_tolerance_um'] for a in 'xy') or abs(position.z-expected['z'])>contract['z_tolerance_um']:
        raise SafetyError('Current XYZ does not match the last durable physical capture; placement continuity is unproved.')
    # CameraTransform normalizes pixel_size_um to a tuple; JSON uses lists.
    from dataclasses import asdict
    from .model import CameraTransform
    if asdict(CameraTransform(**config['camera_transform']))!=asdict(CameraTransform(**profile['camera_transform'])) or config['zen']['camera_resources']!=optics['camera_resources']:
        raise SafetyError('Configured camera calibration/resources differ from prior fixed settings.')
    if config['zen']['job_template']!=optics['camera_job_template'] or config['zen']['camera_white_level']!=optics['white_level']:
        raise SafetyError('Configured camera job/white level changed.')
    controls=scope.hardware_controls();state=controls.objectives()
    selected=next((r for r in state['objectives'] if r['position']==state['position']),None)
    if state['position']!=optics['objective_position'] or selected!=optics['objective']:
        raise SafetyError('Manual objective differs from last captured optical profile.')
    lamp=controls.lamp_percent()
    if not math.isfinite(lamp) or abs(lamp-optics['illumination_percent'])>contract['lamp_tolerance_percent']:
        raise SafetyError('Illumination differs from the last captured optical profile.')
    return {'position_um':{a:getattr(position,a) for a in 'xyz'},'objective':selected,'lamp_percent':lamp,
        'verified_before_arm':True,'camera_settings_verification':'Pinned camera-only job resources; fresh frame checked again after return to reference.'}
