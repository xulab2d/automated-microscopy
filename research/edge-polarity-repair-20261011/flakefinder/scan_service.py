"""Operator scan setup and process supervision. This module never connects hardware.

Only an explicit authenticated Start launches the separate acquisition worker.
Drafts are immutable, outputs are unique, and reads never resume lost processes.
"""
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
import ctypes
import hashlib
import json
import math
import os
import re
import secrets
import shutil
import subprocess
import sys
import threading
import time
import uuid

GIB = 1024**3
ACTIVE = {'queued', 'starting', 'running', 'stopping'}
DEFAULTS = dict(sample_id='', material='graphene', oxide_nm=90, width_mm=None, height_mm=None,
    z_below_um=25, z_above_um=25, focus_travel_policy='controller', min_chip_mm=2, max_chips=16, hours=2, max_tiles=3000,
    exposure_ms=200, light_percent=90, auto_exposure=True, dark_holder=True, operator_ready=False)


def source_files(source):
    """Fingerprint package code without importing hardware or analysis modules."""
    return {path.relative_to(source).as_posix():hashlib.sha256(path.read_bytes()).hexdigest()
            for path in sorted(source.rglob('*.py')) if '__pycache__' not in path.parts}


def read_json(path, default=None):
    try:
        if path.stat().st_size > 2*1024**2:
            raise ValueError('Oversized control record')
        return json.loads(path.read_text(encoding='utf-8-sig'))
    except FileNotFoundError:
        return default


def write_json(path, data, *, exclusive=False):
    raw = json.dumps(data, indent=2, allow_nan=False).encode('utf-8')
    path.parent.mkdir(parents=True, exist_ok=True)
    if exclusive:
        with path.open('xb') as stream:
            stream.write(raw); stream.flush(); os.fsync(stream.fileno())
    else:
        temp = path.with_name('.'+uuid.uuid4().hex+'.tmp')
        try:
            with temp.open('xb') as stream:
                stream.write(raw); stream.flush(); os.fsync(stream.fileno())
            os.replace(temp, path)
        finally:
            if temp.exists(): temp.unlink()


def memory_status():
    if os.name == 'nt':
        class Memory(ctypes.Structure):
            _fields_ = [('length', ctypes.c_ulong), ('load', ctypes.c_ulong)] + [
                (n, ctypes.c_ulonglong) for n in ('total','available','total_page','available_page','total_virtual','available_virtual','extended')]
        value = Memory(); value.length = ctypes.sizeof(value)
        if not ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(value)):
            raise OSError('Windows memory status unavailable')
        return int(value.available), int(value.total)
    return os.sysconf('SC_AVPHYS_PAGES')*os.sysconf('SC_PAGE_SIZE'), os.sysconf('SC_PHYS_PAGES')*os.sysconf('SC_PAGE_SIZE')


def process_alive(pid):
    if not isinstance(pid, int) or pid <= 0: return False
    if os.name == 'nt':
        api = ctypes.WinDLL('kernel32', use_last_error=True)
        api.OpenProcess.restype = ctypes.c_void_p
        api.OpenProcess.argtypes = [ctypes.c_ulong, ctypes.c_int, ctypes.c_ulong]
        api.GetExitCodeProcess.argtypes = [ctypes.c_void_p, ctypes.POINTER(ctypes.c_ulong)]
        api.CloseHandle.argtypes = [ctypes.c_void_p]
        handle = api.OpenProcess(0x1000, False, pid)
        if not handle: return False
        try:
            code = ctypes.c_ulong()
            return bool(api.GetExitCodeProcess(handle, ctypes.byref(code)) and code.value == 259)
        finally: api.CloseHandle(handle)
    try: os.kill(pid, 0); return True
    except OSError: return False


def validate_settings(value):
    if not isinstance(value, dict) or set(value)-set(DEFAULTS):
        raise ValueError('Unrecognized scan settings')
    settings = {**DEFAULTS, **value}
    if settings['focus_travel_policy'] not in ('controller','explicit'):
        raise ValueError('Focus travel policy must be controller or explicit')
    name = settings['sample_id']
    if not isinstance(name, str) or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9 ._-]{0,79}', name):
        raise ValueError('Enter a sample name (letters, numbers, spaces, dots, hyphens or underscores)')
    if settings['material'] not in ('graphene', 'hBN', 'MoTe2'):
        raise ValueError('Choose graphene, hBN or MoTe2')
    ranges = {'oxide_nm':(1,1000), 'width_mm':(.1,100), 'height_mm':(.1,100),
        'z_below_um':(1,1000), 'z_above_um':(1,1000), 'min_chip_mm':(.1,50),
        'max_chips':(1,100), 'hours':(.05,24), 'max_tiles':(1,10000),
        'exposure_ms':(1,2000), 'light_percent':(1,100)}
    for key, (low, high) in ranges.items():
        number = settings[key]
        if isinstance(number, bool) or not isinstance(number, (int,float)) or not math.isfinite(number) or not low <= number <= high:
            raise ValueError(f'{key.replace("_", " ")} must be between {low:g} and {high:g}')
    for key in ('max_chips','max_tiles'):
        if type(settings[key]) is not int: raise ValueError(key+' must be a whole number')
    for key in ('dark_holder','operator_ready','auto_exposure'):
        if type(settings[key]) is not bool: raise ValueError(key+' must be boolean')
    if not settings['dark_holder']:
        raise ValueError('This discovery profile requires a holder darker than the substrate in all RGB channels')
    return settings


@contextmanager
def controller_lock(path):
    """Cross-process launch serialization; lock existence is not lock ownership."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open('a+b') as stream:
        if not stream.tell(): stream.write(b'0'); stream.flush()
        stream.seek(0)
        try:
            if os.name == 'nt':
                import msvcrt
                msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            raise ValueError('Another scan controller is handling a start; refresh its status') from exc
        try: yield
        finally:
            stream.seek(0)
            if os.name == 'nt': msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
            else: fcntl.flock(stream, fcntl.LOCK_UN)


class ScanService:
    def __init__(self, project, *, resource_reader=None, launcher=None):
        self.project = Path(project).resolve()
        self.control = self.project/'scan-control'
        self._resources = resource_reader
        self._launcher = launcher or subprocess.Popen
        self._mutex = threading.RLock()
        self._children = {}

    def folder(self, run_id):
        if not isinstance(run_id, str) or not re.fullmatch(r'scan-\d{8}-\d{6}-[a-f0-9]{8}', run_id):
            raise ValueError('Invalid scan identifier')
        path = (self.project/'data'/run_id).resolve()
        if not path.is_relative_to(self.project/'data'):
            raise ValueError('Scan path escapes the project')
        return path

    def holds(self):
        from .storage import operator_holds
        records = operator_holds()
        return dict(active=any(h['active'] for h in records), records=records,
                    reason='; '.join(h['reason'] for h in records if h['active']))

    def resources(self):
        if self._resources: return self._resources()
        from .storage import load_policy, verify_project
        result = dict(project_root=str(self.project), reserve_bytes=10*GIB, ram_reserve_bytes=GIB,
                      cloud_sync='unverified', storage_ok=False, error=None)
        try:
            policy = load_policy()
            verified = verify_project()
            if verified.resolve() != self.project: raise ValueError('Viewer project and acquisition destination differ')
            cache = policy.get('cache_volume') if policy and os.name == 'nt' else self.project
            result.update(project_free_bytes=shutil.disk_usage(self.project).free,
                          cache_free_bytes=shutil.disk_usage(cache).free)
            result['ram_available_bytes'], result['ram_total_bytes'] = memory_status()
            result['storage_ok'] = min(result['project_free_bytes'], result['cache_free_bytes']) >= 10*GIB+512*1024**2
        except (OSError, ValueError, RuntimeError) as exc: result['error'] = str(exc)
        return result

    def preflight(self, settings):
        resources = self.resources(); blockers = []; warnings = []
        holds = self.holds()
        try: settings = validate_settings(settings)
        except ValueError as exc: blockers.append(str(exc)); settings = None
        if resources.get('error'): blockers.append(resources['error'])
        if not resources.get('storage_ok'): blockers.append('Drive or its local upload cache cannot preserve the 10 GiB reserve and next-capture allowance')
        if (resources.get('ram_available_bytes') or 0) < 1.5*GIB:
            blockers.append('At least 1.5 GiB of available RAM is required before acquisition')
        estimates = {}
        if settings:
            # Full-rectangle upper bound; actual chips occupy only part of it.
            # Sparse sample count is checked before any lattice allocation.
            step_mm = (settings['min_chip_mm']-.04)/math.sqrt(2)
            probes = (math.ceil(settings['width_mm']/step_mm)+1)*(math.ceil(settings['height_mm']/step_mm)+1)
            tiles = (math.ceil(settings['width_mm']/.36)+1)*(math.ceil(settings['height_mm']/.30)+1)
            raw = 2464*2056*3*2
            estimates = dict(tile_upper_bound=tiles, raw_bytes_per_frame=raw,
                estimated_bytes=math.ceil((tiles+settings['max_chips']*100)*raw*1.5),
                max_seed_probes=probes, assumptions='Whole search rectangle at measured 20× scale, 15% overlap, plus 100 focus/search-frame equivalents per chip; not a measured chip plan')
            if probes > 10000: blockers.append('Search plan exceeds 10,000 seed positions; reduce the area or increase the minimum chip size')
            free = min(resources.get('project_free_bytes',0),resources.get('cache_free_bytes',0))
            if estimates['estimated_bytes']+10*GIB > free:
                warnings.append('The full search-area storage estimate exceeds available headroom. Actual chips are checked before scanning; the run may stop before all chips fit.')
            if not settings['operator_ready']:
                warnings.append('Before Start, confirm the sample is placed, travel is clear, and 20× is roughly focused on exposed substrate')
        if holds['active']: warnings.append('The displayed operator hold needs explicit authorization for this run; hold files stay unchanged')
        if any('Unreadable operator hold' in h['reason'] for h in holds['records']): blockers.append('An operator hold record is unreadable')
        from .storage import WORKSPACE
        for stop in (self.project/'STOP', WORKSPACE/'STOP'):
            if stop.exists(): blockers.append('STOP is present: '+str(stop))
        warnings.append('Drive writes consume local upload cache; cloud synchronization is not confirmed by a successful write')
        return dict(ok=not blockers, blockers=list(dict.fromkeys(blockers)), warnings=warnings,
                    estimates=estimates, settings=settings, resources=resources, hold=holds)

    def request(self, run_id):
        request = read_json(self.folder(run_id)/'scan-request.json')
        if not isinstance(request,dict) or request.get('id') != run_id or request.get('schema') != 1:
            raise ValueError('Scan plan is missing or invalid')
        validate_settings(request['settings'])
        return request

    def run_status(self, run_id):
        folder = self.folder(run_id); request = self.request(run_id)
        status = read_json(folder/'scan-status.json', {}) or {}
        started = read_json(folder/'worker-start.json', {}) or read_json(folder/'launch.json', {}) or {}
        heartbeat = read_json(folder/'heartbeat.json', {}) or {}
        state = status.get('state','draft')
        alive = process_alive(started.get('pid')) if started else False
        result = dict(status, id=run_id, state=state, settings=request['settings'], sample_id=request['settings']['sample_id'],
            output_rel='data/'+run_id, process_alive=alive, heartbeat_at=heartbeat.get('at') or heartbeat.get('updated_at'),
            stop_requested=(folder/'STOP').exists())
        if result['stop_requested']:
            stop = folder/'STOP'
            reason = 'Stop requested; no reason was recorded.'
            try:
                if not stop.resolve().is_relative_to(folder.resolve()):
                    raise ValueError('STOP record leaves the run folder')
                result['stop_recorded_at'] = stop.stat().st_mtime
                with stop.open('rb') as stream:
                    raw = stream.read(16*1024+1)
                if len(raw)>16*1024:
                    reason = 'Stop requested; STOP record exceeds the display limit.'
                else:
                    text = raw.decode('utf-8-sig',errors='replace').strip()
                    try: record = json.loads(text)
                    except ValueError: record = text  # Legacy plain-text STOP.
                    candidate = record.get('reason') if isinstance(record,dict) else record
                    if isinstance(candidate,str) and candidate.strip():
                        reason = candidate.strip()[:2000]
                        if len(candidate.strip())>2000: reason += '…'
                    if isinstance(record,dict):
                        at = record.get('at')
                        if isinstance(at,str) or type(at) in (int,float) and math.isfinite(at):
                            result['stop_recorded_at'] = at
            except (OSError,ValueError):
                reason = 'Stop requested; its saved reason is temporarily unreadable.'
            result['stop_reason'] = reason
        if state in ACTIVE and started and not alive:
            result.update(state='needs_attention', error='Worker exited before a final checkpoint. Saved frames remain; no automatic restart or movement.')
        # Project saved acquisition records only; never inspect the camera or
        # infer a completed chip from the current-chip progress counter.
        cache = self.__dict__.setdefault('_scan_count_records', {})
        warnings = []
        def saved_record(path):
            key = str(path)
            previous = cache.get(key)
            try:
                stat = path.stat()
                signature = (stat.st_mtime_ns, stat.st_size)
                if previous and previous[0] == signature:
                    return previous[1]
                if stat.st_size > 32*1024**2:
                    raise ValueError('Oversized acquisition record')
                value = json.loads(path.read_text(encoding='utf-8-sig'))
                if not isinstance(value, dict): raise ValueError('Expected an object')
                # Keep only scalar counters in the polling cache, not every
                # frame's autofocus/coverage payload for all recent runs.
                if path.name == 'manifest.json':
                    rows = value.get('completed')
                    value = dict(state=value.get('state'), tile_count=value.get('tile_count'),
                        completed_count=len(rows) if isinstance(rows, dict) else None)
                elif path.name == 'batch.json':
                    rows = value.get('chip_states', {})
                    attempts=value.get('boundary_attempts',[])
                    unresolved=sum(1 for row in attempts if isinstance(row,dict) and
                        str(row.get('state','')).startswith('unresolved')) if isinstance(attempts,list) else 0
                    value = dict(unresolved_boundaries=unresolved,chip_states={key:{name:row.get(name) for name in ('state','completed_tiles','total_tiles')}
                        if isinstance(row,dict) else row for key,row in rows.items()} if isinstance(rows,dict) else {})
                cache[key] = (signature, value)
                return value
            except FileNotFoundError:
                if not previous: return {}
            except (OSError, ValueError, UnicodeError):
                pass
            warnings.append('Saved counts temporarily unavailable: '+path.relative_to(folder).as_posix())
            return previous[1] if previous else {}
        batch = saved_record(folder/'batch.json')
        chips = batch.get('chip_states', {})
        if not isinstance(chips, dict): chips = {}
        counts = dict(completed_chips=0, skipped_chips=0, raster_images_saved=0, known_chips=len(chips))
        for key, chip in chips.items():
            if not isinstance(chip, dict) or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_-]{0,79}', key):
                warnings.append('Invalid chip status entry omitted')
                continue
            manifest_path = (folder/key/'manifest.json').resolve()
            if not manifest_path.is_relative_to(folder.resolve()):
                warnings.append('Chip manifest outside scan folder omitted')
                continue
            manifest = saved_record(manifest_path)
            complete = manifest.get('completed_count')
            stored = complete if type(complete) is int else chip.get('completed_tiles', 0)
            stored = stored if type(stored) is int and stored >= 0 else 0
            counts['raster_images_saved'] += stored
            chip_state = chip.get('state')
            finished_state = manifest.get('state', chip_state)
            total = manifest.get('tile_count', chip.get('total_tiles'))
            if finished_state in ('complete', 'complete_with_focus_limits', 'complete_with_boundary_limits') and type(total) is int and stored == total:
                counts['completed_chips'] += 1
            if chip_state in ('focus_unresolved', 'skipped', 'deferred', 'deferred_time_budget', 'deferred_storage_budget'):
                counts['skipped_chips'] += 1
        result.update(counts=counts, counts_source='saved batch and chip manifests', counts_warnings=warnings)
        if result['state'] in ('failed','needs_attention') or result.get('repair_required'):
            result['supervision']=dict(state='repair_required',resolved=False,
                message='Scan incomplete: recovery or repair is required. This failure is not a completed scan.',
                evidence='recovery-needed.json' if (folder/'recovery-needed.json').is_file() else 'scan-status.json')
        elif result.get('phase')=='recovering_capture_memory':
            result['supervision']=dict(state='recovering',resolved=False,
                message='Waiting for capture memory; the same worker will resume the pending image when headroom recovers.')
        elif result['state'] in ACTIVE and counts['raster_images_saved']==0 and batch.get('unresolved_boundaries',0)>=3:
            result['supervision']=dict(state='mapping_not_progressing',resolved=False,
                message=f"Mapping has failed on {batch['unresolved_boundaries']} seeds with no raster images. Boundary-search repair is needed.")
        elif result['state'] in ('stopped','time_budget_reached','storage_budget_reached'):
            result['supervision']=dict(state='incomplete',resolved=False,
                message='Acquisition ended before campaign completion; saved coverage and remaining work need review.')
        else:result['supervision']=dict(state='monitoring' if result['state'] in ACTIVE else result['state'])
        if result['supervision'].get('resolved') is False:
            # Repair evidence is separate from immutable acquisition status.
            # A successful offline test must never turn a failed scan green.
            repair_path=folder/'supervision/repair-status.json'
            try:
                if repair_path.exists():
                    if not repair_path.resolve().is_relative_to(folder.resolve()) or repair_path.stat().st_size>16*1024:
                        raise ValueError('Invalid repair record path or size')
                    repair=read_json(repair_path)
                    if (not isinstance(repair,dict) or repair.get('schema')!=1 or repair.get('run_id')!=run_id
                            or repair.get('state') not in ('diagnosing','repairing','offline_validated','awaiting_live_validation')
                            or not isinstance(repair.get('summary'),str) or not repair['summary'].strip()):
                        raise ValueError('Invalid repair record identity or state')
                    result['repair_progress']={key:repair.get(key) for key in ('state','updated_at')}
                    result['repair_progress']['summary']=repair['summary'].strip()[:800]
                    result['supervision']['message']+=' Repair progress: '+result['repair_progress']['summary']
            except (OSError,ValueError,UnicodeError):
                result['repair_status_warning']='Saved repair progress is unavailable; original acquisition status is unchanged.'
        return result

    def memory_cleanup_status(self, run):
        """Read-only saved cleanup activity; host RAM still comes from resources()."""
        if not run: return None
        folder = self.folder(run['id']); path = folder/'memory-guard/status.json'
        try:
            if not path.exists():
                launch=run.get('memory_cleanup_launch')
                if not isinstance(launch,dict):return None
                state='starting' if launch.get('state')=='launched' and process_alive(launch.get('pid')) else 'unavailable'
                return dict(state=state,display_state=state,active=False,actions=0,closed_apps=[],
                    error=launch.get('error') or 'Cleanup helper has not produced its first status.',source='cleanup launch receipt')
            if not path.resolve().is_relative_to(folder) or path.stat().st_size > 16*1024:
                raise ValueError('Cleanup status cannot be read safely')
            record = read_json(path)
            if not isinstance(record,dict) or record.get('run_id') != run['id'] or record.get('worker_pid') != run.get('pid'):
                raise ValueError('Cleanup status does not match this acquisition')
            updated = datetime.fromisoformat(record['updated_at'].replace('Z','+00:00')).timestamp()
            age = time.time()-updated
            if not math.isfinite(age) or age < -5: raise ValueError('Cleanup timestamp is invalid')
            poll = record.get('poll_seconds',10)
            poll = poll if type(poll) in (int,float) and math.isfinite(poll) and 1 <= poll <= 60 else 10
            stale = age > max(30,3*poll)
            state = record.get('state','unknown'); display = state
            running_states = ('starting','monitoring','low_memory','waiting_for_readable_status')
            if state in running_states:
                if run.get('state') not in ACTIVE or run.get('process_alive') is not True:
                    display = 'inactive'
                else:
                    from .scan_continuation import process_alive_strict
                    try: alive = process_alive_strict(record.get('guard_pid'))
                    except RuntimeError: alive = None
                    display = 'exited' if alive is False else 'unavailable' if alive is None else 'stale' if stale else state
            actions = record.get('actions',0); denials = record.get('access_denials',0)
            result = dict(state=state,display_state=display,active=display in ('monitoring','low_memory','starting'),
                actions=actions if type(actions) is int and actions>=0 else 0,
                access_denials=denials if type(denials) is int and denials>=0 else 0,
                updated_at=record['updated_at'],stale=stale,
                error=str(record.get('last_error') or '')[:500],closed_apps=[],source='saved cleanup log')
            events_path=folder/'memory-guard/events.json'
            if events_path.is_file() and events_path.resolve().is_relative_to(folder) and events_path.stat().st_size<=128*1024:
                try:
                    events=read_json(events_path,[])
                    if isinstance(events,list):
                        result['closed_apps']=list(dict.fromkeys(str(e.get('detail',{}).get('name'))[:120]
                            for e in events[-200:] if isinstance(e,dict) and e.get('kind')=='stopped'
                            and isinstance(e.get('detail'),dict) and e['detail'].get('name')))
                except (OSError,ValueError,TypeError): pass
            return result
        except (OSError,ValueError,KeyError,TypeError,AttributeError) as exc:
            return dict(state='unavailable',display_state='unavailable',active=False,actions=None,
                error=str(exc)[:500],closed_apps=[],source='saved cleanup log')

    def snapshot(self):
        pointer = read_json(self.control/'current.json', {}) or {}
        run = self.run_status(pointer['id']) if pointer.get('id') else None
        recent = []
        for path in sorted((self.project/'data').glob('scan-????????-??????-????????'), reverse=True)[:30]:
            try:
                item = self.run_status(path.name)
                recent.append({k:item.get(k) for k in ('id','state','sample_id','output_rel')})
            except (OSError,ValueError,KeyError): continue
        defaults = dict(DEFAULTS)
        if run: defaults.update(run['settings']); defaults['operator_ready'] = False
        progress = None
        if run and (self.folder(run['id'])/'batch.json').exists():
            from .live_progress import build_live_progress
            progress = build_live_progress(self.folder(run['id']))
        if run and not (progress or {}).get('current_image'):
            # A stationary qualification has no batch manifest. Its fixed
            # reference paths still expose an actual saved image and position.
            folder = self.folder(run['id'])
            preview = (folder/'calibration'/'substrate.jpg').resolve()
            sidecar = (folder/'calibration'/'substrate.json').resolve()
            if preview.is_relative_to(folder.resolve()) and sidecar.is_relative_to(folder.resolve()) and preview.is_file() and sidecar.is_file():
                try:
                    record = read_json(sidecar, {})
                    if not isinstance(record, dict): raise ValueError('Invalid reference metadata')
                    captured = record.get('acquired_at')
                    position = record.get('position')
                    saved = preview.stat().st_mtime
                    progress = progress or dict(schema=1, available=True, source='saved_files_only',
                        phase=run.get('phase'), state=run.get('state'), updated_at=run.get('updated_at'),
                        current_step=None, counts={}, focus={}, events=[], timing={}, stage_map={})
                    progress['current_image'] = dict(relative_path='calibration/substrate.jpg',
                        label='Fresh substrate reference', captured_at=captured, saved_at=saved)
                    progress.setdefault('stage_map', {}).update(current_position_um=position,
                        current_position_at=captured, current_position_saved_at=saved,
                        position_source='saved reference capture; not a live stage readback')
                    progress.setdefault('events', []).append(dict(at=captured if isinstance(captured, (int,float)) else saved,
                        kind='reference', label='Fresh substrate reference saved', time_kind='capture' if isinstance(captured, (int,float)) else 'status_saved'))
                except (OSError, ValueError, UnicodeError):
                    pass  # A writer can be between its JPEG and metadata saves.
        if run:
            folder = self.folder(run['id'])
            focus_folder = (folder/'initial-edge-focus').resolve()
            if focus_folder.is_relative_to(folder.resolve()) and focus_folder.is_dir():
                def focus_record(path):
                    try:
                        if not path.resolve().is_relative_to(folder.resolve()): return {}
                        value = read_json(path,{})
                        return value if isinstance(value,dict) else {}
                    except (OSError,ValueError,UnicodeError): return {}
                def number(value):
                    return type(value) in (int,float) and math.isfinite(value)
                recovery = focus_record(focus_folder/'recovery.json')
                result = focus_record(focus_folder/'focus-result.json') or recovery.get('result') or {}
                if not isinstance(result,dict): result={}
                raw_samples=recovery.get('samples',[])
                if not isinstance(raw_samples,list): raw_samples=[]
                samples = [{key:sample[key] for key in ('z_um','score','phase','focus_metric') if key in sample}
                    for sample in raw_samples
                    if isinstance(sample,dict) and number(sample.get('z_um')) and number(sample.get('score'))]
                progress = progress or dict(schema=1,available=True,source='saved_files_only',
                    counts={},events=[],timing={},stage_map={})
                progress.setdefault('phase',run.get('phase') or 'initial_edge_focus')
                progress.setdefault('state',run.get('state'))
                progress.setdefault('updated_at',run.get('updated_at'))
                if not (folder/'batch.json').exists() or run.get('phase') in ('initial_edge_focus','initial_focus_verified'):
                    progress['current_step']='focus'
                observed=recovery.get('best_observed') or {}
                best = result.get('z_um') if result.get('success') is True else observed.get('z_um') if isinstance(observed,dict) else None
                if not number(best) and samples: best=max(samples,key=lambda v:v['score'])['z_um']
                initial_focus = dict(status='focused' if result.get('success') is True else recovery.get('state') or 'awaiting_saved_samples',
                    samples=samples if len(samples)<=400 else [samples[round(i*(len(samples)-1)/399)] for i in range(400)],
                    sample_count=len(samples),samples_sampled=len(samples)>400,best_z_um=best,
                    best_is_verified=result.get('success') is True,latest_sample=samples[-1] if samples else None,
                    label='Initial chip-edge autofocus',source='initial_edge_focus',
                    qualification='Saved initial edge-focus scores. Search phases may use different score metrics; only a verified result confirms focus.')
                images=[];initial_images=[]
                if progress.get('current_image'):
                    images.append((progress['current_image'],progress.get('stage_map',{}).get('current_position_um')))
                for relative,label in (
                    ('selection/full-field','Initial edge-focus selection · full field'),
                    ('best_observed','Best observed edge-focus ROI · unverified'),
                    ('verified_focus','Verified edge-focus ROI'),
                    ('focused-full-field','Verified initial focus · full field')):
                    jpg=(focus_folder/(relative+'.jpg')).resolve()
                    if not jpg.is_relative_to(folder.resolve()) or not jpg.is_file(): continue
                    record=focus_record(focus_folder/(relative+'.json'))
                    if not record: continue
                    try: saved=jpg.stat().st_mtime
                    except OSError: continue
                    metadata=record.get('metadata') or {}
                    if not isinstance(metadata,dict): metadata={}
                    position=metadata.get('physical_stage_position') or metadata.get('physical_position_um') or metadata.get('stage_position_um') or record.get('position')
                    item=(dict(relative_path=jpg.relative_to(folder).as_posix(),label=label,
                        captured_at=record.get('acquired_at'),saved_at=saved),position)
                    images.append(item);initial_images.append(item)
                initial_at=max((item[0]['captured_at'] for item in initial_images if number(item[0].get('captured_at'))),default=None)
                initial_focus['captured_at']=initial_at
                current_focus=progress.get('focus') or {}
                current_at=current_focus.get('captured_at')
                current_image=progress.get('current_image') or {}
                if not number(current_at) and 'focus' in str(current_image.get('relative_path','')):
                    current_at=current_image.get('captured_at')
                if not number(current_at):
                    current_at=current_focus.get('updated_at') or current_focus.get('saved_at')
                has_current=bool(current_focus.get('samples')) or current_focus.get('status') not in (None,'unavailable','awaiting_saved_samples')
                # An empty batch is a handoff record, not new image/focus evidence.
                # Keep a later real focus search even if it has no samples yet.
                if not has_current or number(initial_at) and number(current_at) and initial_at>current_at:
                    progress['focus']=initial_focus
                if images:
                    image,position=max(images,key=lambda pair:pair[0].get('captured_at') if number(pair[0].get('captured_at')) else pair[0].get('saved_at') or 0)
                    progress['current_image']=image
                    progress.setdefault('stage_map',{}).update(current_position_um=position,
                        current_position_at=image.get('captured_at'),current_position_saved_at=image.get('saved_at'),
                        position_source='latest saved focus/reference capture; not a live stage readback')
        current_focus=(progress or {}).get('focus') or {}
        if run and not current_focus.get('samples') and not current_focus.get('best_is_verified'):
            reused_path=self.folder(run['id'])/'initial-focus-reused.json'
            if reused_path.exists():
                progress=progress or dict(schema=1,available=True,source='saved_files_only',
                    phase=run.get('phase'),state=run.get('state'),counts={},events=[],timing={},stage_map={})
                try:
                    if not reused_path.resolve().is_relative_to(self.folder(run['id']).resolve()):
                        raise ValueError('Reused-focus record leaves this run')
                    reused=read_json(reused_path,{})
                    source_folder=self.folder(reused.get('source_run'))
                    result_path=Path(reused.get('source_result','')).resolve()
                    expected=(source_folder/'initial-edge-focus'/'focus-result.json').resolve()
                    if result_path!=expected or not result_path.is_relative_to((self.project/'data').resolve()):
                        raise ValueError('Reused-focus source leaves its pinned run')
                    with result_path.open('rb') as stream: raw=stream.read(2*1024**2+1)
                    if len(raw)>2*1024**2 or hashlib.sha256(raw).hexdigest()!=reused.get('result_sha256'):
                        raise ValueError('Pinned reused-focus result checksum mismatch')
                    result=json.loads(raw)
                    z=result.get('z_um')
                    if result.get('success') is not True or type(z) not in (int,float) or not math.isfinite(z) or z!=reused.get('z_um'):
                        raise ValueError('Pinned reused-focus result is not a verified matching height')
                    rows=result.get('samples',[])
                    if not isinstance(rows,list): rows=[]
                    samples=[{key:row[key] for key in ('z_um','score','phase','focus_metric') if key in row}
                        for row in rows if isinstance(row,dict) and all(type(row.get(key)) in (int,float) and math.isfinite(row[key]) for key in ('z_um','score'))]
                    progress['focus']=dict(status='reused_verified_focus',source='reused_initial_focus',
                        label='Reused initial focus · '+reused['source_run'],source_run=reused['source_run'],
                        source_result=result_path.relative_to(self.project).as_posix(),result_sha256=reused['result_sha256'],
                        samples=samples if len(samples)<=400 else [samples[round(i*(len(samples)-1)/399)] for i in range(400)],
                        sample_count=len(samples),samples_sampled=len(samples)>400,best_z_um=z,best_is_verified=True,
                        validates_current_field=False,saved_at=result_path.stat().st_mtime,
                        qualification='Reused from '+reused['source_run']+'. This verified prior edge measurement does not validate focus in the current field.')
                except (OSError,ValueError,TypeError,AttributeError):
                    progress['focus']=dict(status='reused_focus_unavailable',source='reused_initial_focus',
                        samples=[],sample_count=0,best_is_verified=False,validates_current_field=False,
                        qualification='The referenced focus evidence is unavailable or failed its identity/checksum check; no reused focus is asserted.')
        resources=dict(self.resources());resources['memory_cleanup']=self.memory_cleanup_status(run)
        return dict(defaults=defaults, resources=resources, hold=self.holds(), run=run, progress=progress, recent_runs=recent)

    def plan_continuation(self, prior_run_id, z_min_um, *, z_max_um=None, focus_travel_policy='explicit'):
        """Save a new explicit lower-Z-extension request; never resume or launch."""
        from .scan_continuation import build_contract,continuation_settings
        contract=build_contract(self.project,prior_run_id,z_min_um,z_max_um=z_max_um,focus_travel_policy=focus_travel_policy)
        return self.plan(continuation_settings(self.project,contract),continuation=contract)

    def plan(self, settings, *, continuation=None, focus_first=None):
        check = self.preflight(settings)
        if check['settings'] is None: raise ValueError('; '.join(check['blockers']))
        with self._mutex, controller_lock(self.control/'controller.lock'):
            current = self.snapshot()['run']
            if current and (current['state'] in ACTIVE or current['process_alive']):
                raise ValueError('A scan worker already owns the current run')
            if continuation is not None:
                from .scan_continuation import verify_contract,continuation_settings
                verify_contract(self.project,continuation)
                if check['settings']!=continuation_settings(self.project,continuation):
                    raise ValueError('Continuation settings differ from the pinned same-placement contract')
            run_id = time.strftime('scan-%Y%m%d-%H%M%S-')+uuid.uuid4().hex[:8]
            folder = self.folder(run_id)
            source = Path(__file__).resolve().parent
            request = dict(schema=1, id=run_id, settings=check['settings'], created_at=time.time(),
                source_release=dict(source_directory=str(source.parent), scope='flakefinder-python-v1',
                    files=source_files(source)))
            if continuation is not None:request['continuation']=continuation
            if focus_first is not None:
                if continuation is None:raise ValueError('Saved edge reuse requires a verified same-placement continuation.')
                request['focus_first']=focus_first
            write_json(folder/'scan-request.json',request,exclusive=True)
            write_json(folder/'preflight.json',check)
            write_json(folder/'scan-status.json',dict(state='draft',phase='setup',updated_at=time.time()))
            write_json(self.control/'current.json',dict(id=run_id))
            return dict(run=self.run_status(run_id), preflight=check)

    def start(self, run_id, *, release_hold=False, prepare_only=False):
        if type(prepare_only) is not bool: raise ValueError('prepare_only must be boolean')
        with self._mutex, controller_lock(self.control/'controller.lock'):
            folder = self.folder(run_id); request = self.request(run_id)
            if prepare_only and request.get('continuation'):
                raise ValueError('Continuation includes returning to the prior reference; stationary prepare-only cannot launch it')
            current = self.snapshot()['run']
            if current and current['id'] != run_id and (current['state'] in ACTIVE or current['process_alive']):
                raise ValueError('Another scan is active')
            if (folder/'launch.json').exists() or (folder/'worker-start.json').exists():
                raise ValueError('This scan has already been started. It cannot be restarted implicitly; create a fresh plan.')
            if (folder/'STOP').exists(): raise ValueError('This run has a STOP record; create a new plan after inspecting the reason')
            if request.get('continuation'):
                from .scan_continuation import verify_contract
                verify_contract(self.project,request['continuation'])
            check = self.preflight(request['settings'])
            if not check['ok']: raise ValueError('; '.join(check['blockers']))
            planned = read_json(folder/'preflight.json', {})
            if check['hold']['records'] != planned.get('hold',{}).get('records'):
                raise ValueError('Operator hold changed after planning; inspect the new hold and save a new plan')
            if not request['settings']['operator_ready']: raise ValueError('Confirm specimen placement and current 20× focus in a new saved plan')
            if check['hold']['active'] and release_hold is not True:
                raise ValueError('Explicitly authorize the displayed operator hold for this run')
            source = Path(__file__).resolve().parent
            release = request['source_release']
            if release.get('scope') == 'flakefinder-python-v1':
                if source_files(source) != release['files']:
                    raise ValueError('Software changed after planning; save a new plan before Start')
            else:
                # Historical requests retain their original, smaller file list.
                for name, digest in release['files'].items():
                    if hashlib.sha256((source/name).read_bytes()).hexdigest() != digest:
                        raise ValueError('Software changed after planning; save a new plan before Start')
            from .storage import verify_project
            verify_project(write_probe=True, acquisition=True)
            now = time.time()
            authorization = dict(schema=1, run_id=run_id, operator_start=True,
                request_sha256=hashlib.sha256((folder/'scan-request.json').read_bytes()).hexdigest(),
                nonce=secrets.token_hex(24), issued_at=now, expires_at=now+request['settings']['hours']*3600+300,
                holds=[{k:h[k] for k in ('path','sha256')} for h in check['hold']['records']])
            write_json(folder/'acquisition-authorization.json',authorization,exclusive=True)
            write_json(folder/'scan-status.json',dict(state='queued',phase='starting',started_at=now,updated_at=now))
            env = os.environ.copy(); env['FLAKEFINDER_RUN_AUTHORIZATION'] = str(folder/'acquisition-authorization.json')
            for key in ('OMP_NUM_THREADS','OPENBLAS_NUM_THREADS','MKL_NUM_THREADS'): env[key] = '2'
            args = [sys.executable,'-m','flakefinder.scan_worker','--project',str(self.project),'--run',run_id]
            if prepare_only: args.append('--prepare-only')
            try:
                with (folder/'worker.log').open('ab') as log:
                    child = self._launcher(args, cwd=str(source.parent), env=env, stdin=subprocess.DEVNULL,
                        stdout=log, stderr=subprocess.STDOUT, creationflags=subprocess.CREATE_NO_WINDOW if os.name=='nt' else 0)
                self._children[run_id] = child
                write_json(folder/'launch.json',dict(pid=child.pid,started_at=now,args=args),exclusive=True)
                write_json(self.control/'current.json',dict(id=run_id))
            except BaseException as exc:
                write_json(folder/'scan-status.json',dict(state='failed',phase='launch',error=str(exc),updated_at=time.time()))
                raise
            return dict(run=self.run_status(run_id))

    def stop(self, run_id):
        folder = self.folder(run_id); self.request(run_id)
        try:
            write_json(folder/'STOP',dict(reason='Operator pressed Stop',at=time.time()),exclusive=True)
        except FileExistsError: pass
        return dict(run=self.run_status(run_id), message='Stop requested; current vendor call and cleanup may finish before exit')
