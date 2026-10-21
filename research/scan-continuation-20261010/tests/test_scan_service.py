import hashlib
import json
import os
import time
from types import SimpleNamespace
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


def test_resources_are_rechecked_before_launch_and_stop_is_never_cleared(service):
    service, resources, calls, _ = service
    run = service.plan(setup())['run']; run_id=run['id']
    resources['ram_available_bytes'] = GIB
    with pytest.raises(ValueError,match='RAM'): service.start(run_id,release_hold=True)
    resources['ram_available_bytes'] = 4*GIB; resources['storage_ok'] = False
    with pytest.raises(ValueError,match='reserve'): service.start(run_id,release_hold=True)
    resources['storage_ok'] = True
    service.stop(run_id)
    with pytest.raises(ValueError,match='STOP'): service.start(run_id,release_hold=True)
    assert (service.folder(run_id)/'STOP').exists() and not calls


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
