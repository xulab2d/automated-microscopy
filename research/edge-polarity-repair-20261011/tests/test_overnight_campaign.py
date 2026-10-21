"""Unattended coordinator tests: files and fake workers only, no hardware."""
import json
import subprocess
import time
import pytest
import scripts.run_overnight_campaign as overnight


@pytest.fixture
def setup(tmp_path, monkeypatch):
    config = tmp_path / 'config.json'
    config.write_text('{"backend":"simulator"}', encoding='utf-8')
    batch = {'coordinate_frame_id': 'fresh-xy', 'reference_path': 'reference.tiff',
             'reference_sha256': 'abc', 'search_bounds_um': [0, 100, 0, 100],
             'options': {'minimum_chip_diameter_um': 20}, 'chip_states': {},
             'state': 'chip_complete_discovery_pending', 'current_chip': None,
             'updated_at': time.time()}
    events = []
    def write(**changes):
        batch.update(changes)
        (tmp_path / 'batch.json').write_text(json.dumps(batch), encoding='utf-8')
    write()
    class Lease:
        def __init__(self, *args): pass
        def acquire(self): events.append('lease_acquire')
        def close(self): events.append('lease_close')
    class Watcher:
        def __init__(self, pid): events.append(('watch_pid', pid))
        def alive(self): return False
        def close(self): events.append('watch_close')
    monkeypatch.setattr(overnight, 'HardwareLease', Lease)
    monkeypatch.setattr(overnight, 'ProcessWatcher', Watcher)
    monkeypatch.setattr(overnight, 'hardware_lease_available', lambda config: True)
    monkeypatch.setattr(overnight, 'keep_system_awake', lambda enabled: events.append(('awake', enabled)) or enabled)
    monkeypatch.setattr(overnight.time, 'sleep', lambda duration: events.append('sleep'))
    def unexpected_spawn(*args, **kwargs): raise AssertionError('Must not start acquisition')
    monkeypatch.setattr(overnight.subprocess, 'Popen', unexpected_spawn)
    return tmp_path, config, batch, events, write


@pytest.mark.parametrize('state,code,expected', [
    ('search_sampling_complete', 0, 'acquisition_finished'),
    ('chip_budget_reached', 0, 'acquisition_finished'),
    ('time_budget_reached', 0, 'budget_limited'),
    ('storage_budget_reached', 0, 'budget_limited'),
    ('stopped', 0, 'stopped'), ('failed', 0, 'needs_review'),
    ('unrecognized', 0, 'needs_review'), ('running', 0, 'needs_review'),
    ('search_sampling_complete', 1, 'needs_review'),
])
def test_terminal_allowlist(state, code, expected):
    assert overnight.terminal_outcome({'state': state}, code) == expected
    assert overnight.terminal_outcome({'state': 'search_sampling_complete', 'current_chip': 'chip1'}, 0) == 'needs_review'


def test_stop_during_wait_never_starts_worker(setup):
    root, config, batch, events, write = setup
    (root / 'STOP').write_text('Manual stop', encoding='utf-8')
    assert overnight.run_coordinator(root, config, 64, 9, wait_pid=30328) == 0
    assert json.loads((root / 'overnight.json').read_text())['state'] == 'stopped'
    assert not json.loads((root / 'overnight.json').read_text())['idle_sleep_inhibited']
    assert events[-1] == 'lease_close' and ('awake', False) in events


def test_exited_or_stale_running_owner_is_not_restarted(setup):
    root, config, batch, events, write = setup
    write(state='running', updated_at=time.time()-600)
    assert overnight.run_coordinator(root, config, 64, 9, wait_pid=30328) == 2
    assert json.loads((root / 'overnight.json').read_text())['state'] == 'needs_review'


def test_handoff_requires_original_process_exit_and_lease_release(setup, monkeypatch):
    root, config, batch, events, write = setup
    alive = iter([True, False, False])
    free = iter([False, True])
    monkeypatch.setattr(overnight.ProcessWatcher, 'alive', lambda self: next(alive))
    monkeypatch.setattr(overnight, 'hardware_lease_available', lambda config: next(free))
    class Child:
        pid = 123
        returncode = 0
        def poll(self):
            write(state='search_sampling_complete', sampling_complete=True)
            return 0
    def spawn(command, **kwargs):
        assert events.count('sleep') == 2
        assert '--continue-discovery' in command and '--continuation-hours' in command
        return Child()
    monkeypatch.setattr(overnight.subprocess, 'Popen', spawn)
    assert overnight.run_coordinator(root, config, 64, 9, wait_pid=30328) == 0
    assert json.loads((root / 'overnight.json').read_text())['state'] == 'acquisition_finished'


def test_stale_owned_worker_gets_cooperative_stop_and_no_restart(setup, monkeypatch):
    root, config, batch, events, write = setup
    class Child:
        pid = 123
        returncode = None
        def poll(self): return self.returncode
        def wait(self, timeout):
            assert (root / 'STOP').exists()
            events.append('child_wait')
            self.returncode = 0
            write(state='stopped')
            return 0
    def spawn(*args, **kwargs):
        write(state='running', updated_at=time.time()-600)
        return Child()
    monkeypatch.setattr(overnight.subprocess, 'Popen', spawn)
    assert overnight.run_coordinator(root, config, 64, 9) == 2
    assert 'child_wait' in events
    assert json.loads((root / 'overnight.json').read_text())['state'] == 'needs_review'


def test_changed_campaign_after_spawn_stops_owned_child(setup, monkeypatch):
    root, config, batch, events, write = setup
    class Child:
        pid = 123
        returncode = None
        def poll(self): return self.returncode
        def wait(self, timeout):
            assert (root / 'STOP').exists()
            self.returncode = 0
            events.append('child_wait')
    def spawn(*args, **kwargs):
        write(coordinate_frame_id='different-origin')
        return Child()
    monkeypatch.setattr(overnight.subprocess, 'Popen', spawn)
    with pytest.raises(ValueError, match='identity changed'):
        overnight.run_coordinator(root, config, 64, 9)
    assert 'child_wait' in events and events[-1] == 'lease_close'


def test_sleep_guard_reset_failure_still_releases_coordinator_lease(setup, monkeypatch):
    root, config, batch, events, write = setup
    (root / 'STOP').touch()
    def awake(enable):
        if not enable: raise OSError('reset failed')
        return True
    monkeypatch.setattr(overnight, 'keep_system_awake', awake)
    with pytest.raises(OSError, match='reset failed'):
        overnight.run_coordinator(root, config, 64, 9)
    assert events[-1] == 'lease_close'


def test_hung_owned_worker_terminates_only_its_tree_and_preserves_stop(tmp_path, monkeypatch):
    (tmp_path / 'STOP').write_text('User explanation', encoding='utf-8')
    commands = []
    class Child:
        pid = 4321
        returncode = None
        def poll(self): return self.returncode
        def wait(self, timeout):
            if self.returncode is None: raise subprocess.TimeoutExpired('owned', timeout)
            return self.returncode
        def kill(self): self.returncode = 1
    child = Child()
    def taskkill(command, **kwargs):
        commands.append(command)
        child.returncode = 1
        return subprocess.CompletedProcess(command, 0, '', '')
    monkeypatch.setattr(overnight.subprocess, 'run', taskkill)
    result = overnight.stop_owned_child(child, tmp_path, 'Coordinator reason', 0)
    assert result['forced_termination'] and not result['hardware_state_verified']
    if overnight.os.name == 'nt': assert commands == [['taskkill', '/PID', '4321', '/T', '/F']]
    assert (tmp_path / 'STOP').read_text() == 'User explanation'


def test_seed_recovery_command_preserves_budget_and_rejects_overrides(setup):
    root, config, batch, events, write = setup
    command = overnight.continuation_command(root, config, None, None, recover_seed_rejection=True)
    assert '--recover-seed-rejection' in command and '--allow-source-change' in command
    assert '--continuation-max-chips' not in command and '--continuation-hours' not in command
    assert '--minimum-chip-diameter-um' not in command
    with pytest.raises(ValueError, match='preserves the saved budget'):
        overnight.continuation_command(root, config, 64, 9, recover_seed_rejection=True)


def test_saved_pause_continues_without_budget_renewal(setup,monkeypatch):
    root,config,batch,events,write=setup
    options={'minimum_chip_diameter_um':20,'max_chips':64,'walltime_budget_s':35780}
    write(state='stopped',options=options,elapsed_seconds=4000)
    class Child:
        pid=124
        returncode=0
        def poll(self):
            write(state='search_sampling_complete')
            return 0
    def spawn(command,**kwargs):
        assert '--continue-discovery' in command
        assert '--continuation-hours' not in command and '--continuation-max-chips' not in command
        assert '--recover-seed-rejection' not in command
        return Child()
    monkeypatch.setattr(overnight.subprocess,'Popen',spawn)
    assert overnight.run_coordinator(root,config,None,None,continue_saved=True)==0
    report=json.loads((root/'overnight.json').read_text())
    assert report['additional_active_hours']==0 and report['launch_mode']=='saved_continuation'
    assert batch['options']==options


def test_saved_pause_cannot_recover_failures_or_extend_budget(setup):
    root,config,batch,events,write=setup
    write(state='failed',error='motion timeout')
    with pytest.raises(ValueError,match='paused discovery'):
        overnight.run_coordinator(root,config,None,None,continue_saved=True)
    with pytest.raises(ValueError,match='preserves the saved budget'):
        overnight.continuation_command(root,config,64,9,continue_saved=True)


def test_recovery_does_not_accept_other_failures_or_active_chips(setup):
    root, config, batch, events, write = setup
    for error, current in [('Motion timeout', None), (overnight.SEED_REJECTION_ERROR, 'chip1')]:
        write(state='failed', error=error, current_chip=current)
        with pytest.raises(ValueError, match='exact saved seed-classification'):
            overnight.run_coordinator(root, config, None, None, recover_seed_rejection=True)
    # The normal waiting mode still refuses even the exact historical failure.
    write(state='failed', error=overnight.SEED_REJECTION_ERROR, current_chip=None)
    assert overnight.run_coordinator(root, config, 64, 9) == 2


def test_verified_recovery_archives_report_and_allows_old_preflight_checkpoint(setup, monkeypatch):
    root, config, batch, events, write = setup
    options = {'minimum_chip_diameter_um': 20, 'max_chips': 64, 'walltime_budget_s': 35780}
    write(state='failed', error=overnight.SEED_REJECTION_ERROR, updated_at=time.time()-3600,
          options=options, elapsed_seconds=3841)
    previous = b'{"state":"needs_review","worker_pid":29776}\n'
    (root / 'overnight.json').write_bytes(previous)
    def verify(*args):
        events.append('evidence_verified')
        return {'attempt_id': 'candidate003', 'verified_seed_hashes': {'seed.tiff': 'abc'}}
    monkeypatch.setattr(overnight, 'verify_seed_rejection', verify)
    class Child:
        pid = 123
        returncode = None
        count = 0
        def poll(self):
            self.count += 1
            if self.count == 2:
                self.returncode = 0
                write(state='search_sampling_complete')
            return self.returncode
    def spawn(command, **kwargs):
        assert 'evidence_verified' in events
        assert '--recover-seed-rejection' in command
        assert '--continuation-hours' not in command and '--continuation-max-chips' not in command
        return Child()
    monkeypatch.setattr(overnight.subprocess, 'Popen', spawn)
    assert overnight.run_coordinator(root, config, None, None, recover_seed_rejection=True) == 0
    report = json.loads((root / 'overnight.json').read_text())
    assert report['state'] == 'acquisition_finished' and report['additional_active_hours'] == 0
    assert report['maximum_total_chips'] == 64 and report['remaining_saved_active_seconds'] == 31939
    assert overnight.Path(report['previous_report_archive']).read_bytes() == previous
    assert batch['options'] == options and batch['elapsed_seconds'] == 3841
    assert not (root / 'STOP').exists()
