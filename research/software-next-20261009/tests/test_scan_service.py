import hashlib
import json
import os
import time
from types import SimpleNamespace
from pathlib import Path
from urllib.request import Request, urlopen
from urllib.error import HTTPError
import pytest

from flakefinder import storage
from flakefinder.scan_service import ScanService, DEFAULTS, GIB, write_json, validate_settings


@pytest.fixture
def service(tmp_path, monkeypatch):
    project = tmp_path/'project'; project.mkdir()
    workspace = tmp_path/'workspace'; workspace.mkdir()
    policy = dict(schema=1,mode='drive_required',project_id='scan-test',project_root=str(project))
    write_json(project/'.flakefinder-project.json',dict(project_id='scan-test'))
    write_json(workspace/'OPERATOR-HOLD.json',dict(acquisition_authorized=False,reason='Operator working'))
    monkeypatch.setattr(storage,'WORKSPACE',workspace)
    monkeypatch.setattr(storage,'load_policy',lambda:policy)
    monkeypatch.delenv('FLAKEFINDER_RUN_AUTHORIZATION',raising=False)
    resources = dict(project_root=str(project),storage_ok=True,ram_available_bytes=4*GIB,ram_total_bytes=16*GIB,
                     cache_free_bytes=80*GIB,project_free_bytes=80*GIB,reserve_bytes=10*GIB,error=None)
    calls = []
    def launch(*args,**kwargs): calls.append((args,kwargs)); return SimpleNamespace(pid=12345)
    result = ScanService(project,resource_reader=lambda:resources,launcher=launch)
    monkeypatch.setattr('flakefinder.scan_service.process_alive',lambda pid:pid==12345)
    return result, resources, calls, workspace


def setup(**changes):
    return {**DEFAULTS,'sample_id':'Graphene 90 nm','width_mm':8,'height_mm':8,'operator_ready':True,**changes}


def test_exhausted_historical_search_reports_attention_without_rewriting_records(service):
    service,_,calls,_=service
    identifier=service.plan(setup(expected_chips=1))['run']['id'];folder=service.folder(identifier)
    write_json(folder/'scan-status.json',dict(state='search_sampling_complete',phase='finished',hardware_closed=True))
    write_json(folder/'batch.json',dict(state='search_sampling_complete',expected_chip_count=1,chip_states={},
        boundary_attempts=[{'state':'unresolved'} for _ in range(9)]))
    original={name:(folder/name).read_bytes() for name in ('scan-status.json','batch.json')}
    result=service.run_status(identifier)
    assert result['state']=='needs_attention' and result['termination_reason']=='search_sampling_complete'
    assert result['expected_completed_chip_count_remaining']==1 and result['discovery_unresolved_count']==9
    assert result['requires_attention'] and result['supervision']['resolved'] is False
    assert '9 boundary attempts' in result['supervision']['message']
    assert all((folder/name).read_bytes()==blob for name,blob in original.items()) and not calls


def test_storage_forecast_stop_explains_actual_reason_without_changing_history(service):
    service,_,calls,_=service
    identifier=service.plan(setup(expected_chips=1))['run']['id'];folder=service.folder(identifier)
    write_json(folder/'scan-status.json',dict(state='storage_budget_reached',phase='finished',hardware_closed=True))
    write_json(folder/'batch.json',dict(state='storage_budget_reached',phase='next_chip_exceeds_free_storage',
        chip_states={},boundary_attempts=[dict(state='deferred_storage_budget',storage_preflight=dict(volumes=[
            dict(sufficient=False,free_bytes=120*GIB,required_free_bytes=212*GIB)]))]))
    original=(folder/'scan-status.json').read_bytes()
    result=service.run_status(identifier)
    assert result['state']=='storage_budget_reached' and not result['supervision']['resolved']
    assert '212.0 GiB estimated' in result['supervision']['message']
    assert '120.0 GiB free' in result['supervision']['message']
    assert 'not a failed disk write' in result['supervision']['message']
    assert (folder/'scan-status.json').read_bytes()==original and not calls


def test_historical_campaign_invocation_displays_without_becoming_startable(service):
    service,_,calls,_=service
    parent=service.plan(setup())['run'];parent_folder=service.folder(parent['id'])
    child='scan-20261011-162824-1400155a';folder=service.folder(child)
    request=dict(schema=1,id=child,kind='existing_campaign_storage_continuation_v1',
        campaign_output=str(parent_folder),
        parent_request_sha256=hashlib.sha256((parent_folder/'scan-request.json').read_bytes()).hexdigest())
    write_json(folder/'scan-request.json',request)
    write_json(folder/'scan-status.json',dict(state='stopped',hardware_closed=True))
    write_json(service.control/'current.json',dict(id=child))
    snap=service.snapshot()
    assert snap['run']['id']==child and snap['run']['state']=='stopped'
    assert snap['run']['settings']==setup() and not snap['defaults']['operator_ready']
    assert snap['run']['campaign_output_rel']==parent['output_rel']
    assert any(r.get('settings')==setup() for r in snap['recent_runs'])
    with pytest.raises((ValueError,KeyError)):
        service.start(child,release_hold=True)
    assert not calls
    write_json(parent_folder/'batch.json',dict(chip_states={'chip001':dict(state='stopped',completed_tiles=2,total_tiles=10)}))
    assert service.run_status(child)['counts']['raster_images_saved']==2
    request['parent_request_sha256']='changed';write_json(folder/'scan-request.json',request)
    with pytest.raises(ValueError,match='changed'):service.run_status(child)


@pytest.mark.parametrize('state,age,alive,expected',[
    ('low_memory',2,True,'low_memory'),('monitoring',90,True,'stale'),
    ('monitoring',2,False,'exited'),('failed',2,False,'failed'),
    ('worker_exited_or_replaced',2,False,'worker_exited_or_replaced')])
def test_memory_cleanup_is_read_only_and_never_replaces_host_ram(service,monkeypatch,state,age,alive,expected):
    from datetime import datetime,timezone
    service,resources,calls,_=service
    run=service.plan(setup())['run'];run.update(state='running',pid=12345,process_alive=True)
    folder=service.folder(run['id']);path=folder/'memory-guard/status.json'
    record=dict(schema=1,run_id=run['id'],worker_pid=12345,guard_pid=23456,state=state,
        updated_at=datetime.fromtimestamp(time.time()-age,timezone.utc).isoformat(),
        poll_seconds=10,actions=3,access_denials=0,available_bytes=123)
    write_json(path,record)
    write_json(folder/'memory-guard/events.json',[dict(kind='stopped',detail=dict(name='Widgets'))])
    monkeypatch.setattr('flakefinder.scan_continuation.process_alive_strict',lambda pid:alive)
    before=path.read_bytes();result=service.memory_cleanup_status(run)
    assert result['display_state']==expected and result['actions']==3 and result['closed_apps']==['Widgets']
    assert result['active']==(expected=='low_memory')
    assert resources['ram_available_bytes']==4*GIB and 'available_bytes' not in result
    assert path.read_bytes()==before and not calls
    run['state']='stopped'
    if state in ('monitoring','low_memory'):assert service.memory_cleanup_status(run)['display_state']=='inactive'


def test_memory_cleanup_identity_unavailable_and_snapshot_integration(service,monkeypatch):
    from datetime import datetime,timezone
    service,resources,_,_=service
    run=service.plan(setup())['run'];folder=service.folder(run['id'])
    assert service.memory_cleanup_status(run) is None
    write_json(folder/'scan-status.json',dict(state='running',pid=12345))
    write_json(folder/'worker-start.json',dict(pid=12345))
    record=dict(run_id=run['id'],worker_pid=12345,guard_pid=23456,state='monitoring',actions=3,
        updated_at=datetime.now(timezone.utc).isoformat())
    write_json(folder/'memory-guard/status.json',record)
    monkeypatch.setattr('flakefinder.scan_continuation.process_alive_strict',lambda pid:True)
    snap=service.snapshot()
    assert snap['resources']['memory_cleanup']['active'] and snap['resources']['ram_available_bytes']==4*GIB
    record['run_id']='wrong';write_json(folder/'memory-guard/status.json',record)
    assert service.memory_cleanup_status(snap['run'])['display_state']=='unavailable'


def test_preflight_and_drafts_are_hardware_free_and_immutable(service):
    service, _, calls, workspace = service
    check = service.preflight(setup())
    assert check['ok'] and check['hold']['active']
    assert not (service.project/'data').exists() and not calls
    first = service.plan(setup())['run']; second = service.plan(setup(sample_id='Next sample'))['run']
    assert first['id'] != second['id'] and first['output_rel'] != second['output_rel']
    assert service.request(first['id'])['settings']['sample_id']=='Graphene 90 nm'
    assert service.snapshot()['defaults']['operator_ready'] is False
    assert not calls and json.loads((workspace/'OPERATOR-HOLD.json').read_text())['acquisition_authorized'] is False


def test_reference_reuse_is_verified_then_pinned_in_immutable_draft(service,monkeypatch):
    import flakefinder.retained_reference as retained
    service,_,calls,workspace=service
    contract={'schema':1,'donor_run_id':'scan-20261013-205538-00000000',
              'source_files_sha256':{'calibration/substrate.tiff':'a'*64}}
    original=json.loads(json.dumps(contract));verified=[]
    def verify(project,value):
        assert project==service.project and value==original
        assert not (project/'data').exists()
        verified.append(value)
    monkeypatch.setattr(retained,'verify_contract',verify)
    run=service.plan(setup(),reference_reuse=contract)['run']
    path=service.folder(run['id'])/'scan-request.json';before=path.read_bytes()
    assert len(verified)==1 and service.request(run['id'])['reference_reuse']==original
    contract['source_files_sha256']['calibration/substrate.tiff']='changed'
    assert path.read_bytes()==before and service.request(run['id'])['reference_reuse']==original
    assert 'continuation' not in service.request(run['id']) and 'focus_first' not in service.request(run['id'])
    assert run['state']=='draft' and not calls
    assert json.loads((workspace/'OPERATOR-HOLD.json').read_text())['acquisition_authorized'] is False


def test_rejected_reference_reuse_writes_no_draft(service,monkeypatch):
    import flakefinder.retained_reference as retained
    service,_,calls,_=service
    def reject(*args,**kwargs):raise ValueError('Reference donor evidence changed')
    monkeypatch.setattr(retained,'verify_contract',reject)
    with pytest.raises(ValueError,match='donor evidence changed'):
        service.plan(setup(),reference_reuse={'invalid':'contract'})
    assert not (service.project/'data').exists() and not calls


@pytest.mark.parametrize('mixed',['continuation','focus_first'])
def test_reference_reuse_refuses_other_recovery_modes(service,monkeypatch,mixed):
    import flakefinder.retained_reference as retained
    import flakefinder.scan_continuation as continuation
    service,_,calls,_=service
    monkeypatch.setattr(retained,'verify_contract',lambda *a,**k:pytest.fail('Mixed reuse reached donor validation'))
    monkeypatch.setattr(continuation,'verify_contract',lambda *a,**k:None)
    monkeypatch.setattr(continuation,'continuation_settings',lambda *a,**k:setup())
    with pytest.raises(ValueError,match='cannot be combined'):
        service.plan(setup(),reference_reuse={'donor':'exact'},**{mixed:{'saved':'request'}})
    assert not (service.project/'data').exists() and not calls


def test_plan_records_focus_and_package_source_without_importing_it(service):
    service, _, calls, _ = service
    run_id = service.plan(setup())['run']['id']
    release = service.request(run_id)['source_release']
    package = Path(release['source_directory'])/'flakefinder'
    assert release['scope'] == 'flakefinder-python-v1'
    for name in ('focus_recovery.py', 'autofocus.py', 'acquisition_budget.py',
                 'capture_overlap.py', 'motion.py', 'photometric_edges.py'):
        assert release['files'][name] == hashlib.sha256((package/name).read_bytes()).hexdigest()
    assert set(release['files']) == {p.relative_to(package).as_posix() for p in package.rglob('*.py')
                                   if '__pycache__' not in p.parts}
    assert not calls


@pytest.mark.parametrize('change', ['focus_recovery.py', 'autofocus.py', 'acquisition_budget.py',
                                   'added_module', 'removed_module', 'unchanged'])
def test_start_uses_complete_planned_package_source(service, monkeypatch, tmp_path, change):
    service, _, calls, _ = service
    # A tiny isolated package exercises the launch path without modifying live code.
    package = tmp_path/'source'/'flakefinder'; package.mkdir(parents=True)
    for name in ('scan_service.py', 'focus_recovery.py', 'autofocus.py', 'acquisition_budget.py'):
        (package/name).write_text('# original source\n', encoding='utf-8')
    monkeypatch.setattr('flakefinder.scan_service.__file__', str(package/'scan_service.py'))
    run_id = service.plan(setup())['run']['id']
    request_path = service.folder(run_id)/'scan-request.json'
    original = request_path.read_bytes()
    if change == 'added_module':
        nested = package/'focus'; nested.mkdir()
        (nested/'refinement.py').write_text('# new implementation\n', encoding='utf-8')
    elif change == 'removed_module':
        (package/'autofocus.py').unlink()
    elif change != 'unchanged':
        (package/change).write_text('# changed implementation\n', encoding='utf-8')
    if change == 'unchanged':
        assert service.start(run_id, release_hold=True)['run']['state'] == 'queued'
        assert len(calls) == 1
    else:
        with pytest.raises(ValueError, match='Software changed after planning'):
            service.start(run_id, release_hold=True)
        assert not calls
        assert not (service.folder(run_id)/'acquisition-authorization.json').exists()
    assert request_path.read_bytes() == original


def test_resources_are_rechecked_before_launch_and_stop_is_never_cleared(service):
    service, resources, calls, _ = service
    run = service.plan(setup())['run']; run_id=run['id']
    resources['ram_available_bytes'] = GIB
    with pytest.raises(ValueError,match='capacity'): service.start(run_id,release_hold=True)
    resources['ram_available_bytes'] = 4*GIB; resources['storage_ok'] = False
    with pytest.raises(ValueError,match='reserve'): service.start(run_id,release_hold=True)
    resources['storage_ok'] = True
    service.stop(run_id)
    with pytest.raises(ValueError,match='STOP'): service.start(run_id,release_hold=True)
    assert (service.folder(run_id)/'STOP').exists() and not calls


def test_offline_repair_progress_is_visible_without_marking_failed_scan_complete(service):
    service,_,calls,_=service
    run_id=service.plan(setup())['run']['id'];folder=service.folder(run_id)
    write_json(folder/'scan-status.json',dict(state='failed',phase='finished',error='Saved hardware failure',hardware_closed=True))
    write_json(folder/'supervision/repair-status.json',dict(schema=1,run_id=run_id,state='offline_validated',
        updated_at='2026-10-11T00:00:00Z',summary='Retry history repaired; live validation pending.'))
    before=(folder/'scan-status.json').read_bytes()
    result=service.run_status(run_id)
    assert result['state']=='failed' and result['error']=='Saved hardware failure'
    assert result['supervision']['resolved'] is False
    assert 'Retry history repaired' in result['supervision']['message']
    assert result['repair_progress']['state']=='offline_validated'
    assert (folder/'scan-status.json').read_bytes()==before and not calls


@pytest.mark.parametrize('problem',['wrong_run','invalid_state','oversized','malformed'])
def test_invalid_repair_progress_does_not_break_or_resolve_scan_status(service,problem):
    service,_,calls,_=service
    run_id=service.plan(setup())['run']['id'];folder=service.folder(run_id)
    write_json(folder/'scan-status.json',dict(state='failed',error='Saved failure',hardware_closed=True))
    repair=dict(schema=1,run_id=run_id,state='offline_validated',summary='Repair tested')
    if problem=='wrong_run':repair['run_id']='unrelated-run'
    elif problem=='invalid_state':repair['state']='complete'
    elif problem=='oversized':repair['summary']='x'*20000
    path=folder/'supervision/repair-status.json';write_json(path,repair)
    if problem=='malformed':path.write_text('{',encoding='utf-8')
    result=service.run_status(run_id)
    assert result['state']=='failed' and result['supervision']['resolved'] is False
    assert 'repair_progress' not in result and result['repair_status_warning']
    assert not calls


def test_start_requires_explicit_readiness_and_scoped_hold_authorization(service):
    service, _, calls, workspace = service
    unready = service.plan(setup(operator_ready=False))['run']['id']
    with pytest.raises(ValueError,match='Confirm specimen'): service.start(unready,release_hold=True)
    run_id = service.plan(setup())['run']['id']
    with pytest.raises(ValueError,match='authorize'): service.start(run_id)
    hold = (workspace/'OPERATOR-HOLD.json').read_bytes()
    result = service.start(run_id,release_hold=True)
    assert result['run']['state']=='queued' and len(calls)==1
    args, kwargs=calls[0]
    assert args[0][1:3]==['-m','flakefinder.scan_worker']
    assert kwargs['env']['FLAKEFINDER_RUN_AUTHORIZATION'].endswith('acquisition-authorization.json')
    assert (workspace/'OPERATOR-HOLD.json').read_bytes()==hold
    with pytest.raises(ValueError,match='already been started'): service.start(run_id,release_hold=True)
    with pytest.raises(ValueError,match='already owns'): service.plan(setup())


def test_changed_operator_hold_invalidates_saved_plan(service):
    service, _, calls, workspace = service
    run_id=service.plan(setup())['run']['id']
    write_json(workspace/'OPERATOR-HOLD.json',dict(acquisition_authorized=False,reason='New operator cancellation'))
    with pytest.raises(ValueError,match='changed after planning'):service.start(run_id,release_hold=True)
    assert not calls


def test_stationary_check_is_explicit_and_uses_same_start_interlocks(service):
    service, _, calls, _ = service
    run_id = service.plan(setup())['run']['id']
    with pytest.raises(ValueError, match='authorize'):
        service.start(run_id, prepare_only=True)
    service.start(run_id, release_hold=True, prepare_only=True)
    assert calls[0][0][0][-1] == '--prepare-only'


@pytest.mark.parametrize('contents,expected', [
    (json.dumps({'reason':'Focus outside approved Z','at':'2026-10-10T10:04:33Z'}), 'Focus outside approved Z'),
    ('Legacy operator stop\n', 'Legacy operator stop'),
    ('', 'Stop requested; no reason was recorded.'),
    ('x'*(16*1024+1), 'Stop requested; STOP record exceeds the display limit.'),
])
def test_stopped_run_exposes_bounded_stop_reason_without_mutation(service, contents, expected):
    service, _, calls, _ = service
    run_id=service.plan(setup())['run']['id'];folder=service.folder(run_id)
    write_json(folder/'scan-status.json',dict(state='stopped',phase='finished',error=None,hardware_closed=True))
    stop=folder/'STOP';stop.write_text(contents,encoding='utf-8')
    original_status=(folder/'scan-status.json').read_bytes();original_stop=stop.read_bytes()
    status=service.run_status(run_id)
    assert status['state']=='stopped' and status['error'] is None
    assert status['stop_requested'] and status['stop_reason']==expected
    assert status['stop_recorded_at']
    assert (folder/'scan-status.json').read_bytes()==original_status and stop.read_bytes()==original_stop
    assert not calls


def test_initial_edge_focus_progress_precedes_batch_and_uses_physical_position(service,monkeypatch):
    service, _, calls, _ = service
    run_id=service.plan(setup())['run']['id'];folder=service.folder(run_id)
    write_json(folder/'scan-status.json',dict(state='running',phase='initial_edge_focus',updated_at=100))
    def frame(relative,captured,position,metadata=None):
        path=folder/relative;path.parent.mkdir(parents=True,exist_ok=True)
        path.with_suffix('.jpg').write_bytes(b'preview')
        write_json(path.with_suffix('.json'),dict(acquired_at=captured,position=position,metadata=metadata or {}))
    physical={'x':10,'y':20,'z':30}
    frame('calibration/substrate',10,{'x':1,'y':2,'z':30})
    frame('initial-edge-focus/selection/full-field',20,physical)
    frame('initial-edge-focus/best_observed',30,{'x':11,'y':22,'z':30},{'physical_stage_position':physical})
    write_json(folder/'initial-edge-focus'/'recovery.json',dict(state='searching',samples=[
        dict(z_um=30,score=1,phase='initial'),dict(z_um=25,score=2,phase='explore')],best_observed={'z_um':25}))
    before=(folder/'initial-edge-focus'/'recovery.json').read_bytes()
    value=service.snapshot()['progress']
    assert value['current_step']=='focus' and value['focus']['sample_count']==2
    assert value['focus']['best_z_um']==25 and not value['focus']['best_is_verified']
    assert value['current_image']['relative_path']=='initial-edge-focus/best_observed.jpg'
    assert value['stage_map']['current_position_um']==physical
    # Save order is deliberately different from acquisition order.
    frame('initial-edge-focus/verified_focus',40,physical)
    frame('initial-edge-focus/focused-full-field',50,physical)
    frame('initial-edge-focus/selection/full-field',20,physical)
    write_json(folder/'initial-edge-focus'/'focus-result.json',dict(success=True,z_um=27))
    value=service.snapshot()['progress']
    assert value['current_image']['relative_path']=='initial-edge-focus/focused-full-field.jpg'
    assert value['focus']['best_is_verified'] and value['focus']['best_z_um']==27
    assert (folder/'initial-edge-focus'/'recovery.json').read_bytes()==before and not calls
    write_json(folder/'batch.json',dict(state='running',phase='sparse_edge_probe',chip_states={}))
    write_json(folder/'scan-status.json',dict(state='running',phase='sparse_edge_probe',updated_at=60))
    value=service.snapshot()['progress']
    assert value['current_image']['relative_path']=='initial-edge-focus/focused-full-field.jpg'
    assert value['focus']['best_is_verified'] and value['current_step']=='boundary'
    # A newer mapping image replaces the initial image, but an empty focus
    # placeholder does not erase the verified initial focus curve.
    later_position={'x':50,'y':60,'z':27}
    def mapping_snapshot():
        return dict(current_step='boundary',phase='sparse_edge_probe',
            current_image=dict(relative_path='discovery/probe.jpg',captured_at=60,saved_at=10),
            stage_map={'current_position_um':later_position},focus={'status':'unavailable','samples':[]})
    monkeypatch.setattr('flakefinder.live_progress.build_live_progress',lambda _:mapping_snapshot())
    value=service.snapshot()['progress']
    assert value['current_image']['relative_path']=='discovery/probe.jpg'
    assert value['stage_map']['current_position_um']==later_position
    assert value['focus']['best_is_verified'] and value['focus']['source']=='initial_edge_focus'
    newer_focus=dict(status='searching',captured_at=70,samples=[dict(z_um=26,score=3)])
    monkeypatch.setattr('flakefinder.live_progress.build_live_progress',lambda _:{**mapping_snapshot(),'focus':newer_focus})
    assert service.snapshot()['progress']['focus']==newer_focus
    older_focus={**newer_focus,'captured_at':40}
    monkeypatch.setattr('flakefinder.live_progress.build_live_progress',lambda _:{**mapping_snapshot(),'focus':older_focus})
    assert service.snapshot()['progress']['focus']['source']=='initial_edge_focus'


def test_partial_initial_focus_metadata_does_not_hide_available_preview(service):
    service, _, calls, _ = service
    run_id=service.plan(setup())['run']['id'];folder=service.folder(run_id)/'initial-edge-focus'
    folder.mkdir();(folder/'recovery.json').write_text('{unfinished',encoding='utf-8')
    (folder/'best_observed.jpg').write_bytes(b'preview')
    write_json(folder/'best_observed.json',dict(acquired_at=20,position={'x':1,'y':2,'z':3}))
    value=service.snapshot()['progress']
    assert value['focus']['samples']==[] and value['focus']['status']=='awaiting_saved_samples'
    assert value['current_image']['relative_path']=='initial-edge-focus/best_observed.jpg'
    assert not calls


def test_pinned_reused_focus_keeps_current_image_and_yields_to_new_samples(service,monkeypatch):
    service, _, calls, _=service
    prior=service.plan(setup())['run']['id'];source=service.folder(prior)/'initial-edge-focus'/'focus-result.json'
    write_json(source,dict(success=True,z_um=27,samples=[dict(z_um=27,score=3,phase='fine')]))
    digest=hashlib.sha256(source.read_bytes()).hexdigest()
    current=service.plan(setup())['run']['id'];folder=service.folder(current)
    reused=dict(source_run=prior,source_result=str(source),result_sha256=digest,z_um=27)
    write_json(folder/'initial-focus-reused.json',reused)
    write_json(folder/'batch.json',dict(state='running',phase='sparse_edge_probe',chip_states={}))
    image=dict(relative_path='discovery/current.jpg',captured_at=100)
    def mapping():
        return dict(current_step='boundary',current_image=image,
            focus=dict(status='unavailable',samples=[]),stage_map={'current_position_um':{'x':100,'y':200,'z':27}})
    monkeypatch.setattr('flakefinder.live_progress.build_live_progress',lambda _:mapping())
    before=(folder/'initial-focus-reused.json').read_bytes()
    value=service.snapshot()['progress'];focus=value['focus']
    assert value['current_image']==image and value['current_step']=='boundary'
    assert focus['source']=='reused_initial_focus' and focus['source_run']==prior
    assert focus['best_is_verified'] and not focus['validates_current_field']
    assert focus['sample_count']==1 and focus['result_sha256']==digest
    assert (folder/'initial-focus-reused.json').read_bytes()==before
    source.write_text('{"success":true,"z_um":0}',encoding='utf-8')
    assert service.snapshot()['progress']['focus']['status']=='reused_focus_unavailable'
    write_json(folder/'initial-focus-reused.json',{**reused,'source_result':str(service.project.parent/'other.json')})
    assert service.snapshot()['progress']['focus']['status']=='reused_focus_unavailable'
    newer=dict(status='searching',samples=[dict(z_um=26,score=2)])
    monkeypatch.setattr('flakefinder.live_progress.build_live_progress',lambda _:{**mapping(),'focus':newer})
    assert service.snapshot()['progress']['focus']==newer and not calls


def test_authorization_is_bound_to_exact_request_pid_hold_and_deadline(service,monkeypatch):
    service, _, _, workspace=service
    run_id=service.plan(setup())['run']['id']; service.start(run_id,release_hold=True)
    folder=service.folder(run_id); auth_path=folder/'acquisition-authorization.json'
    auth=json.loads(auth_path.read_text())
    write_json(folder/'worker-start.json',dict(pid=os.getpid(),nonce=auth['nonce'],request_sha256=auth['request_sha256']))
    monkeypatch.setenv('FLAKEFINDER_RUN_AUTHORIZATION',str(auth_path))
    storage.assert_acquisition_allowed()
    write_json(folder/'worker-start.json',dict(pid=os.getpid()+1,nonce=auth['nonce'],request_sha256=auth['request_sha256']))
    with pytest.raises(storage.StorageUnavailable):storage.assert_acquisition_allowed()
    write_json(folder/'worker-start.json',dict(pid=os.getpid(),nonce=auth['nonce'],request_sha256=auth['request_sha256']))
    write_json(workspace/'OPERATOR-HOLD.json',dict(acquisition_authorized=False,reason='Stop now'))
    with pytest.raises(storage.StorageUnavailable):storage.assert_acquisition_allowed()
    write_json(auth_path,{**auth,'expires_at':time.time()-1})
    with pytest.raises(storage.StorageUnavailable):storage.assert_acquisition_allowed()


def test_invalid_settings_do_not_allocate_plans_or_launch(service):
    service, _, calls, _ = service
    for changes in (dict(width_mm=None),dict(width_mm=float('nan')),dict(max_chips=2.5),dict(operator_ready='yes'),
                    dict(sample_id='../old'),dict(material='unknown'),dict(dark_holder=False)):
        assert not service.preflight(setup(**changes))['ok']
    excessive=service.preflight(setup(width_mm=100,height_mm=100,min_chip_mm=.1))
    assert not excessive['ok'] and any('10,000' in b for b in excessive['blockers'])
    with pytest.raises(ValueError):service.folder('../../somewhere')
    assert not calls and not (service.project/'data').exists()


def test_scan_http_requires_same_origin_token(service):
    service, _, calls, _=service
    from flakefinder.suite_server import serve_suite
    from test_suite_server import Catalog
    server,url=serve_suite(service.project,0,provider=Catalog(service.project),scan_service=service)
    try:
        session=json.load(urlopen(url+'api/session')); token=session['token']
        body=json.dumps({'settings':setup()}).encode()
        req=Request(url+'api/scan/plan',data=body,headers={'Content-Type':'application/json','X-Review-Token':token})
        with pytest.raises(HTTPError) as error:urlopen(req)
        assert error.value.code==403
        req.add_header('Origin',url.rstrip('/'))
        result=json.load(urlopen(req)); assert result['run']['state']=='draft'
        assert json.load(urlopen(url+'api/scan'))['run']['id']==result['run']['id']
        assert not calls
    finally:server.shutdown();server.server_close()


def test_offline_queue_precedes_acquisition_launch_and_survives_browser_exit(service,monkeypatch):
    from flakefinder import analysis_lifecycle
    service,resources,calls,_=service
    monkeypatch.setattr(analysis_lifecycle,'source_files',lambda:{})
    events=[]
    monkeypatch.setattr(analysis_lifecycle,'ensure_worker',lambda project,run_id:events.append('offline-supervisor') or {'state':'waiting_for_acquisition'})
    original=service._launcher
    run_id=service.plan(setup(analysis_after_scan=True))['run']['id']
    def launch(*args,**kwargs):
        assert (service.folder(run_id)/'offline-workflow/plan.json').exists()
        events.append('acquisition');return original(*args,**kwargs)
    service._launcher=launch
    result=service.start(run_id,release_hold=True)
    assert events==['offline-supervisor','acquisition'] and result['analysis']['state']=='waiting_for_acquisition'


def test_offline_launch_failure_does_not_start_acquisition(service,monkeypatch):
    from flakefinder import analysis_lifecycle
    service,resources,calls,_=service
    monkeypatch.setattr(analysis_lifecycle,'source_files',lambda:{})
    monkeypatch.setattr(analysis_lifecycle,'ensure_worker',lambda *a:(_ for _ in ()).throw(OSError('no process')))
    run_id=service.plan(setup(analysis_after_scan=True))['run']['id']
    with pytest.raises(ValueError,match='acquisition was not launched'):service.start(run_id,release_hold=True)
    assert not calls
    status=json.loads((service.folder(run_id)/'scan-status.json').read_text())
    assert status['hardware_closed'] is True and status['phase']=='finished'
