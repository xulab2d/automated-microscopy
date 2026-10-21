import json
from pathlib import Path
from types import SimpleNamespace
import pytest

from flakefinder import analysis_lifecycle as workflow

RUN='scan-20261014-180000-1234abcd'


@pytest.fixture
def queued(tmp_path,monkeypatch):
    run=tmp_path/'data'/RUN
    workflow.save(run/'scan-request.json',{'id':RUN,'settings':{'analysis_after_scan':True,'material':'graphene','oxide_nm':90,'substrate':'SiO2/Si'}})
    monkeypatch.setattr(workflow,'source_files',lambda:{})
    monkeypatch.setattr(workflow,'process_alive',lambda pid:False)
    from flakefinder import acquisition_resources
    monkeypatch.setattr(acquisition_resources,'available_memory_bytes',lambda:4*1024**3)
    workflow.enqueue(tmp_path,RUN)
    workflow.save(run/'scan-status.json',{'state':'stopped','phase':'finished','hardware_closed':True,'pid':123})
    workflow.save(run/'worker-start.json',{'pid':123,'request_sha256':workflow.digest(run/'scan-request.json')})
    for chip in ('chip001','chip002'):
        workflow.save(run/chip/'manifest.json',{'completed':{'tile_000001':{'hashes':{'tiles/tile_000001.tiff':'a'*64}}}})
    workflow.save(run/'STOP',{'reason':'Acquisition STOP stays intact'})
    return tmp_path,run


def executor(project,run,calls,paused_once=False):
    def launch(command,**kwargs):
        script=Path(command[1]).name;args=dict(zip(command[2::2],command[3::2]));calls.append(command)
        if script=='analyze_graphene_run.py':
            chip=args['--chip'];output=project/'analysis/graphene'/RUN/chip/args['--version']
            workflow.save(output/'plan.json',{'pin':{'identity':{'run_id':RUN,'scan_request_sha256':workflow.digest(run/'scan-request.json')},
                'chip':chip,'manifest_sha256':workflow.digest(run/chip/'manifest.json')}})
            workflow.save(output/'ledger.json',{})
            workflow.save(output/'status.json',{'state':'resource_paused' if paused_once and len(calls)==1 else 'complete','finished_fields':1,'counts':{'assessed':1}})
        elif script=='refine_graphene_regions.py':
            source=Path(args['--analysis']);output=source.parent/args['--version']
            workflow.save(output/'plan.json',{'run_id':RUN,'source_plan_sha256':workflow.digest(source/'plan.json')})
            workflow.save(output/'status.json',{'state':'complete','fields_complete':1})
        else:
            output=project/'analysis/graphene'/RUN/args['--chip']/args['--version']
            workflow.save(output/'dedup.json',{'groups':[]})
            workflow.save(output/'dedup-progress.json',{'state':'complete','observation_count':1})
        return SimpleNamespace(pid=99999999,poll=lambda:0,returncode=0)
    return launch


def test_waits_for_closed_owner_and_process_absence(queued):
    root,run=queued;plan=workflow.read(run/'offline-workflow/plan.json')
    assert not workflow.acquisition_closed(run,plan,lambda pid:True)
    assert workflow.acquisition_closed(run,plan,lambda pid:False)
    state=workflow.read(run/'scan-status.json');state['hardware_closed']=False;workflow.save(run/'scan-status.json',state)
    assert not workflow.acquisition_closed(run,plan,lambda pid:False)
    state['hardware_closed']=True;workflow.save(run/'scan-status.json',state)
    workflow.save(run/'worker-start.json',{'pid':123,'request_sha256':'wrong'})
    with pytest.raises(ValueError,match='receipt'):workflow.acquisition_closed(run,plan,lambda pid:False)


def test_serial_pipeline_publishes_every_chip_and_resumes_without_reprocessing(queued):
    root,run=queued;calls=[];stop=(run/'STOP').read_bytes()
    state=workflow.run_job(root,RUN,alive=lambda pid:False,sleep=lambda seconds:None,popen=executor(root,run,calls,True))
    assert state['state']=='complete' and len(calls)==7
    assert (run/'STOP').read_bytes()==stop
    for chip in ('chip001','chip002'):
        selection=workflow.read(run/f'graphene-analysis-selection-{chip}.json')
        assert '/'+chip+'/' in selection['analysis_relative_path']
        review=workflow.read(run/f'graphene-review-selection-{chip}.json')
        assert review['plan_sha256']==selection['plan_sha256'] and review['refinement']['kind']=='stream'
        assert state['chips'][chip]['review_url'].endswith('&chip='+chip)
    assert '/chip001/' in workflow.read(run/'graphene-analysis-selection.json')['analysis_relative_path']
    second=workflow.run_job(root,RUN,alive=lambda pid:False,sleep=lambda seconds:None,popen=lambda *a,**k:pytest.fail('completed stages must not rerun'))
    assert second['state']=='complete'


def test_independent_stop_and_changed_request_fail_without_launch(queued):
    root,run=queued;job=run/'offline-workflow'
    workflow.save(job/'STOP',{'reason':'offline only'})
    state=workflow.run_job(root,RUN,alive=lambda pid:False,popen=lambda *a,**k:pytest.fail('STOP'))
    assert state['state']=='stopped'
    request=workflow.read(run/'scan-request.json');request['changed']=True;workflow.save(run/'scan-request.json',request)
    with pytest.raises(ValueError,match='changed'):workflow.enqueue(root,RUN)
    assert workflow.snapshot(root,RUN)['state']=='failed'


def test_source_change_and_closed_manifest_change_fail(queued):
    root,run=queued;job=run/'offline-workflow';plan=workflow.read(job/'plan.json')
    plan['source_sha256']={'flakefinder/analysis_lifecycle.py':'0'*64};workflow.save(job/'plan.json',plan)
    result=workflow.run_job(root,RUN,alive=lambda pid:False,popen=lambda *a,**k:pytest.fail('changed code'))
    assert result['state']=='failed' and 'software changed' in result['reason']
    workflow.pin_inputs(run,job)
    workflow.save(run/'chip001/manifest.json',{'completed':{'tile_000002':{}}})
    with pytest.raises(ValueError,match='manifests changed'):workflow.pin_inputs(run,job)


def test_recovery_only_known_queue_and_no_second_live_supervisor(queued,monkeypatch):
    root,run=queued;calls=[]
    def launch(*args,**kwargs):calls.append(args);return SimpleNamespace(pid=12345)
    workflow.ensure_worker(root,RUN,launcher=launch)
    assert len(calls)==1 and '-m' in calls[0][0] and 'flakefinder.analysis_lifecycle' in calls[0][0]
    monkeypatch.setattr(workflow,'process_alive',lambda pid:pid==12345)
    workflow.ensure_worker(root,RUN,launcher=lambda *a,**k:pytest.fail('duplicate owner'))
    other=root/'data'/'scan-20261014-180001-1234abce'
    workflow.save(other/'scan-request.json',{'id':other.name,'settings':{'analysis_after_scan':True}})
    assert len(workflow.recover_queued(root))==1


def test_capabilities_do_not_promise_unsupported_models():
    assert workflow.capability({'material':'graphene','oxide_nm':90})['supported']
    for material,oxide in [('graphene',285),('MoTe2',90),('hBN',90)]:
        assert not workflow.capability({'material':material,'oxide_nm':oxide})['supported']


def test_previous_child_is_waited_out_before_resuming(queued):
    root,run=queued;calls=[];waited=[]
    workflow.save(run/'offline-workflow/child.json',{'pid':555,'stage':'analysis'})
    def alive(pid):return pid==555 and not waited
    result=workflow.run_job(root,RUN,alive=alive,sleep=lambda seconds:waited.append(seconds),popen=executor(root,run,calls))
    assert waited and result['state']=='complete' and len(calls)==6


def test_transient_read_retry_is_bounded_and_cancelable(tmp_path,monkeypatch):
    import errno
    calls=[];monkeypatch.setattr(workflow.time,'sleep',lambda _:None)
    def action():
        calls.append(1)
        if len(calls)<3:raise OSError(errno.EINVAL,'provider transient')
        return b'original bytes'
    assert workflow.retry_read(action)==b'original bytes' and len(calls)==3
    with pytest.raises(InterruptedError):workflow.retry_read(action,lambda:(_ for _ in ()).throw(InterruptedError()))
    path=tmp_path/'saved.json';workflow.save(path,{'value':1});assert workflow.read(path)=={'value':1}
    path.unlink()
    with pytest.raises(FileNotFoundError):workflow.read(path)


def test_recovered_stage_stop_is_forwarded_without_touching_acquisition(queued):
    root,run=queued;job=run/'offline-workflow';workflow.save(job/'child.json',{'pid':555,'stage':'refinement','chip':'chip001'})
    workflow.save(job/'STOP',{'reason':'offline stop'})
    # The initial STOP is handled before recovery; simulate it arriving once
    # the previous child is observed alive, as after a supervisor crash.
    (job/'STOP').unlink()
    def alive(pid):
        if pid==555:workflow.save(job/'STOP',{'reason':'offline stop'});return True
        return False
    result=workflow.run_job(root,RUN,alive=alive,popen=lambda *a,**k:pytest.fail('must not launch'))
    assert result['state']=='stopped'
    assert (root/'analysis/graphene'/RUN/'chip001/auto-boundaries-v1/STOP').exists()
    assert workflow.read(run/'STOP')['reason']=='Acquisition STOP stays intact'
