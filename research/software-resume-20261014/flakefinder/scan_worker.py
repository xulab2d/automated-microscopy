"""One explicitly started, fresh-coordinate scan campaign. Importing is offline.

The server owns request/authorization creation. This worker never lifts an
operator hold, resumes historical positions, changes objectives, or restarts.
"""
from __future__ import annotations

from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path
import argparse
import hashlib
import json
import math
import os
import re
import shutil
import threading
import time
import traceback
import uuid

for _name in ('OMP_NUM_THREADS','OPENBLAS_NUM_THREADS','MKL_NUM_THREADS','NUMEXPR_NUM_THREADS'):
    os.environ.setdefault(_name,'1')

from .model import AcquisitionError, Cancelled, Limits, SafetyError, atomic_json

DISK_FLOOR = 10 * 1024**3 + 512 * 1024**2
RAM_FLOOR = 80*1024**2  # Legacy reader capacity fallback; no physical floor on Windows.
WORKSPACE = Path(__file__).resolve().parents[1]


def utc_now():
    return datetime.now(timezone.utc).isoformat().replace('+00:00', 'Z')


def file_hash(path):
    value = hashlib.sha256()
    with Path(path).open('rb') as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b''):
            value.update(chunk)
    return value.hexdigest()


def read_json(path):
    return json.loads(Path(path).read_text(encoding='utf-8-sig'))


def _exclusive_json(path, value):
    with Path(path).open('x', encoding='utf-8') as stream:
        json.dump(value, stream, indent=2, allow_nan=False)
        stream.flush()
        os.fsync(stream.fileno())


def load_request(project, run_id):
    project = Path(project).resolve(strict=True)
    if not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_-]{0,95}', run_id):
        raise ValueError('Run ID must be a single simple directory name.')
    run = (project / 'data' / run_id).resolve(strict=True)
    if run.parent != (project / 'data').resolve() or not run.is_relative_to(project):
        raise SafetyError('Run directory escapes this project.')
    path = run / 'scan-request.json'
    if path.resolve(strict=True).parent != run:
        raise SafetyError('Request redirects outside its run.')
    if path.stat().st_size>2*1024**2:raise ValueError('Oversized scan request.')
    request = read_json(path)
    if request.get('schema') != 1 or request.get('id') != run_id or not request.get('source_release') or not request.get('created_at'):
        raise ValueError('Request identity, source release and creation time are required.')
    settings = request.get('settings', {})
    if not isinstance(settings.get('sample_id'), str) or not settings['sample_id'].strip():
        raise ValueError('A fresh sample ID is required.')
    if settings.get('material') not in ('graphene', 'hBN', 'MoTe2'):
        raise ValueError('Unsupported material setting.')
    positive = ('oxide_nm', 'width_mm', 'height_mm', 'z_below_um', 'z_above_um',
                'min_chip_mm', 'hours', 'exposure_ms', 'light_percent')
    for key in positive:
        value = settings.get(key)
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value <= 0:
            raise ValueError('Finite positive setting required: ' + key)
    for key in ('max_chips', 'max_tiles'):
        value = settings.get(key)
        if type(value) is not int or value < 1:
            raise ValueError('Positive integer setting required: ' + key)
    if settings['light_percent'] > 100 or settings['exposure_ms'] > 10000 or settings['hours'] > 24:
        raise ValueError('Light must be <=100%, exposure <=10000 ms, duration <=24 hours.')
    if min(settings['width_mm'], settings['height_mm'], settings['min_chip_mm']) <= .04:
        raise ValueError('Search and minimum chip size must exceed the 40 µm seed patch.')
    if settings.get('dark_holder') is not True or settings.get('operator_ready') is not True:
        raise SafetyError('This worker requires the explicit dark-holder and fresh specimen-ready declarations.')
    if type(settings.get('auto_exposure',True)) is not bool:
        raise ValueError('auto_exposure must be boolean.')
    if settings.get('focus_travel_policy','explicit') not in ('controller','explicit'):
        raise ValueError('Unknown focus travel policy.')
    if any((run / name).exists() for name in ('worker-start.json', 'batch.json', 'layout.json')):
        raise SafetyError('This run has already started; the worker never resumes or restarts it.')
    status = read_json(run / 'scan-status.json') if (run / 'scan-status.json').exists() else {}
    if status.get('state', 'draft') not in ('draft', 'queued', 'starting'):
        raise SafetyError('Run status is not a new draft/queued start.')
    return project, run, request, file_hash(path)


def load_camera_profile():
    station_path=WORKSPACE/'configs'/'microscope-station.json'
    station=read_json(station_path)
    if station.get('schema')!=1 or not station.get('id'):
        raise SafetyError('A versioned installed microscope station configuration is required.')
    connection={key:station[key] for key in ('endpoint','bindings_dir','gateway_dir') if key in station}
    if not connection.get('endpoint'):
        raise SafetyError('Microscope station endpoint is missing.')
    for key in ('bindings_dir','gateway_dir'):
        path=Path(connection.get(key,''))
        if not path.is_absolute() or not path.is_dir():
            raise SafetyError('Installed microscope station directory is unavailable: '+key)
        connection[key]=str(path.resolve())
    path=Path(station.get('objective_profile',''))
    if not str(path) or str(path)=='.':raise SafetyError('Station objective profile is missing.')
    if not path.is_absolute():path=station_path.parent/path
    profile=read_json(path)
    profile['connection']=connection
    profile['station_provenance']={'id':station['id'],'path':str(station_path),'sha256':file_hash(station_path),
        'objective_profile_path':str(path.resolve()),'objective_profile_sha256':file_hash(path)}
    return profile


def fresh_config(profile, settings, project, run_id, position=None):
    """Whitelist camera/transform fields; copy no old sample or absolute coordinates."""
    from .model import CameraTransform
    objective = profile.get('objective', {})
    if objective.get('magnification') != 20 or type(objective.get('position')) is not int:
        raise SafetyError('A measured manual 20× objective profile is required.')
    transform = CameraTransform(**profile['camera_transform'])
    if not transform.calibrated:
        raise SafetyError('Camera-to-stage calibration is not measured.')
    balance = profile.get('camera_resources', {}).get('ff_white_balance')
    if not balance or not profile.get('job_template'):
        raise SafetyError('A fixed white-balance setting and camera job are required.')
    coefficients=[float(value) for value in str(balance).split(',')]
    if len(coefficients)!=3 or not all(math.isfinite(value) and 0<value<=10 for value in coefficients):
        raise SafetyError('Invalid measured fixed white-balance coefficients.')
    balance=','.join(format(value,'.12g') for value in coefficients)
    white = float(profile.get('context', {}).get('white_level', 0))
    if not 0 < white <= 65535:
        raise SafetyError('Measured camera white level is missing.')
    config = {'schema': 1, 'backend': 'zen', 'camera_transform': asdict(transform),
        'zen': {'job_template': profile['job_template'], 'job_output_dir': str(project / 'data' / 'zen-captures'),
            'timeout_s': 30, 'settle_s': .5, 'xy_backlash_um': 30.,
            'camera_resources': {'ff_exposure_ms': float(settings['exposure_ms']), 'ff_white_balance': balance},
            'expected_objective_position': objective['position'], 'camera_white_level': white,
            'keep_vendor_results': False, 'processing_overlap': False},
        'context': {'sample_id': settings['sample_id'], 'material': settings['material'],
            'substrate': 'SiO2/Si', 'oxide_nm': float(settings['oxide_nm']), 'run_id': run_id,
            'coordinate_frame': 'Fresh current readback for this run; no historical XY/Z reused.',
            'objective': objective['name'], 'exposure_ms': float(settings['exposure_ms']),
            'white_balance': balance, 'white_level': white, 'illumination_percent': float(settings['light_percent']),
            'color_profile_kind': 'Fixed acquisition gains; effective lamp/camera response not calibrated.'}}
    config['zen'].update({key:value for key,value in profile.get('connection',{}).items()
                          if key in ('endpoint','bindings_dir','gateway_dir')})
    if profile.get('station_provenance'):config['station_provenance']=profile['station_provenance']
    if position is None:
        return config, None
    dx = settings['width_mm'] * 500.;dy = settings['height_mm'] * 500.
    bounds = [position.x-dx, position.x+dx, position.y-dy, position.y+dy]
    config['limits'] = asdict(Limits(bounds[0]-30., bounds[1], bounds[2]-30., bounds[3],
        position.z-settings['z_below_um'], position.z+settings['z_above_um'], 3000., 10.))
    return config, bounds


def resolve_focus_travel(scope,config,settings):
    """Resolve an explicit controller policy on the sole leased worker.

    Never change controller settings or infer a travel range from an old run.
    Historical requests without the policy retain their explicit interval.
    """
    if settings.get('focus_travel_policy','explicit')!='controller':return None
    controls=scope.hardware_controls()
    value=controls.focus_travel_range()
    low,high=value['min_um'],value['max_um']
    if not all(type(v) in (int,float) and math.isfinite(v) for v in (low,high)) or low>=high:
        raise SafetyError('Invalid live controller focus travel range.')
    current=scope.position()
    if not low<current.z<high:raise SafetyError('Current focus is outside the live controller travel range.')
    config['limits'].update(z_min=float(low),z_max=float(high))
    audit={'policy':'controller','controller_range':value,'position_um':asdict(current),
           'artificial_starting_z_window':False,'controller_settings_changed':False,'read_before_arm':True}
    config['focus_travel']=audit
    return audit


def resolve_grid_travel(scope,config,settings,bounds):
    """Intersect a fresh search request with live XY travel, including backlash."""
    if not settings.get('grid_4x4',False):return bounds,None
    value=scope.hardware_controls().stage_travel_range()
    low_x,high_x,low_y,high_y=[value[k] for k in ('x_min_um','x_max_um','y_min_um','y_max_um')]
    if not all(type(v) in (int,float) and math.isfinite(v) for v in (low_x,high_x,low_y,high_y)) or low_x>=high_x or low_y>=high_y:
        raise SafetyError('Invalid live XY controller range.')
    current=scope.position()
    if not low_x<=current.x<=high_x or not low_y<=current.y<=high_y:
        raise SafetyError('ZEN XY readback is outside the reported controller range.')
    if any(not math.isfinite(value.get(axis+'_position_um',float('nan'))) or
           abs(value[axis+'_position_um']-getattr(current,axis))>2. for axis in ('x','y')):
        raise SafetyError('ZEN and controller XY coordinate frames do not agree.')
    approach=float(config['zen']['xy_backlash_um'])
    clipped=[max(bounds[0],low_x+approach+1.),min(bounds[1],high_x-1.),
             max(bounds[2],low_y+approach+1.),min(bounds[3],high_y-1.)]
    if clipped[0]>=clipped[1] or clipped[2]>=clipped[3]:raise SafetyError('No searchable XY area inside controller travel.')
    config['limits'].update(x_min=min(current.x,clipped[0]-approach),x_max=max(current.x,clipped[1]),
                            y_min=min(current.y,clipped[2]-approach),y_max=max(current.y,clipped[3]))
    audit={'controller_range':value,'requested_bounds_um':bounds,'search_bounds_um':clipped,
           'starting_position_um':asdict(current),'controller_settings_changed':False,
           'qualification':'Search extent only; grid translation and chip positions remain unmeasured.'}
    config['stage_travel']=audit
    return clipped,audit


def resource_snapshot(project):
    from .storage import load_policy
    policy = load_policy() or {}
    cache = Path(policy.get('cache_volume', Path(project).anchor))
    volumes = {str(cache): shutil.disk_usage(cache).free,
               str(Path(project).anchor): shutil.disk_usage(project).free}
    from .memory_capacity import snapshot
    memory=snapshot()
    return {'free_disk_bytes': volumes, 'available_ram_bytes': memory['physical_available_bytes'],
            'disk_floor_bytes': DISK_FLOOR, 'ram_floor_bytes': 0,'memory_capacity':memory}


def check_resources(snapshot):
    if min(snapshot['free_disk_bytes'].values()) < DISK_FLOOR:
        raise AcquisitionError('Storage reserve reached: retain 10 GiB plus 512 MiB on output/cache volumes.')
    from .memory_capacity import require,CONTROL_WORKING_BYTES
    # Custom resource readers without commit telemetry conservatively expose
    # their reported availability as capacity; production supplies all fields.
    memory=snapshot.get('memory_capacity') or dict(physical_available_bytes=snapshot['available_ram_bytes'],
        commit_available_bytes=snapshot['available_ram_bytes'],address_available_bytes=snapshot['available_ram_bytes'])
    require(memory,CONTROL_WORKING_BYTES,purpose='acquisition control and checkpointing')


class WorkerWatchdog:
    """One heartbeat/watchdog writer; only the acquisition owner writes status."""
    def __init__(self, run, project, duration_s, hold_check, resource_reader, *, interval_s=5., clock=time.monotonic):
        self.run=run;self.project=project;self.duration_s=duration_s;self.hold_check=hold_check
        self.resource_reader=resource_reader;self.interval_s=interval_s;self.clock=clock
        self.started=clock();self.last_progress=self.started;self.phase='preflight'
        self.done=threading.Event();self.triggered=threading.Event();self.scope=None;self.reason=None
        self.lock=threading.Lock();self.thread=None
        self.memory_low_since=None;self.memory_pressure=None
        from .storage import load_policy
        installation=(load_policy() or {}).get('installation_root')
        paths=[run/'STOP',project/'STOP',WORKSPACE/'STOP']
        if installation:paths.append(Path(installation)/'STOP')
        self.stop_paths=tuple(dict.fromkeys(paths))

    def progress(self, phase):
        with self.lock:self.phase=str(phase)[:100];self.last_progress=self.clock()

    def _failure(self):
        for path in self.stop_paths:
            if path.exists():return 'External STOP: '+str(path)
        self.hold_check()
        now=self.clock()
        with self.lock:phase=self.phase;age=now-self.last_progress
        if now-self.started >= self.duration_s:return 'Absolute acquisition deadline reached.'
        snapshot=self.resource_reader(self.project)
        try:check_resources(snapshot)
        except MemoryError as exc:
            if self.memory_low_since is None:self.memory_low_since=now
            elapsed=now-self.memory_low_since
            self.memory_pressure={'available_bytes':snapshot['available_ram_bytes'],'elapsed_s':elapsed}
            if elapsed>=120:raise MemoryError('RAM reserve remained unavailable for 120 seconds; bounded recovery exhausted.') from exc
        else:
            self.memory_low_since=None;self.memory_pressure=None
        allowance=240. if any(term in phase.lower() for term in ('focus','recovery','boundary','edge')) else 180.
        allowance=min(allowance,600.)
        if age > allowance:return f'No acquisition progress for {age:.1f}s in {phase}; limit {allowance:g}s.'
        return None

    def check(self):
        if self.triggered.is_set():raise Cancelled(self.reason or 'Watchdog STOP.')
        for path in self.stop_paths:
            if path.exists():raise Cancelled('STOP requested: '+str(path))

    def tick(self):
        try:reason=self._failure()
        except Exception as exc:reason=type(exc).__name__+': '+str(exc)
        with self.lock:phase=self.phase;age=self.clock()-self.last_progress
        if reason:
            self.reason=reason;self.triggered.set()
            try:
                with (self.run/'STOP').open('x',encoding='utf-8') as stream:stream.write(reason+'\n')
            except FileExistsError:pass
            except OSError:pass  # In-memory cancellation must survive a full disk.
            try:atomic_json(self.run/'watchdog.json',{'schema':1,'triggered_at':utc_now(),'reason':reason,'phase':phase,'pid':os.getpid(),'automatic_restart':False})
            except OSError:pass
            if self.scope is not None:
                try:self.scope.stop()
                except Exception as exc:self.reason += '; STOP error: '+str(exc)
            return False
        try:atomic_json(self.run/'heartbeat.json',{'schema':1,'pid':os.getpid(),'utc':utc_now(),'at':time.time(),'phase':phase,'progress_age_s':age,
            'deadline_remaining_s':max(0,self.duration_s-(self.clock()-self.started)),
            'memory_pressure':self.memory_pressure})
        except OSError as exc:
            self.reason='Heartbeat persistence failed: '+str(exc);self.triggered.set()
            if self.scope is not None:self.scope.stop()
            return False
        return True

    def start(self):
        def loop():
            while not self.done.is_set():
                if not self.tick():break
                self.done.wait(self.interval_s)
        self.thread=threading.Thread(target=loop,name='scan-resource-watchdog',daemon=True);self.thread.start()

    def finish(self, timeout_s=20.):
        self.done.set()
        if self.thread:self.thread.join(timeout_s)
        return not self.thread or not self.thread.is_alive()


def _bounded_close(scope, timeout_s=20.):
    outcome={}
    def close():
        try:scope.close();outcome['closed']=True
        except BaseException as exc:outcome['error']=type(exc).__name__+': '+str(exc)
    thread=threading.Thread(target=close,name='scan-adapter-close',daemon=True);thread.start();thread.join(timeout_s)
    if thread.is_alive():return {'closed':False,'error':'Adapter cleanup exceeded its bounded wait; lease release is unconfirmed.'}
    return outcome


def validate_reference(frame, config):
    import numpy as np
    from .layout import SubstrateReference, classify_seed_patch, _context
    from .config import transform_from_config
    transform=transform_from_config(config);transform.validate_frame(frame)
    context=_context(frame);expected=config['zen'];objective=context.get('objective') or {}
    if context['objective_position']!=expected['expected_objective_position'] or objective.get('magnification')!=20:
        raise SafetyError('Fresh frame does not show the expected engaged 20× objective.')
    if context['camera_job_template']!=expected['job_template'] or context['camera_resources']!=expected['camera_resources']:
        raise SafetyError('Fresh camera settings differ from the requested fixed profile.')
    if context['white_level']!=expected['camera_white_level'] or context['illumination_percent'] is None or abs(context['illumination_percent']-config['context']['illumination_percent'])>1.:
        raise SafetyError('Fresh white-level/lamp metadata differs from the requested profile.')
    white=context['white_level'];pixels=frame.rgb.reshape(-1,3)
    clipped=float(np.mean(np.any(pixels>=white,axis=1)))
    if clipped>.001 or np.any(np.percentile(pixels,50,axis=0)<white*.03):
        raise AcquisitionError('Reference is clipped or too dark; adjust fixed exposure/light and create a new run.')
    reference=SubstrateReference.from_frame(frame);labels=reference.classify(frame)
    seed=classify_seed_patch(labels,frame.rgb.shape,transform.matrix,40.)
    support=float(np.mean(labels==1))
    # Flakes in the central patch do not invalidate a reference estimated from
    # the full field. Keep this local heuristic diagnostic, not a startup gate.
    if support<.55:
        raise AcquisitionError('Fresh reference lacks coherent full-field substrate support.')
    from .photometric_edges import MEASURED_PROFILE_MODEL
    profile={'schema':1,'profile_id':config['context']['run_id']+'-fresh-manual20x-measured-step',
        'holder_model':MEASURED_PROFILE_MODEL,'manual_objective':True,'objective_position':context['objective_position'],
        'objective_magnification':20,'optical_context':context,
        'reference_pixels_sha256':hashlib.sha256(np.ascontiguousarray(frame.rgb).tobytes()).hexdigest(),
        'qualification':'Exact fresh optics and reference-bound substrate; each boundary requires independent coherent RGB support of a single measured polarity.'}
    from .photometric_edges import assess_edge_profile
    contract=assess_edge_profile(profile,frame)
    if not contract['supported']:raise SafetyError(contract['reason'])
    return profile,{'substrate_color_support_fraction':support,'central_patch':seed,
        'central_patch_policy':'informational_only','clipped_fraction':clipped,
        'maximum_clipped_fraction':.001,'reference_model':reference.to_dict(),'edge_contract':contract,
        'qualification':'Color coherence plus operator-declared bare chip; not independent chemical identification or verified focus.'}


def _capture_reference(scope, config, settings, run, position, check, update):
    """At most three stationary, saved captures; only exposure may adapt."""
    import numpy as np
    from .imaging import save_frame
    from .vendor_lifecycle import acknowledge_raw
    automatic=settings.get('auto_exposure',True);limit=3 if automatic else 1
    exposure=float(settings['exposure_ms']);white=config['zen']['camera_white_level']
    audit={'schema':1,'enabled':automatic,'maximum_attempts':limit,'initial_exposure_ms':exposure,
        'target_q99_fraction':.65,'acceptable_q99_fraction':[.52,.78],'per_step_multiplier_bounds':[.25,4.],
        'white_balance_changed':False,'lamp_changed_during_search':False,'attempts':[],
        'qualification':'Bounded stationary exposure adjustment. Field identity and focus require separate reference validation; no material/layer or true-color calibration.'}
    last_receipt=None
    for index in range(limit):
        check();scope.set_exposure_ms(exposure)
        config['zen']['camera_resources']['ff_exposure_ms']=exposure;config['context']['exposure_ms']=exposure
        update('reference_exposure',exposure_attempt=index+1,exposure_ms=exposure)
        frame=scope.capture();check()
        if math.hypot(frame.position.x-position.x,frame.position.y-position.y)>.5 or abs(frame.position.z-position.z)>.5:
            raise SafetyError('Stage changed during stationary reference acquisition; create a new request.')
        name=f'reference-attempt-{index+1:02d}'
        save_frame(run/'calibration',name,frame);path=run/'calibration'/(name+'.tiff')
        last_receipt=acknowledge_raw(scope,frame,path)
        pixels=frame.rgb.reshape(-1,3);sample=pixels[::max(1,int(np.ceil(len(pixels)/100000)))]
        q99=np.percentile(sample,99,axis=0);median=np.median(sample,axis=0)
        clipped=float(np.mean(np.any(pixels>=white,axis=1)));level=float(np.max(q99))
        target_met=.52*white<=level<=.78*white and clipped<=.001 and float(np.min(median))>=.03*white
        audit['attempts'].append({'attempt':index+1,'exposure_ms':exposure,'rgb_q99':q99.tolist(),
            'rgb_median':median.tolist(),'clipped_fraction':clipped,'target_met':target_met,
            'raw_path':str(path),'raw_sha256':file_hash(path),'metadata_sha256':file_hash(path.with_suffix('.json')),
            'vendor_receipt':last_receipt})
        audit.update(chosen_exposure_ms=exposure,converged=target_met,choice='last_measured_capture')
        atomic_json(run/'exposure-adjustment.json',audit)
        if not automatic or target_met or index==limit-1:break
        ratio=float(np.clip(.65*white/max(level,1.),.25,4.))
        if clipped>.001:ratio=min(ratio,.65)
        next_exposure=round(float(np.clip(exposure*ratio,1.,10000.)),3)
        if next_exposure==exposure:break
        exposure=next_exposure
    return frame,audit,last_receipt


def run_scan_worker(project, run_id, *, scope_factory=None, profile_loader=None,
                    campaign_runner=None, hold_check=None, resource_reader=None, watchdog_interval_s=5.,prepare_only=False):
    from . import storage
    from .config import connect, arm
    from .imaging import save_frame
    from .chip_campaign import CampaignOptions, run_chip_campaign, validate_campaign_envelope
    from .vendor_lifecycle import acknowledge_raw
    project,run,request,request_sha=load_request(project,run_id)
    continuation=request.get('continuation')
    rectangle_handoff=request.get('rectangle_handoff')
    if rectangle_handoff is not None:
        if prepare_only or continuation is not None or request.get('reference_reuse') is not None or request.get('focus_first') is not None:
            raise SafetyError('Rectangle handoff cannot be mixed with bootstrap, stationary prepare or another recovery.')
        if request['settings']!=rectangle_handoff['settings']:raise SafetyError('Handoff settings differ from the saved session.')
    if prepare_only and request.get('reference_reuse') is not None:
        raise SafetyError('Retained-reference reuse requires an explicit motion start; stationary prepare-only is unavailable.')
    from .startup_continuation import donor_contract
    donor=donor_contract(continuation) if continuation else None
    if continuation:
        if prepare_only:raise SafetyError('Continuation requires an explicit motion start; stationary prepare-only is unavailable.')
        from .scan_continuation import verify_contract,continuation_settings
        verify_contract(project,continuation,consumer_run_id=run_id)
        if request['settings']!=continuation_settings(project,continuation):raise SafetyError('Continuation settings changed.')
    # No arbitrary output mount is accepted by the production storage policy.
    if storage.resolve_output_path(run)!=run:raise SafetyError('Worker project differs from configured managed output.')
    settings=request['settings'];hold_check=hold_check or storage.assert_acquisition_allowed
    reader=resource_reader or resource_snapshot;production_scope=scope_factory is None;scope_factory=scope_factory or connect
    profile_loader=profile_loader or load_camera_profile;campaign_runner=campaign_runner or run_chip_campaign
    auth_path=os.environ.get('FLAKEFINDER_RUN_AUTHORIZATION')
    if auth_path and (Path(auth_path).resolve().parent!=run or Path(auth_path).name!='acquisition-authorization.json'):
        raise SafetyError('Run authorization belongs to another scan.')
    auth=read_json(auth_path) if auth_path else {}
    receipt={'schema':1,'id':run_id,'pid':os.getpid(),'nonce':auth.get('nonce',uuid.uuid4().hex),
        'request_sha256':request_sha,'source_release':request['source_release'],'worker_sha256':file_hash(__file__),'started_at':utc_now(),
        'never_resume':True,'operator_hold_changed':False,'prepare_only':bool(prepare_only)}
    _exclusive_json(run/'worker-start.json',receipt)
    status={'schema':1,'id':run_id,'pid':os.getpid(),'state':'starting','phase':'preflight','started_at':receipt['started_at'],
        'request_sha256':request_sha,'source_release':request['source_release'],'hardware_closed':False,'automatic_restart':False}
    scope=None;original_check=None;watch=None;failure=None;failure_traceback=None;failed_phase=None;correspondence_guard=None
    def update(phase, **values):
        status.update(phase=str(phase)[:100],updated_at=utc_now(),**values)
        atomic_json(run/'scan-status.json',status)
        if watch:watch.progress(phase)
    try:
        update('preflight');hold_check();check_resources(reader(project))
        import cv2
        cv2.setNumThreads(1)
        duration=settings['hours']*3600
        if rectangle_handoff is not None:
            duration=min(duration,rectangle_handoff['deadline_at']-time.time())
            if duration<=0:raise SafetyError('Original acquisition deadline passed before rectangle handoff.')
        if continuation:
            duration=min(duration,continuation['deadline_at']-time.time())
            if duration<=0:raise SafetyError('Original acquisition deadline passed before continuation setup.')
        watch=WorkerWatchdog(run,project,duration,hold_check,reader,interval_s=watchdog_interval_s)
        watch.start();watch.check();profile=profile_loader();config,_=fresh_config(profile,settings,project,run_id)
        last_file_update=-float('inf')
        def handoff_file_progress(info):
            nonlocal last_file_update
            watch.check();status['file_progress']=dict(info);watch.progress(status['phase'])
            now=watch.clock()
            if now-last_file_update>=1.:
                update(status['phase']);last_file_update=now
        if rectangle_handoff is not None:
            from .rectangle_handoff import source_config
            update('validating_handoff_files')
            config,handoff_profile=source_config(project,rectangle_handoff,check=watch.check,progress=handoff_file_progress)
        _exclusive_json(run/'camera-profile-used.json',profile)
        watch.check();scope=scope_factory(config);watch.scope=scope
        # A host-global lease must precede every position/MTB read as well as motion.
        scope.lease.acquire();original_check=scope.check_cancel
        def check():
            watch.check();original_check()
        scope.check_cancel=check
        def memory_check():
            from .acquisition_resources import wait_for_memory_headroom
            previous_phase=status['phase']
            def memory_report(state,elapsed,error):
                record={'schema':1,'state':state,'elapsed_s':elapsed,'error':error,'updated_at':utc_now(),
                    'pending_phase':previous_phase,'policy':'wait before next allocation; same acquisition owner and pending capture'}
                atomic_json(run/'memory-recovery.json',record)
                update(previous_phase if state=='recovered' else 'recovering_capture_memory',memory_recovery=record)
            from .acquisition_resources import check_memory_headroom,CAPTURE_WORKING_BYTES
            return wait_for_memory_headroom(check,scope.cancel.wait,report=memory_report,
                checker=lambda:check_memory_headroom(scope.capture_working_bytes() if hasattr(scope,'capture_working_bytes') else CAPTURE_WORKING_BYTES))
        scope.capture_memory_check=memory_check
        def launch_cleanup(phase):
            if not production_scope:return
            from .memory_cleanup import launch_memory_guard
            deadline=time.time()+max(0,duration-(watch.clock()-watch.started))
            for inherited in (rectangle_handoff,continuation):
                if inherited is not None:deadline=min(deadline,inherited['deadline_at'])
            cleanup=launch_memory_guard(run,deadline,source=WORKSPACE)
            update(phase,memory_cleanup_launch=cleanup)
        if rectangle_handoff is not None:
            from .rectangle_handoff import run_handoff
            update('resuming_saved_rectangle_evidence',state='running',prior_run_id=rectangle_handoff['prior_run_id'])
            launch_cleanup('resuming_saved_rectangle_evidence')
            result=run_handoff(scope,project,run,rectangle_handoff,config,profile,handoff_profile,check,update,
                campaign_runner=campaign_runner,file_progress=handoff_file_progress)
            if watch.triggered.is_set():raise Cancelled(watch.reason)
            status.update(state=result['state'],campaign_state=result['state'],all_chips_found=result.get('all_chips_found',False),
                chip_count=len(result.get('chip_states',{})),finished_at=utc_now())
            return status
        check();position=scope.position()
        if continuation:
            from .scan_continuation import verify_stationary_checkpoint
            from .motion import move_z_bounded
            from .model import Position
            prior_config,prior_profile=verify_contract(project,continuation,consumer_run_id=run_id)
            for key in ('objective','camera_transform','job_template','camera_resources','context'):
                if profile.get(key)!=prior_profile.get(key):raise SafetyError('Installed objective/camera profile changed since prior scan: '+key)
            config,bounds=fresh_config(profile,settings,project,run_id,Position(**continuation['reference_origin_um']))
            config['limits']=dict(continuation['limits']);bounds=list(continuation['search_bounds_um'])
            config['context'].update(coordinate_frame='Explicit same-placement lineage; original XY envelope retained after leased physical checkpoint verification.',
                prior_run_id=donor['prior_run_id'],continuation_parent_run_id=continuation['prior_run_id'],prior_coordinate_frame_id=continuation['prior_coordinate_frame_id'])
            proof=verify_stationary_checkpoint(scope,position,profile,config,continuation)
            _exclusive_json(run/'continuation-verification.json',{'schema':1,'continuation':continuation,'checkpoint':proof})
            focus_travel=resolve_focus_travel(scope,config,settings)
            if focus_travel:_exclusive_json(run/'focus-travel.json',focus_travel)
            validate_campaign_envelope(config,bounds);arm(scope,config)
            update('returning_to_verified_reference',state='running',continuation_prior_run=continuation['prior_run_id'],
                checkpoint_position_um=asdict(position),search_bounds_um=bounds)
            if continuation.get('reference_correspondence'):
                from .correspondence_continuation import stationary_pose
                position=stationary_pose(scope,continuation)
            else:
                origin=continuation['reference_origin_um'];check();move_z_bounded(scope,origin['z']);check();scope.travel(origin['x'],origin['y']);check()
                position=scope.position()
                if math.hypot(position.x-origin['x'],position.y-origin['y'])>.5 or abs(position.z-origin['z'])>.25:
                    raise SafetyError('Return to the pinned reference position was not verified.')
        else:config,bounds=fresh_config(profile,settings,project,run_id,position)
        if not continuation:
            bounds,stage_travel=resolve_grid_travel(scope,config,settings,bounds)
            if stage_travel:_exclusive_json(run/'stage-travel.json',stage_travel)
            focus_travel=resolve_focus_travel(scope,config,settings)
            if focus_travel:_exclusive_json(run/'focus-travel.json',focus_travel)
            validate_campaign_envelope(config,bounds);arm(scope,config)
        update('applying_fixed_optics',state='running',initial_position_um=asdict(position),search_bounds_um=bounds)
        launch_cleanup('applying_fixed_optics')
        controls=scope.hardware_controls();optics=controls.objectives()
        objective=next((o for o in optics['objectives'] if o['position']==optics['position']),{})
        if optics['position']!=config['zen']['expected_objective_position'] or objective.get('magnification')!=20:
            raise SafetyError('The manually engaged objective does not match the measured 20× profile.')
        actual_lamp=controls.set_lamp_percent(settings['light_percent']);scope.set_exposure_ms(settings['exposure_ms'])
        if not math.isfinite(actual_lamp) or abs(actual_lamp-settings['light_percent'])>1.:
            raise SafetyError('Lamp readback does not match the requested level.')
        config['context']['illumination_percent']=actual_lamp
        if continuation and continuation.get('reference_correction'):
            if not continuation.get('reference_correspondence'):
                from .reference_repair import prepare_position
                position=prepare_position(scope,project/'data'/donor['prior_run_id'],donor,update)
            config['context']['reference_correction']=True
        update('capturing_fresh_reference')
        measured_focus=None
        bootstrap_proof=None
        retained_proof=None
        if request.get('reference_reuse') is not None:
            if continuation or request.get('focus_first'):
                raise SafetyError('Retained reference cannot be mixed with continuation or focus-first requests.')
            from .retained_reference import acquire_reference as acquire_retained_reference
            update('retained_reference')
            frame,exposure_audit,vendor_receipt,retained_proof=acquire_retained_reference(
                scope,config,project,run,request['reference_reuse'],check,
                lambda candidate:validate_reference(candidate,config),bounds=bounds)
        elif continuation and continuation.get('reference_correspondence'):
            from .correspondence_continuation import acquire_reference
            checked_reference={}
            def validate_corresponding(frame,decision,path,raw,guard):
                from .reference_repair import verify_corresponding
                corrected=verify_corresponding(frame,project/'data'/donor['prior_run_id'],run,decision,path,raw,guard)
                checked_reference['profile'],checked_reference['quality']=validate_reference(frame,config)
                guard();return corrected
            frame,exposure_audit,vendor_receipt,measured_focus,correspondence_guard=acquire_reference(
                scope,config,settings,run,continuation,update,validate_corresponding)
        elif continuation and continuation.get('reference_focus_recovery'):
            from .reference_focus_recovery import acquire_reference
            checked_reference={}
            def validate_refocused(frame,proof):
                from .reference_repair import verify_fresh
                corrected=verify_fresh(frame,project/'data'/donor['prior_run_id'],run,measured_focus=proof,persist=False)
                checked_reference['profile'],checked_reference['quality']=validate_reference(frame,config)
                return corrected
            frame,exposure_audit,vendor_receipt,measured_focus=acquire_reference(
                scope,config,settings,run,position,check,update,_capture_reference,continuation,validate_refocused)
        elif not continuation and not prepare_only and settings.get('autonomous_reference',False):
            from .autonomous_reference import acquire_reference, BootstrapOptions
            update('autonomous_reference')
            frame,exposure_audit,vendor_receipt,bootstrap_proof=acquire_reference(
                scope,config,settings,run,position,bounds,check,update,_capture_reference,
                lambda candidate:validate_reference(candidate,config),
                options=BootstrapOptions(minimum_chip_um=settings['min_chip_mm']*1000))
        else:frame,exposure_audit,vendor_receipt=_capture_reference(scope,config,settings,run,position,check,update)
        if continuation and continuation.get('reference_correction') and measured_focus is None:
            from .reference_repair import verify_fresh
            verify_fresh(frame,project/'data'/donor['prior_run_id'],run,measured_focus=measured_focus)
        if correspondence_guard:correspondence_guard()
        save_frame(run/'calibration','substrate',frame)
        if correspondence_guard:correspondence_guard()
        reference=run/'calibration'/'substrate.tiff'
        try:
            if measured_focus is not None:edge_profile,quality=checked_reference['profile'],checked_reference['quality']
            else:edge_profile,quality=validate_reference(frame,config)
        except (ValueError,AcquisitionError,SafetyError) as exc:
            _exclusive_json(run/'reference-validation.json',{'schema':1,'accepted':False,'reason':str(exc),
                'raw_sha256':file_hash(reference),'metadata_sha256':file_hash(reference.with_suffix('.json')),
                'position_um':asdict(frame.position),'vendor_receipt':vendor_receipt,'exposure_adjustment':exposure_audit})
            raise
        config['chip_discovery']={'edge_validation_profile':edge_profile}
        if bootstrap_proof is not None:
            quality['qualification']='Autonomous independently focused bright-chip/dark-holder edge and fresh bright-side reference correspondence; not chemical identification.'
            quality['autonomous_bootstrap']={'proof_path':'autonomous-reference/proof.json',
                'proof_sha256':file_hash(run/'autonomous-reference'/'proof.json')}
        if retained_proof is not None:
            quality['qualification']='Explicit same-placement retained focused bright-side reference with independently verified raw edge/focus evidence and verified bounded return; no new reference capture or inherited search bounds.'
            quality['retained_reference']={'proof_path':'retained-reference-proof.json',
                'proof_sha256':file_hash(run/'retained-reference-proof.json'),'source_run':retained_proof['source_run'],
                'fresh_capture_performed':False}
        config['context']['illumination_percent']=frame.metadata['illumination_percent']
        _exclusive_json(run/'reference-validation.json',{'schema':1,'accepted':True,**quality,'raw_sha256':file_hash(reference),
            'metadata_sha256':file_hash(reference.with_suffix('.json')),'position_um':asdict(frame.position),'vendor_receipt':vendor_receipt,
            'exposure_adjustment':exposure_audit})
        _exclusive_json(run/'scan-config.json',config)
        if correspondence_guard:correspondence_guard()
        # The committed reference is loaded by the campaign. Do not retain a
        # second full-resolution camera buffer throughout the whole scan.
        del frame
        if file_hash(run/'scan-request.json')!=request_sha:raise SafetyError('Immutable request changed during setup.')
        hold_check();check();update('starting_discovery')
        options=CampaignOptions(minimum_chip_diameter_um=settings['min_chip_mm']*1000,
            max_chips=settings['max_chips'],walltime_budget_s=settings['hours']*3600,
            grid_prior={'rows':4,'columns':4,'spacing_um':settings['grid_spacing_mm']*1000} if settings.get('grid_4x4') else None)
        full={'focus_anchor_mode':'chip_edges','edge_focus_roi_um':64.,'focus_search_bounds_um':[config['limits']['z_min'],config['limits']['z_max']],
            'max_tiles':settings['max_tiles'],'overlap':.15}
        def progress(phase, _frame, info):
            check()
            allowed=('chip_id','chip','index','total','anchor_index','z_um','search_phase','score','state','reason','completed_tiles','tile_id')
            compact={key:value for key,value in info.items() if key in allowed and isinstance(value,(str,int,float,bool,type(None)))}
            compact={key:(value[:300] if isinstance(value,str) else value) for key,value in compact.items() if not isinstance(value,float) or math.isfinite(value)}
            update(phase,progress=compact)
        if prepare_only:
            update('reference_prepared')
            result={'state':'prepared','chip_states':{},'all_chips_found':False}
        else:
            focus_seed={}
            if measured_focus is not None:
                if continuation.get('reference_correspondence'):
                    from .correspondence_continuation import campaign_seed
                else:
                    from .reference_focus_recovery import campaign_seed
                if correspondence_guard:correspondence_guard()
                focus_seed=campaign_seed(run,measured_focus)
                if correspondence_guard:correspondence_guard()
            elif request.get('focus_first'):
                from .initial_edge_focus import focus_saved_edge
                focus_seed=focus_saved_edge(scope,config,reference,run,request['focus_first'],progress)
            correspondence_guard=None
            result=campaign_runner(config,reference,run,bounds,options,full_chip_options=full,
                scope=scope,callback=progress,**focus_seed)
        if watch.triggered.is_set():raise Cancelled(watch.reason)
        status.update(state=result['state'],campaign_state=result['state'],all_chips_found=result.get('all_chips_found',False),
            chip_count=len(result.get('chip_states',{})),finished_at=utc_now())
    except BaseException as exc:
        if correspondence_guard is not None:
            from .correspondence_continuation import invalidate
            invalidate(run,exc)
        failed_phase=status.get('phase');failure_traceback=traceback.format_exc()[-16000:]
        failure=exc;status.update(state='stopped' if isinstance(exc,(Cancelled,KeyboardInterrupt)) else 'failed',
            error_type=type(exc).__name__,error=str(exc)[:1800],finished_at=utc_now(),failed_phase=failed_phase)
        if scope is not None and not (watch and watch.triggered.is_set()):
            try:status['stop_errors']=scope.stop()
            except BaseException as stop_error:status['stop_error']=str(stop_error)[:500]
    finally:
        watchdog_closed=watch.finish() if watch else True
        if scope is not None and watchdog_closed:
            if original_check is not None:scope.check_cancel=original_check
            closed=_bounded_close(scope);status['hardware_closed']=bool(closed.get('closed'))
            if closed.get('error'):status['cleanup_error']=closed['error']
        elif scope is None:status['hardware_closed']=True
        else:status['cleanup_error']='Watchdog STOP has not returned; adapter closure/lease release unconfirmed.'
        if not status['hardware_closed']:
            status.update(state='failed',requires_attention=True)
        status.update(phase='finished',updated_at=utc_now(),watchdog_reason=watch.reason if watch else None)
        if status.get('state')=='failed' or status.get('watchdog_reason') and not any(
                term in status['watchdog_reason'] for term in ('External STOP:','Absolute acquisition deadline')):
            status.update(recovery_state='needs_repair',repair_required=True)
            try:
                atomic_json(run/'recovery-needed.json',{'schema':1,'run_id':run_id,'state':'open',
                    'created_at':utc_now(),'request_sha256':request_sha,'failed_phase':failed_phase,
                    'error_type':status.get('error_type'),'error':status.get('error'),
                    'traceback':failure_traceback,'hardware_closed':status['hardware_closed'],
                    'source_release':request['source_release'],
                    'next_action':'Diagnose saved evidence, repair and validate the cause, then resume saved work within the authorized session. A failed run is not completion.',
                    'evidence':['scan-status.json','heartbeat.json','batch.json','worker.log']} )
            except OSError as exc:status['recovery_record_error']=str(exc)[:500]
        try:atomic_json(run/'scan-status.json',status)
        except OSError as exc:
            print(json.dumps({'state':'failed','status_persistence_error':str(exc),'original_error':str(failure)}),flush=True)
            raise
    return status


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--project',required=True,type=Path);parser.add_argument('--run',required=True)
    parser.add_argument('--prepare-only',action='store_true',help='Apply fixed optics and validate a stationary fresh reference; no discovery or stage motion.')
    args=parser.parse_args()
    result=run_scan_worker(args.project,args.run,prepare_only=args.prepare_only)
    print(json.dumps(result,allow_nan=False),flush=True)
    return 1 if result['state']=='failed' else 0


if __name__=='__main__':
    raise SystemExit(main())
