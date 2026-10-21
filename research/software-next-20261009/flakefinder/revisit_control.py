"""Explicit UI revisit sessions. Read APIs are offline; only a new worker connects.

The worker has its own PID-bound authorization and host-wide microscope lease.
It never captures, changes optics, homes, or commands Z. A closed worker cannot
be resumed. Manual positioning uses release/reconnect within the same session.
"""
from dataclasses import asdict
from pathlib import Path
import argparse
import json
import os
import re
import secrets
import subprocess
import sys
import threading
import time
import uuid

from .model import Cancelled, Limits, Position, SafetyError, atomic_json
from .revisit_geometry import (digest, read, source_metadata, verify_source,
    source_anchor, fit_registration, transform_target)

WORKSPACE = Path(__file__).resolve().parents[1]
ACTIVE = {'queued', 'connecting', 'ready', 'registration_required', 'released', 'moving', 'stopping'}


def no_active_scan(project):
    from .scan_service import process_alive
    pointer = Path(project)/'scan-control'/'current.json'
    if not pointer.exists():
        return
    identifier = read(pointer).get('id')
    if not re.fullmatch(r'scan-[A-Za-z0-9_-]+', str(identifier)):
        raise SafetyError('Current scan pointer is invalid.')
    folder = Path(project)/'data'/identifier
    status = read(folder/'scan-status.json')
    owner = read(folder/'worker-start.json') if (folder/'worker-start.json').exists() else {}
    if status.get('state') in ('queued', 'starting', 'running', 'stopping') or process_alive(owner.get('pid')):
        raise SafetyError('A scan is active; close it before starting a revisit session.')
    if owner and status.get('hardware_closed') is not True:
        raise SafetyError('Previous acquisition hardware closure is unconfirmed.')


class RevisitControl:
    """Server facade. Commands return immediately; poll status for completion."""
    def __init__(self, project, *, launcher=None, metadata_loader=None):
        self.project = Path(project).resolve(strict=True)
        self.control = self.project/'scan-control'
        self.launcher = launcher or subprocess.Popen
        self.metadata_loader = metadata_loader or source_metadata
        self._lock = threading.Lock()

    def metadata(self, run_id, observation):
        return self.metadata_loader(self.project, run_id, observation)

    def folder(self, session_id):
        if not re.fullmatch(r'revisit-\d{8}-\d{6}-[a-f0-9]{8}', str(session_id)):
            raise ValueError('Invalid revisit session ID.')
        folder = (self.project/'data'/session_id).resolve(strict=True)
        if folder.parent != (self.project/'data').resolve():
            raise SafetyError('Revisit session path redirects outside project data.')
        return folder

    def status(self, session_id=None):
        from .scan_service import process_alive
        if session_id is None:
            pointer = self.control/'revisit-current.json'
            if not pointer.exists():
                return dict(session_id=None, state='idle', hardware_closed=True)
            session_id = read(pointer)['id']
        folder = self.folder(session_id)
        status = read(folder/'scan-status.json')
        owner_path = folder/'worker-start.json'
        owner = read(owner_path) if owner_path.exists() else {}
        launch = read(folder/'launch.json') if (folder/'launch.json').exists() else {}
        alive = process_alive(owner.get('pid', launch.get('pid')))
        result = {**status, 'session_id': session_id, 'process_alive': alive}
        if not alive and status.get('state') in ACTIVE and (owner or launch):
            result.update(state='interrupted', error='Session process is absent. Create a fresh session; it is never automatically re-armed.')
        return result

    def begin(self, run_id, observation, *, placement_mode, operator_confirmed,
              limits=None, duration_s=1800):
        from . import storage
        from .scan_service import controller_lock, write_json, source_files
        if operator_confirmed is not True:
            raise SafetyError('Explicit specimen identity, placement, objective and current-height travel clearance confirmation is required.')
        if placement_mode not in ('unchanged', 'reloaded'):
            raise ValueError('Choose unchanged or reloaded placement.')
        if type(duration_s) not in (int, float) or not 30 <= duration_s <= 8*3600:
            raise ValueError('Session duration must be 30 seconds to 8 hours.')
        if limits is not None:
            limits = asdict(Limits(**limits))
        with self._lock, controller_lock(self.control/'controller.lock'):
            no_active_scan(self.project)
            previous = self.status()
            if previous['state'] in ACTIVE or previous.get('process_alive'):
                raise SafetyError('Another revisit session remains active.')
            if previous.get('session_id') and previous.get('hardware_closed') is not True:
                raise SafetyError('Previous revisit hardware closure is unconfirmed.')
            metadata = self.metadata(run_id, observation)
            verify_source(self.project, metadata)
            storage.verify_project(write_probe=True)
            if storage.project_root().resolve() != self.project:
                raise SafetyError('Revisit project differs from configured acquisition storage.')
            identifier = 'revisit-'+time.strftime('%Y%m%d-%H%M%S')+'-'+uuid.uuid4().hex[:8]
            folder = self.project/'data'/identifier
            folder.mkdir(parents=True, exist_ok=False)
            now = time.time()
            request = dict(schema=1, id=identifier, kind='operator_graphene_revisit',
                source=metadata, placement_mode=placement_mode, operator_confirmed=True,
                limits=limits, created_at=now, expires_at=now+duration_s,
                focus_policy='fresh_current_z_only', source_files=source_files(WORKSPACE/'flakefinder'))
            write_json(folder/'scan-request.json', request, exclusive=True)
            authorization = dict(schema=1, run_id=identifier, operator_start=True,
                request_sha256=digest(folder/'scan-request.json'), nonce=secrets.token_hex(24),
                issued_at=now, expires_at=request['expires_at'],
                holds=[{k:h[k] for k in ('path','sha256')} for h in storage.operator_holds()])
            write_json(folder/'acquisition-authorization.json', authorization, exclusive=True)
            write_json(folder/'scan-status.json', dict(session_id=identifier, state='queued', hardware_closed=True,
                run_id=run_id, chip_id=metadata['chip_id'], observation=observation, expires_at=request['expires_at']))
            write_json(self.control/'revisit-current.json', {'id': identifier})
            env = os.environ.copy()
            env['FLAKEFINDER_RUN_AUTHORIZATION'] = str(folder/'acquisition-authorization.json')
            for key in ('OMP_NUM_THREADS', 'OPENBLAS_NUM_THREADS', 'MKL_NUM_THREADS'):
                env[key] = '1'
            child = None
            try:
                with (folder/'worker.log').open('ab') as log:
                    child = self.launcher([sys.executable, '-m', 'flakefinder.revisit_control',
                        '--project', str(self.project), '--session', identifier], cwd=str(WORKSPACE), env=env,
                        stdin=subprocess.DEVNULL, stdout=log, stderr=subprocess.STDOUT,
                        creationflags=subprocess.CREATE_NO_WINDOW if os.name=='nt' else 0)
                write_json(folder/'launch.json', {'pid': child.pid, 'started_at': now}, exclusive=True)
            except BaseException as exc:
                atomic_json(folder/'scan-status.json', dict(session_id=identifier, state='failed', hardware_closed=child is None, error=str(exc)))
                raise
            return self.status(identifier)

    def _command(self, session_id, action, **values):
        from .scan_service import controller_lock, write_json
        folder = self.folder(session_id)
        with self._lock, controller_lock(folder/'command.lock'):
            status = self.status(session_id)
            if status['state'] not in ACTIVE or status['state'] in ('queued', 'connecting', 'stopping'):
                raise SafetyError('Session is not ready to accept commands.')
            commands = folder/'commands'; commands.mkdir(exist_ok=True)
            if any(not (folder/'receipts'/p.name).exists() for p in commands.glob('*.json')):
                raise SafetyError('A revisit command is still running.')
            command_id = uuid.uuid4().hex
            write_json(commands/(command_id+'.json'), dict(id=command_id, action=action, **values), exclusive=True)
            return {**status, 'command_id': command_id, 'command_state': 'queued'}

    def anchor(self, session_id, corner_id=None, source_observation=None, source_field=None, source_pixel_xy=None):
        return self._command(session_id, 'anchor', corner_id=corner_id, source_observation=source_observation,
                             source_field=source_field, source_pixel_xy=source_pixel_xy)

    def register(self, session_id, *, orientation_unchanged=False):
        return self._command(session_id, 'register', orientation_unchanged=orientation_unchanged)

    def move(self, session_id, observation=None):
        return self._command(session_id, 'move', observation=observation)

    def release(self, session_id):
        return self._command(session_id, 'release')

    def reconnect(self, session_id, *, operator_confirmed):
        if operator_confirmed is not True:
            raise SafetyError('Confirm specimen identity and safe current focus height before reconnecting.')
        return self._command(session_id, 'reconnect', operator_confirmed=True)

    def stop(self, session_id):
        atomic_json(self.folder(session_id)/'STOP', {'time': time.time(), 'operator_stop': True})
        return {**self.status(session_id), 'stop_requested': True}

    def close(self, session_id):
        atomic_json(self.folder(session_id)/'CLOSE', {'time': time.time(), 'operator_close': True})
        return {**self.status(session_id), 'close_requested': True}


class RevisitSession:
    """Worker-only engine, injectable for tests. Construction is still offline."""
    def __init__(self, project, folder, request, *, scope_factory=None, profile_loader=None,
                 hold_check=None, scan_check=None):
        from .config import connect
        from .scan_worker import load_camera_profile
        from .storage import assert_acquisition_allowed
        self.project, self.folder, self.request = Path(project), Path(folder), request
        self.scope_factory = scope_factory or connect
        self.profile_loader = profile_loader or load_camera_profile
        self.hold_check = hold_check or assert_acquisition_allowed
        self.scan_check = scan_check or (lambda: no_active_scan(self.project))
        self.scope = None
        self.anchors = []
        self.registration = None
        self.profile_pins = {}
        self.interrupted = threading.Event()
        self.state = dict(session_id=folder.name, state='connecting', hardware_closed=True,
            run_id=request['source']['run_id'], chip_id=request['source']['chip_id'],
            observation=request['source']['observation'], placement_mode=request['placement_mode'],
            expires_at=request['expires_at'], anchors=[], registration=None)

    def save(self, **values):
        self.state.update(values, updated_at=time.time())
        atomic_json(self.folder/'scan-status.json', self.state)

    def check(self):
        if self.interrupted.is_set() or (self.folder/'STOP').exists() or (self.folder/'CLOSE').exists():
            raise Cancelled('Revisit session stopped or closed.')
        if time.time() >= self.request['expires_at']:
            raise Cancelled('Revisit session authorization expired.')
        self.hold_check()

    def verify(self):
        self.check()
        verify_source(self.project, self.request['source'])
        for anchor in self.anchors:
            verify_source(self.project, anchor)
        for path, expected in self.profile_pins.items():
            if digest(path) != expected:
                raise SafetyError('Installed microscope profile changed during revisit.')

    def connect(self):
        from .scan_worker import fresh_config
        self.verify(); self.scan_check()
        self.save(state='connecting')
        profile = self.profile_loader()
        provenance = profile.get('station_provenance', {})
        for key in ('path', 'objective_profile_path'):
            if provenance.get(key):
                self.profile_pins[provenance[key]] = digest(provenance[key])
        config, _ = fresh_config(profile, self.request['source']['source_context'], self.project, self.folder.name)
        if json.dumps(config['camera_transform'], sort_keys=True) != json.dumps(self.request['source']['transform'], sort_keys=True):
            raise SafetyError('Current installed camera geometry differs from the saved source profile.')
        required = self.request['source'].get('required_objective')
        if required and (profile['objective']['position'] != required['position'] or
                         profile['objective']['magnification'] != required['magnification']):
            raise SafetyError('Installed objective profile differs from the saved source objective.')
        # No capture jobs are used, even when STOP is requested. Preserve current optics.
        config['zen'].update(job_template=None, keep_vendor_results=True, xy_backlash_um=0., motion_only=True)
        scope = self.scope_factory(config)
        self.scope = scope
        self.save(hardware_closed=False)
        scope.lease.acquire()  # precedes every position/MTB read
        original_check = scope.check_cancel
        def check():
            self.check(); original_check()
        scope.check_cancel = check
        self.check()
        position = scope.position()
        controls = scope.hardware_controls()
        xy, z = controls.stage_travel_range(), controls.focus_travel_range()
        for axis in ('x', 'y'):
            if abs(xy[axis+'_position_um']-getattr(position, axis)) > 2.:
                raise SafetyError('Controller and API XY coordinate frames disagree.')
        limits = dict(x_min=xy['x_min_um'], x_max=xy['x_max_um'], y_min=xy['y_min_um'], y_max=xy['y_max_um'],
                      z_min=z['min_um'], z_max=z['max_um'], max_xy_step_um=1200., max_z_step_um=10.)
        requested = self.request.get('limits')
        if requested:
            for axis in ('x', 'y', 'z'):
                limits[axis+'_min'] = max(limits[axis+'_min'], requested[axis+'_min'])
                limits[axis+'_max'] = min(limits[axis+'_max'], requested[axis+'_max'])
            limits['max_xy_step_um'] = min(limits['max_xy_step_um'], requested['max_xy_step_um'])
            limits['max_z_step_um'] = min(limits['max_z_step_um'], requested['max_z_step_um'])
        scope.arm(Limits(**limits))
        self.check()
        if self.request['placement_mode']=='unchanged' and self.registration is None:
            self.registration = dict(rotation=[[1.,0.],[0.,1.]], translation_um=[0.,0.],
                mode='operator_asserted_unchanged_placement', qualification='Explicit operator assertion; no image registration.')
        self.save(state='ready' if self.registration else 'registration_required', position_um=asdict(position),
                  limits=limits, live_controller_ranges={'xy': xy, 'z': z}, registration=self.registration,
                  profile_provenance=profile.get('station_provenance'), focus_policy='fresh_current_z_only')

    def disconnect(self):
        if self.scope is not None:
            self.scope.close()
            self.scope = None
        self.save(hardware_closed=True)

    def execute(self, command):
        self.verify()
        action = command['action']
        if action == 'release':
            self.disconnect(); self.save(state='released'); return
        if action == 'reconnect':
            if self.scope is not None or command.get('operator_confirmed') is not True:
                raise SafetyError('Release hardware and explicitly confirm readiness before reconnecting.')
            self.connect(); return
        if self.scope is None:
            raise SafetyError('Reconnect explicitly before recording anchors or moving.')
        self.scope.check_cancel()
        self.scope.hardware_controls().verify_optics(getattr(self.scope, 'expected_objective_position', None))
        if action == 'anchor':
            anchor = source_anchor(self.project, self.request['source'], **{k:command.get(k) for k in
                ('corner_id','source_observation','source_field','source_pixel_xy')})
            anchor.update(position_um=asdict(self.scope.position()), recorded_at=time.time())
            self.scope.limits.check(Position(**anchor['position_um']))
            existing = [a for a in self.anchors if a['id'] != anchor['id']]
            if len(existing) >= 2:
                raise SafetyError('Two anchors are already recorded. Start a new session for another correspondence.')
            self.anchors = existing+[anchor]; self.registration = None
            self.save(anchors=self.anchors, registration=None, state='registration_required', position_um=anchor['position_um'])
        elif action == 'register':
            self.registration = fit_registration(self.anchors, orientation_unchanged=command.get('orientation_unchanged',False))
            self.save(registration=self.registration, state='ready')
        elif action == 'move':
            if self.registration is None:
                raise SafetyError('Register this placement before moving to saved flakes.')
            target = self.request['source']
            if command.get('observation') and command['observation'] != target['observation']:
                target = source_metadata(self.project, target['run_id'], command['observation'])
                if target['chip_id'] != self.request['source']['chip_id']:
                    raise SafetyError('Target belongs to another chip. Begin its own placement session.')
            verify_source(self.project, target)
            xy, uncertainty = transform_target(self.registration, target['target']['stage_xy_um'])
            start = self.scope.position()
            desired = Position(*xy, start.z)
            self.scope.limits.check(desired)
            receipt = dict(observation=target['observation'], source_pins=target['source_pins'],
                before_um=asdict(start), target_um=asdict(desired), registration=self.registration,
                registration_allowance_um=uncertainty, focus_policy='preserve_fresh_current_z',
                identity_verified=False, requested_at=time.time())
            self.save(state='moving', move=receipt)
            self.scope.travel(*xy)
            self.check()
            final = self.scope.position()
            if max(abs(final.x-desired.x), abs(final.y-desired.y)) > .5 or abs(final.z-start.z) > .3:
                raise SafetyError('Revisit arrival or preserved current focus height was not verified.')
            receipt.update(arrived_um=asdict(final), completed_at=time.time())
            atomic_json(self.folder/'moves'/(command['id']+'.json'), receipt)
            self.save(state='ready', move=receipt, observation=target['observation'], position_um=asdict(final))
        else:
            raise ValueError('Unknown revisit command.')


def run_worker(project, session_id):
    from .scan_service import write_json, source_files
    control = RevisitControl(project)
    folder = control.folder(session_id)
    request = read(folder/'scan-request.json')
    auth = read(folder/'acquisition-authorization.json')
    if request.get('id') != session_id or request.get('kind') != 'operator_graphene_revisit':
        raise SafetyError('Invalid revisit request identity.')
    if digest(folder/'scan-request.json') != auth['request_sha256']:
        raise SafetyError('Revisit authorization does not match its immutable request.')
    if Path(os.environ.get('FLAKEFINDER_RUN_AUTHORIZATION','')).resolve() != (folder/'acquisition-authorization.json').resolve():
        raise SafetyError('Worker authorization belongs to another session.')
    write_json(folder/'worker-start.json', dict(pid=os.getpid(), nonce=auth['nonce'],
        request_sha256=auth['request_sha256'], started_at=time.time()), exclusive=True)
    engine = RevisitSession(project, folder, request)
    done = threading.Event()
    startup_deadline = time.monotonic()+120.
    stop_errors = []
    def monitor():
        while not done.wait(.25):
            try:
                engine.check()
                if engine.state['state']=='connecting' and time.monotonic() > startup_deadline:
                    raise Cancelled('Revisit connection exceeded 120 seconds.')
            except Exception as exc:
                engine.interrupted.set()
                stop_errors.append(str(exc))
                if engine.scope is not None:
                    try: stop_errors.extend(engine.scope.stop() or [])
                    except Exception as error: stop_errors.append(str(error))
                return
    watcher = threading.Thread(target=monitor, daemon=True); watcher.start()
    try:
        if source_files(WORKSPACE/'flakefinder') != request['source_files']:
            raise SafetyError('Software changed after revisit was requested. Begin a fresh session.')
        engine.connect()
        while True:
            engine.check()
            for path in sorted((folder/'commands').glob('*.json')):
                receipt_path = folder/'receipts'/path.name
                if receipt_path.exists():
                    continue
                command = read(path)
                if command.get('id') != path.stem:
                    raise SafetyError('Command identity changed.')
                engine.verify()
                if digest(folder/'scan-request.json') != auth['request_sha256']:
                    raise SafetyError('Session request changed.')
                if source_files(WORKSPACE/'flakefinder') != request['source_files']:
                    raise SafetyError('Revisit software changed during the session.')
                if command['action']=='reconnect': startup_deadline=time.monotonic()+120.
                try:
                    engine.execute(command)
                    receipt = dict(command_id=command['id'], action=command['action'], state='complete', completed_at=time.time())
                except (ValueError, KeyError) as exc:
                    receipt = dict(command_id=command['id'], action=command['action'], state='rejected', error=str(exc), completed_at=time.time())
                atomic_json(receipt_path, receipt)
                engine.save(last_command=receipt)
            done.wait(.1)
    except Cancelled as exc:
        engine.save(state='stopping', error=str(exc))
    except BaseException as exc:
        engine.save(state='failed', error=str(exc))
    finally:
        done.set(); watcher.join(timeout=15)
        # On failure/STOP cancel outstanding motion before releasing ownership.
        if engine.scope is not None and engine.state.get('state') != 'released':
            try: stop_errors.extend(engine.scope.stop() or [])
            except Exception as exc: stop_errors.append(str(exc))
        try:
            engine.disconnect()
        except Exception as exc:
            engine.save(hardware_closed=False, close_error=str(exc), state='failed')
        if engine.state['state'] != 'failed':
            engine.save(state='closed' if (folder/'CLOSE').exists() else 'stopped')
        engine.save(stop_errors=stop_errors)


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--project', required=True)
    parser.add_argument('--session', required=True)
    arguments = parser.parse_args()
    run_worker(arguments.project, arguments.session)
