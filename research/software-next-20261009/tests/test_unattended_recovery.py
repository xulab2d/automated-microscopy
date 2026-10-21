"""Exercise transient recovery and durable failure reporting without hardware."""
import json
import pytest

from flakefinder import acquisition_resources as memory,scan_worker as worker
from flakefinder.model import AcquisitionError,Cancelled
from test_scan_worker import run_folder,FakeScope,profile,resources
from test_scan_service import service,setup


def test_capture_headroom_recovers_without_restarting_or_submitting_a_job():
    clock=[0.];reports=[];checks=[]
    def checker():
        checks.append(clock[0])
        if clock[0]<3:raise memory.MemoryHeadroomError('transient pressure')
        return {'available_bytes':2*1024**3}
    result=memory.wait_for_memory_headroom(lambda:None,lambda seconds:clock.__setitem__(0,clock[0]+seconds),
        checker=checker,clock=lambda:clock[0],report=lambda *args:reports.append(args))
    assert checks==[0,1,2,3] and result['memory_recovery']=='recovered'
    assert result['memory_wait_seconds']==3 and reports[-1][0]=='recovered'


def test_persistent_pressure_is_bounded_and_remains_an_error():
    clock=[0.];reports=[]
    def checker():raise memory.MemoryHeadroomError('no room')
    with pytest.raises(memory.MemoryHeadroomError,match='exhausted after 7.0s'):
        memory.wait_for_memory_headroom(lambda:None,lambda seconds:clock.__setitem__(0,clock[0]+seconds),
            max_wait_s=7,checker=checker,clock=lambda:clock[0],report=lambda *args:reports.append(args))
    assert clock[0]==7 and reports[-1][0]=='exhausted'


def test_operator_stop_or_deadline_interrupts_memory_wait():
    clock=[0.]
    def check():
        if clock[0]>=1:raise Cancelled('STOP or deadline')
    def checker():raise memory.MemoryHeadroomError('low')
    with pytest.raises(Cancelled):
        memory.wait_for_memory_headroom(check,lambda seconds:clock.__setitem__(0,clock[0]+seconds),
            checker=checker,clock=lambda:clock[0])
    assert clock[0]==1


def test_resource_read_failure_does_not_masquerade_as_recoverable_shortage():
    waits=[]
    def bad():raise AcquisitionError('memory read failed')
    with pytest.raises(AcquisitionError,match='memory read failed'):
        memory.wait_for_memory_headroom(lambda:None,waits.append,checker=bad)
    assert not waits


def test_watchdog_allows_brief_ram_pressure_but_preserves_deadline(tmp_path,monkeypatch):
    project=tmp_path/'project';run=project/'data'/'run';run.mkdir(parents=True)
    monkeypatch.setattr(worker,'WORKSPACE',tmp_path/'code');clock=[0.];ram=[40*1024**2]
    def reader(p):return {**resources(p),'available_ram_bytes':ram[0]}
    watch=worker.WorkerWatchdog(run,project,1000,lambda:None,reader,clock=lambda:clock[0])
    assert watch.tick() and not (run/'STOP').exists()
    clock[0]=20;ram[0]=2*1024**3
    assert watch.tick() and watch.memory_low_since is None
    clock[0]=30;ram[0]=40*1024**2;assert watch.tick()
    clock[0]=150;assert not watch.tick() and 'recovery exhausted' in watch.reason


def test_deadline_wins_even_while_memory_is_recovering(tmp_path,monkeypatch):
    project=tmp_path/'project';run=project/'data'/'run';run.mkdir(parents=True)
    monkeypatch.setattr(worker,'WORKSPACE',tmp_path/'code');clock=[0.]
    watch=worker.WorkerWatchdog(run,project,5,lambda:None,
        lambda p:{**resources(p),'available_ram_bytes':0},clock=lambda:clock[0])
    assert watch.tick();clock[0]=5
    assert not watch.tick() and 'deadline' in watch.reason


def test_failure_creates_open_repair_record_with_phase_and_traceback(run_folder):
    project,run=run_folder;owned=[]
    def factory(config):owned.append(FakeScope(config));return owned[-1]
    def broken(*args,**kwargs):
        kwargs['callback']('sparse_edge_probe',None,{'index':3})
        raise AcquisitionError('synthetic positioning rejection')
    result=worker.run_scan_worker(project,'fresh-01',scope_factory=factory,profile_loader=profile,
        campaign_runner=broken,hold_check=lambda:None,resource_reader=resources)
    issue=json.loads((run/'recovery-needed.json').read_text())
    assert result['state']=='failed' and result['repair_required'] and result['hardware_closed']
    assert issue['state']=='open' and issue['failed_phase']=='sparse_edge_probe'
    assert 'broken' in issue['traceback'] and issue['request_sha256']==result['request_sha256']
    assert owned[0].closed and not owned[0].armed


def test_capture_memory_recovery_restores_phase_and_keeps_same_owner(run_folder,monkeypatch):
    project,run=run_folder;owned=[]
    def factory(config):owned.append(FakeScope(config));return owned[-1]
    def fake_wait(check,wait,*,report,checker=None):
        check();report('waiting',0.,'transient');report('recovered',2.,None)
        return {'memory_recovery':'recovered','memory_wait_seconds':2.}
    monkeypatch.setattr(memory,'wait_for_memory_headroom',fake_wait)
    def campaign(*args,**kwargs):
        scope=kwargs['scope'];kwargs['callback']('raster',None,{'index':2})
        assert scope.capture_memory_check()['memory_recovery']=='recovered'
        assert worker.read_json(run/'scan-status.json')['phase']=='raster'
        assert scope.armed and scope is owned[0]
        return {'state':'chip_budget_reached','chip_states':{}}
    result=worker.run_scan_worker(project,'fresh-01',scope_factory=factory,profile_loader=profile,
        campaign_runner=campaign,hold_check=lambda:None,resource_reader=resources)
    assert result['state']=='chip_budget_reached' and len(owned)==1
    assert worker.read_json(run/'memory-recovery.json')['state']=='recovered'
    assert not (run/'recovery-needed.json').exists()


def test_cleanup_launch_is_bound_to_current_worker_and_hidden(tmp_path):
    import os,subprocess,time
    from types import SimpleNamespace as N
    from flakefinder.memory_cleanup import launch_memory_guard
    source=tmp_path/'source';(source/'scripts').mkdir(parents=True)
    (source/'scripts'/'watch_scan_memory.ps1').write_text('# offline fixture')
    (source/'scripts'/'watch_scan_memory.py').write_text('# offline fixture')
    (source/'scripts'/'native_memory_actions.py').write_text('# offline fixture')
    runtime=tmp_path/'pwsh.exe';runtime.write_text('not executable')
    run=tmp_path/'run';run.mkdir();calls=[]
    result=launch_memory_guard(run,time.time()+60,runtime=runtime,source=source,
        started_reader=lambda:'2026-10-10T14:44:28.7752023Z',
        launcher=lambda *a,**k:(calls.append((a,k)) or N(pid=99)))
    assert result['state']=='launched' and result['worker_pid']==os.getpid()
    args=calls[0][0][0]
    assert args[args.index('--worker')+1]==str(os.getpid())
    assert args[args.index('--started')+1]=='2026-10-10T14:44:28.7752023Z'
    assert '--powershell' not in args
    assert calls[0][1]['creationflags']==subprocess.CREATE_NO_WINDOW
    assert json.loads((run/'memory-guard-launch.json').read_text())['pid']==99


def test_missing_cleanup_runtime_is_reported_without_failing_acquisition(tmp_path):
    import time
    from flakefinder.memory_cleanup import launch_memory_guard
    result=launch_memory_guard(tmp_path,time.time()+60,source=tmp_path/'missing-source')
    assert result['state']=='unavailable' and result['error']
    assert (tmp_path/'memory-guard-launch.json').is_file()


def test_failed_run_remains_an_open_repair_in_service_snapshot(service):
    svc,_,_,_=service
    run=svc.plan(setup())['run'];folder=svc.folder(run['id'])
    worker.atomic_json(folder/'scan-status.json',{'state':'failed','hardware_closed':True,'error':'move failed'})
    status=svc.run_status(run['id'])
    assert status['supervision']['state']=='repair_required' and not status['supervision']['resolved']
    assert status['counts']['raster_images_saved']==0


def test_lively_mapping_with_no_rasters_is_not_healthy_progress(service):
    svc,_,_,_=service
    run=svc.plan(setup())['run'];folder=svc.folder(run['id'])
    worker.atomic_json(folder/'scan-status.json',{'state':'running','pid':12345})
    worker.atomic_json(folder/'worker-start.json',{'pid':12345})
    worker.atomic_json(folder/'batch.json',{'chip_states':{},'boundary_attempts':[{'state':'unresolved'}]*3})
    status=svc.run_status(run['id'])
    assert status['supervision']['state']=='mapping_not_progressing'
    assert status['state']=='running'  # Reporting does not stop or change hardware.
