"""Wait for the active chip, then continue its saved campaign in one owner.

Root STOP remains authoritative. Failures are never retried automatically;
explicit seed-rejection recovery preserves the existing campaign budget.
"""
from pathlib import Path
import argparse, hashlib, json, math, os, subprocess, sys, time

WORKSPACE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(WORKSPACE))
from flakefinder.lease import HardwareLease
from flakefinder.model import SafetyError, atomic_json

FINISHED_STATES = {'search_sampling_complete', 'chip_budget_reached'}
BOUNDED_STATES = {'time_budget_reached', 'storage_budget_reached'}
SEED_REJECTION_ERROR = 'Seed centre is not verified substrate under this profile.'


def read_batch(root):
    # Atomic replacement can briefly contend with a Windows reader. Persistent
    # corrupt/missing status is an error, never permission to restart.
    for attempt in range(3):
        try:
            value = json.loads((root / 'batch.json').read_text(encoding='utf-8'))
            if not isinstance(value, dict):
                raise ValueError('Campaign status must be a JSON object.')
            return value
        except (OSError, json.JSONDecodeError):
            if attempt == 2:
                raise
            time.sleep(0.2)


def campaign_identity(batch):
    keys = ('coordinate_frame_id', 'reference_path', 'reference_sha256', 'search_bounds_um')
    if not batch.get('coordinate_frame_id') or not batch.get('reference_path'):
        raise ValueError('Campaign coordinate/reference identity is missing.')
    return {key: batch.get(key) for key in keys}


def checkpoint_age(batch):
    stamp = float(batch.get('updated_at', 0))
    if not math.isfinite(stamp) or stamp <= 0:
        raise ValueError('Campaign checkpoint timestamp is missing or invalid.')
    return time.time() - stamp


def verify_seed_rejection(root, config, batch):
    """Reuse the scanner's read-only evidence validator before starting a worker."""
    if (batch.get('state') != 'failed' or batch.get('error') != SEED_REJECTION_ERROR
            or batch.get('current_chip') is not None):
        raise ValueError('Recovery requires the exact saved seed-classification failure with no active chip.')
    from flakefinder.chip_campaign import _verify_legacy_seed_rejection
    from flakefinder.config import load_config, transform_from_config
    from flakefinder.imaging import load_frame
    cfg = load_config(config)
    return _verify_legacy_seed_rejection(root, batch, cfg, load_frame(batch['reference_path']), transform_from_config(cfg))


def archive_previous_report(root):
    try:
        previous = (root / 'overnight.json').read_bytes()
    except FileNotFoundError:
        return None
    folder = root / 'overnight-history'
    folder.mkdir(exist_ok=True)
    destination = folder / (str(time.time_ns()) + '-' + hashlib.sha256(previous).hexdigest()[:12] + '.json')
    with destination.open('xb') as stream:
        stream.write(previous)
    return str(destination)


def continuation_command(root, config, max_chips, hours, *, recover_seed_rejection=False, continue_saved=False):
    if recover_seed_rejection and continue_saved:
        raise ValueError('Choose seed recovery or saved continuation.')
    if (recover_seed_rejection or continue_saved) and (max_chips is not None or hours is not None):
        raise ValueError('Saved continuation preserves the saved budget; omit max-chips and hours.')
    batch = read_batch(root)
    command = [sys.executable, str(WORKSPACE / 'scripts/scan_chip_campaign.py'), 'run',
            '--config', str(config), '--reference', batch['reference_path'], '--output', str(root),
            '--bounds', *[str(v) for v in batch['search_bounds_um']],
            '--continue-discovery', '--allow-source-change']
    if recover_seed_rejection:
        return command + ['--recover-seed-rejection']
    if continue_saved:
        return command
    return command + ['--minimum-chip-diameter-um', str(batch['options']['minimum_chip_diameter_um']),
                      '--continuation-max-chips', str(max_chips), '--continuation-hours', str(hours)]


def keep_system_awake(enable):
    if os.name != 'nt':
        return False
    import ctypes
    call = ctypes.windll.kernel32.SetThreadExecutionState
    call.argtypes = [ctypes.c_uint]
    call.restype = ctypes.c_uint
    if not call(0x80000000 | (0x00000001 if enable else 0)):
        raise OSError('Could not set the temporary Windows execution-state request.')
    return bool(enable)


class ProcessWatcher:
    """Hold the original Windows process handle rather than trusting PID reuse."""
    def __init__(self, pid=None):
        self.pid, self.handle = pid, None
        if pid is not None and os.name == 'nt':
            import ctypes
            from ctypes import wintypes
            self.kernel = ctypes.WinDLL('kernel32', use_last_error=True)
            self.kernel.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
            self.kernel.OpenProcess.restype = wintypes.HANDLE
            self.kernel.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
            self.kernel.WaitForSingleObject.restype = wintypes.DWORD
            self.kernel.CloseHandle.argtypes = [wintypes.HANDLE]
            self.handle = self.kernel.OpenProcess(0x00100000, False, pid)  # SYNCHRONIZE only
            if not self.handle and ctypes.get_last_error() != 87:  # PID already exited
                raise ctypes.WinError(ctypes.get_last_error())

    def alive(self):
        if self.pid is None:
            return False
        if os.name == 'nt':
            if not self.handle:
                return False
            result = self.kernel.WaitForSingleObject(self.handle, 0)
            if result not in (0, 258):
                raise OSError('Could not determine preceding worker process state.')
            return result == 258
        try:
            os.kill(self.pid, 0)
            return True
        except ProcessLookupError:
            return False

    def close(self):
        if self.handle:
            self.kernel.CloseHandle(self.handle)
            self.handle = None


def hardware_lease_available(config):
    """Filesystem lock only: no microscope construction or hardware/API call."""
    cfg = json.loads(config.read_text(encoding='utf-8-sig'))
    if cfg.get('backend') != 'zen':
        return True
    lease = HardwareLease(cfg.get('zen', {}).get('endpoint', 'localhost:5002'), WORKSPACE / 'data/locks')
    try:
        lease.acquire()
        return True
    except SafetyError:
        return False
    finally:
        lease.close()


def request_stop(root, reason):
    try:
        with (root / 'STOP').open('x', encoding='utf-8') as stream:
            stream.write(reason)
    except FileExistsError:
        pass  # Preserve the user's STOP explanation.


def stop_owned_child(child, root, reason, grace_seconds=120):
    """Cooperative cancellation, then bound a hung owned worker's lifetime."""
    if child.poll() is not None:
        return {'worker_stopped': True, 'forced_termination': False}
    request_stop(root, reason)
    try:
        child.wait(timeout=grace_seconds)
        return {'worker_stopped': True, 'forced_termination': False}
    except subprocess.TimeoutExpired:
        # A Windows venv redirector can own a second Python process. Killing
        # just the redirector would orphan the actual scanner.
        if os.name == 'nt':
            result = subprocess.run(['taskkill', '/PID', str(child.pid), '/T', '/F'],
                                    capture_output=True, text=True, timeout=15,
                                    creationflags=subprocess.CREATE_NO_WINDOW)
            if result.returncode and child.poll() is None:
                raise RuntimeError('Owned worker process-tree termination failed: ' + result.stderr.strip())
        else:
            child.kill()
        child.wait(timeout=15)
        return {'worker_stopped': True, 'forced_termination': True,
                'hardware_state_verified': False,
                'termination_note': 'Worker ignored STOP; process tree terminated. Hardware state needs review.'}


def terminal_outcome(batch, returncode, stop_present=False):
    state = batch.get('state')
    if returncode == 0 and state == 'stopped':
        return 'stopped'
    if returncode != 0 or batch.get('current_chip') or stop_present:
        return 'needs_review'
    if state in FINISHED_STATES:
        return 'acquisition_finished'
    if state in BOUNDED_STATES:
        return 'budget_limited'
    return 'needs_review'


def run_coordinator(root, config, max_chips, hours, *, wait_pid=None, poll_seconds=10,
                    stale_seconds=300, stop_grace_seconds=120, owner_timeout_seconds=21600,
                    recover_seed_rejection=False, continue_saved=False):
    initial = read_batch(root)
    identity = campaign_identity(initial)
    config_hash = hashlib.sha256(config.read_bytes()).hexdigest()
    command = continuation_command(root, config, max_chips, hours, recover_seed_rejection=recover_seed_rejection, continue_saved=continue_saved)
    recovery = verify_seed_rejection(root, config, initial) if recover_seed_rejection else None
    if continue_saved and (initial.get('current_chip') is not None or initial.get('state') not in
                          ('stopped', 'chip_complete_discovery_pending', 'time_budget_reached', 'storage_budget_reached')):
        raise ValueError('Saved continuation requires a paused discovery with no active chip; failed runs require separate review.')
    preserve_budget = recover_seed_rejection or continue_saved
    lease = HardwareLease('overnight-coordinator:' + os.path.normcase(str(root)), root / 'control-locks')
    lease.acquire()
    try:
        previous_report = archive_previous_report(root)
    except BaseException:
        lease.close()
        raise  # Never overwrite a prior report if preserving it failed.
    report = {'schema': 2, 'state': 'waiting_for_current_chip', 'pid': os.getpid(),
              'started_at': time.time(), 'config': str(config), 'config_sha256': config_hash,
              'campaign_identity': identity, 'command': command, 'preceding_worker_pid': wait_pid,
              'maximum_total_chips': initial['options']['max_chips'] if preserve_budget else max_chips,
              'additional_active_hours': 0 if preserve_budget else hours,
              'launch_mode': 'seed_rejection_recovery' if recovery else 'saved_continuation' if continue_saved else 'normal_continuation',
              'previous_report_archive': previous_report,
              'seed_rejection_recovery_evidence': recovery,
              'remaining_saved_active_seconds': max(0, float(initial['options'].get('walltime_budget_s', 0)) - float(initial.get('elapsed_seconds', 0))),
              'expected_chip_count': None, 'idle_sleep_inhibited': False,
              'stop_policy': 'Root STOP prevents continuation; failures are never retried automatically.',
              'budget_policy': 'Stop starting chips at active-time budget; finish the current chip.'}

    def save(**updates):
        report.update(**updates, updated_at=time.time())
        atomic_json(root / 'overnight.json', report)

    def current_batch():
        batch = read_batch(root)
        if campaign_identity(batch) != identity:
            raise ValueError('Campaign coordinate/reference identity changed while coordinator was active.')
        return batch

    child = watcher = None
    awake = False
    try:
        watcher = ProcessWatcher(wait_pid)
        awake = keep_system_awake(True)
        save(idle_sleep_inhibited=awake)
        waiting_started = time.monotonic()
        deadline = waiting_started + owner_timeout_seconds
        while True:
            if (root / 'STOP').exists():
                save(state='stopped', reason='Root STOP is present.')
                return 0
            batch = current_batch()
            state = batch.get('state')
            if time.monotonic() > deadline:
                save(state='needs_review', reason='Initial owner wait expired; no acquisition started.')
                return 2
            if preserve_budget:
                # Recovery is an explicit one-shot launch from the exact evidence
                # validated above, never a watcher waiting for arbitrary failures.
                if batch != initial:
                    raise ValueError('Saved campaign changed while waiting for owner release.')
                if not watcher.alive() and hardware_lease_available(config):
                    break
                if time.monotonic() - waiting_started > stale_seconds:
                    save(state='needs_review', reason='Seed-recovery owner did not release acquisition.')
                    return 2
                save(phase='waiting_for_owner_release', campaign_state=state)
            elif state == 'chip_complete_discovery_pending':
                if batch.get('current_chip'):
                    raise ValueError('Completed-chip handoff still names an incomplete current chip.')
                if not watcher.alive() and hardware_lease_available(config):
                    break
                if checkpoint_age(batch) > stale_seconds:
                    save(state='needs_review', reason='Completed chip owner did not release acquisition; no restart attempted.')
                    return 2
                save(phase='waiting_for_owner_release', campaign_state=state)
            elif state != 'running':
                save(state='needs_review', reason='Current campaign did not complete its chip normally.', campaign_state=state)
                return 2
            elif (wait_pid is not None and not watcher.alive()) or checkpoint_age(batch) > stale_seconds:
                save(state='needs_review', reason='Current worker exited or its progress checkpoint is stale; no restart attempted.')
                return 2
            else:
                save(current_chip=batch.get('current_chip'), campaign_state=state)
            time.sleep(poll_seconds)

        if hashlib.sha256(config.read_bytes()).hexdigest() != config_hash:
            raise ValueError('Continuation configuration changed while waiting.')
        if (root / 'STOP').exists():
            save(state='stopped', reason='Root STOP is present.')
            return 0
        launch_batch = current_batch()
        if preserve_budget and launch_batch != initial:
            raise ValueError('Saved seed failure changed before recovery launch.')
        launch_checkpoint = json.dumps(launch_batch, sort_keys=True)
        awaiting_first_checkpoint = True
        with (root / 'overnight-acquisition.log').open('a', encoding='utf-8', buffering=1) as log:
            child = subprocess.Popen(command, cwd=WORKSPACE, stdout=log, stderr=subprocess.STDOUT,
                                     creationflags=subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0)
            launched = time.monotonic()
            save(state='scanning_remaining_chips', worker_pid=child.pid, continuation_started_at=time.time())
            while child.poll() is None:
                if (root / 'STOP').exists():
                    save(**stop_owned_child(child, root, 'Root STOP requested.', stop_grace_seconds))
                    break
                batch = current_batch()
                # Saved evidence replay precedes the first running checkpoint;
                # permit bounded preflight, rather than judging the old stamp.
                if json.dumps(batch, sort_keys=True) != launch_checkpoint:
                    awaiting_first_checkpoint = False
                stale = not awaiting_first_checkpoint and checkpoint_age(batch) > stale_seconds
                startup_stalled = awaiting_first_checkpoint and time.monotonic() - launched > max(900, stale_seconds)
                if stale or startup_stalled:
                    save(**stop_owned_child(child, root, 'Overnight worker progress stalled; orderly stop requested.', stop_grace_seconds),
                         state='needs_review', campaign_state=batch.get('state'),
                         reason='Continuation progress stalled; no restart attempted.')
                    return 2
                save(current_chip=batch.get('current_chip'), campaign_state=batch.get('state'),
                     completed_chips=sum(bool(c.get('planned_area_acquired')) for c in batch.get('chip_states', {}).values()))
                time.sleep(poll_seconds)
        batch = current_batch()
        outcome = terminal_outcome(batch, child.returncode, (root / 'STOP').exists())
        if report.get('forced_termination'):
            outcome = 'needs_review'
        save(state=outcome, returncode=child.returncode, campaign_state=batch.get('state'),
             finished_at=time.time(), sampling_complete=batch.get('sampling_complete', False),
             completed_chips=sum(bool(c.get('planned_area_acquired')) for c in batch.get('chip_states', {}).values()))
        return 2 if outcome == 'needs_review' else 0
    except BaseException as exc:
        save(state='needs_review', error_type=type(exc).__name__, reason=str(exc))
        raise
    finally:
        try:
            if child is not None and child.poll() is None:
                save(**stop_owned_child(child, root, 'Overnight coordinator exited; orderly stop requested.', stop_grace_seconds), state='needs_review')
        finally:
            try:
                if awake:
                    keep_system_awake(False)
                    awake = False
                    try:
                        save(idle_sleep_inhibited=False)
                    except Exception as exc:
                        # Reporting failure must not retain the ownership lock.
                        print('Could not update released sleep-guard status: ' + str(exc), file=sys.stderr)
            finally:
                if watcher is not None:
                    watcher.close()
                lease.close()


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--output', type=Path, required=True)
    p.add_argument('--config', type=Path, required=True)
    p.add_argument('--max-chips', type=int, help='Normal continuation total chip cap (default 64); forbidden during recovery')
    p.add_argument('--hours', type=float, help='Normal continuation additional active hours (default 9); forbidden during recovery')
    p.add_argument('--recover-seed-rejection', action='store_true', help='Recover only the verified historical seed-classification failure, preserving the saved budget')
    p.add_argument('--continue-saved', action='store_true', help='Continue a deliberately paused discovery without extending its saved budget')
    p.add_argument('--wait-pid', type=int, help='OS PID of the existing acquisition worker, not an exec session ID')
    p.add_argument('--dry-run', action='store_true')
    a = p.parse_args()
    from flakefinder.storage import resolve_output_path, assert_acquisition_allowed
    root, config = resolve_output_path(a.output), a.config.resolve()
    if not a.dry_run:
        assert_acquisition_allowed()
    if a.recover_seed_rejection and a.continue_saved:
        p.error('Choose --recover-seed-rejection or --continue-saved.')
    preserving = a.recover_seed_rejection or a.continue_saved
    if preserving:
        if a.max_chips is not None or a.hours is not None:
            p.error('Seed recovery preserves the saved budget; omit --max-chips and --hours.')
    else:
        a.max_chips = 64 if a.max_chips is None else a.max_chips
        a.hours = 9. if a.hours is None else a.hours
    if (not config.is_file() or (not preserving and (not 1 <= a.max_chips <= 256 or not 0 < a.hours <= 24))
            or (a.wait_pid is not None and a.wait_pid <= 0)):
        p.error('Need an existing configuration, 1..256 chip cap, 0..24 hour budget and positive optional PID.')
    if a.dry_run:
        if a.recover_seed_rejection:
            verify_seed_rejection(root, config, read_batch(root))
        print(json.dumps({'command': continuation_command(root, config, a.max_chips, a.hours, recover_seed_rejection=a.recover_seed_rejection, continue_saved=a.continue_saved),
                          'wait_for': ('verified seed rejection' if a.recover_seed_rejection else 'chip_complete_discovery_pending') + ' plus owner exit and hardware lease release',
                          'wait_pid': a.wait_pid, 'root_STOP_respected': True}, indent=2))
        return 0
    return run_coordinator(root, config, a.max_chips, a.hours, wait_pid=a.wait_pid, recover_seed_rejection=a.recover_seed_rejection, continue_saved=a.continue_saved)


if __name__ == '__main__':
    sys.exit(main())
