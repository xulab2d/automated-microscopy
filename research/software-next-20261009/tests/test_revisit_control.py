"""No hardware: geometry, source provenance, worker engine and launch boundary."""
from dataclasses import asdict
import json
import math
from pathlib import Path
import time

import numpy as np
import pytest

from flakefinder.model import Limits, Position, SafetyError, Cancelled, atomic_json
from flakefinder.revisit_geometry import (corners_from_plan, pixel_to_stage, fit_registration,
    transform_target, source_anchor, digest, verify_source)
from flakefinder.revisit_control import RevisitSession, RevisitControl, no_active_scan


def plan():
    return {'camera_shape':[100,200,3], 'transform':{'matrix':[[.2,-.01],[.02,.2]],
        'pixel_size_um':[.2,.2], 'calibrated':True},
        'boundary_estimate':{'uncertainty_um':235., 'polygon_um':[[-235,-235],[1235,-235],[1235,2235],[-235,2235]],
            'lines':[{'normal':[1,0], 'offset_um':1000., 'expanded_offset_um':1235.},
                     {'normal':[0,1], 'offset_um':2000., 'expanded_offset_um':2235.},
                     {'normal':[-1,0], 'offset_um':0., 'expanded_offset_um':235.},
                     {'normal':[0,-1], 'offset_um':0., 'expanded_offset_um':235.}]}}


@pytest.fixture
def source(tmp_path):
    run=tmp_path/'data'/'scan-test'; chip=run/'chip001'; chip.mkdir(parents=True)
    atomic_json(chip/'coverage-plan.json', plan())
    atomic_json(chip/'tiles'/'tile_000001.json', {'position':{'x':100.,'y':200.,'z':9000.}})
    (chip/'tiles'/'tile_000001.jpg').write_bytes(b'fixture display derivative')
    atomic_json(chip/'manifest.json', {'completed':{'tile_000001':{'hashes':{
        'tiles/tile_000001.json':digest(chip/'tiles'/'tile_000001.json')}}}})
    paths=[chip/'coverage-plan.json',chip/'manifest.json']
    return {'run_id':'scan-test','chip_id':'chip001','observation':'chip001/tile_000001/1',
        'source_pins':{p.relative_to(tmp_path).as_posix():digest(p) for p in paths},
        'corners':corners_from_plan(plan()), 'transform':plan()['transform'],
        'target':{'stage_xy_um':[300.,400.]},
        'source_context':{'sample_id':'test','material':'graphene','oxide_nm':90.,'exposure_ms':200.,'light_percent':90.}}


def test_unexpanded_line_corners_not_raster_envelope():
    corners=corners_from_plan(plan())
    assert sorted(c['xy_um'] for c in corners)==[[0.,0.],[0.,2000.],[1000.,0.],[1000.,2000.]]
    assert all(c['uncertainty_um']==235. and c['kind']=='inferred_line_intersection' for c in corners)


def test_native_pixel_uses_half_pixel_centre_and_off_diagonal_transform():
    actual=pixel_to_stage([199,99],[100,200],{'x':100,'y':200},plan()['transform'])
    assert actual==pytest.approx((np.array([[.2,-.01],[.02,.2]])@np.array([99.5,49.5])+[100,200]).tolist())
    with pytest.raises(SafetyError):pixel_to_stage([200,99],[100,200],{'x':0,'y':0},plan()['transform'])


def anchor(xy, current, uncertainty=5.):
    return {'source_xy_um':xy,'position_um':{'x':current[0],'y':current[1],'z':9999.},'uncertainty_um':uncertainty}


def test_two_points_rotate_translate_without_scale_or_z_fit():
    angle=.4; r=np.array([[math.cos(angle),-math.sin(angle)],[math.sin(angle),math.cos(angle)]])
    t=np.array([800,900])
    points=[[100,200],[1100,200]]
    fit=fit_registration([anchor(p,(r@p+t).tolist()) for p in points])
    xy,allowance=transform_target(fit,[400,500])
    assert xy==pytest.approx(r@[400,500]+t)
    assert fit['scale_fitted'] is False and allowance>0
    assert 'z' not in fit


def test_single_point_requires_explicit_orientation_assertion():
    a=anchor([100,200],[1000,2000])
    with pytest.raises(SafetyError,match='single anchor'):fit_registration([a])
    fit=fit_registration([a],orientation_unchanged=True)
    assert transform_target(fit,[120,250])[0]==[1020.,2050.]


@pytest.mark.parametrize('points', [
    [anchor([0,0],[0,0]),anchor([1,0],[1,0])],
    [anchor([0,0],[0,0]),anchor([1000,0],[2000,0])]])
def test_bad_correspondence_and_short_baseline_refused(points):
    with pytest.raises(SafetyError):fit_registration(points)


def test_source_pixel_anchor_pins_actual_metadata(tmp_path,source):
    a=source_anchor(tmp_path,source,source_field='chip001/tile_000001',source_pixel_xy=[99.5,49.5])
    assert a['source_xy_um']==[100.,200.]
    assert a['kind']=='operator_matched_image_point'
    path=tmp_path/'data'/'scan-test'/'chip001'/'tiles'/'tile_000001.json'
    atomic_json(path,{'position':{'x':900,'y':900,'z':0}})
    with pytest.raises(SafetyError):verify_source(tmp_path,a)
    with pytest.raises(SafetyError):source_anchor(tmp_path,source,source_field='chip001/tile_000001',source_pixel_xy=[99,49])


def test_source_anchor_cannot_cross_chip(tmp_path,source):
    with pytest.raises(SafetyError):source_anchor(tmp_path,source,source_field='chip002/tile_000001',source_pixel_xy=[0,0])


def test_nonfinite_registration_coordinates_refused():
    with pytest.raises(SafetyError):fit_registration([anchor([0,0],[float('nan'),0])],orientation_unchanged=True)
    with pytest.raises(SafetyError):fit_registration([anchor([float('inf'),0],[0,0])],orientation_unchanged=True)


def test_preview_derivative_changes_invalidate_recorded_anchor(tmp_path,source):
    a=source_anchor(tmp_path,source,source_field='chip001/tile_000001',source_pixel_xy=[99.5,49.5])
    (tmp_path/'data'/'scan-test'/'chip001'/'tiles'/'tile_000001.jpg').write_bytes(b'changed display')
    with pytest.raises(SafetyError):verify_source(tmp_path,a)


def test_motion_only_capture_refuses_before_any_client_call():
    from flakefinder.zen import ZenMicroscope
    scope=object.__new__(ZenMicroscope);scope.motion_only=True
    with pytest.raises(SafetyError,match='disabled'):scope.capture()


def test_motion_only_constructor_uses_receipt_storage_not_capture_reserve(monkeypatch):
    from flakefinder.zen import ZenMicroscope
    from flakefinder import storage
    calls=[]
    monkeypatch.setattr(storage,'assert_acquisition_allowed',lambda:calls.append('authorization'))
    monkeypatch.setattr(storage,'verify_project',lambda **kw:calls.append(kw))
    monkeypatch.setattr(storage,'prepare_acquisition_storage',lambda p:pytest.fail('capture reservation called'))
    # Deliberately end before any hardware client is constructed.
    def resolved(path):raise RuntimeError('end offline constructor test')
    monkeypatch.setattr(storage,'resolve_output_path',resolved)
    with pytest.raises(RuntimeError,match='end offline'):ZenMicroscope(motion_only=True)
    assert calls==['authorization',{'write_probe':True}]


class Lease:
    def __init__(self,log):self.log=log;self.file=None
    def acquire(self):self.log.append('lease');self.file=True
    def close(self):self.log.append('lease_close');self.file=None


class Scope:
    def __init__(self):
        self.log=[];self.lease=Lease(self.log);self.p=Position(100.,200.,123.);self.armed=False
        self.expected_objective_position=2;self.stopped=False
    def position(self):
        assert self.lease.file is not None
        self.log.append('position');return self.p
    def hardware_controls(self):
        assert self.lease.file is not None
        return self
    def stage_travel_range(self):return dict(x_min_um=-10000.,x_max_um=20000.,y_min_um=-10000.,y_max_um=20000.,x_position_um=self.p.x,y_position_um=self.p.y)
    def focus_travel_range(self):return dict(min_um=0.,max_um=20000.)
    def verify_optics(self,value):assert value==2
    def arm(self,limits):self.limits=limits;limits.check(self.p);self.armed=True
    def check_cancel(self):
        if self.stopped:raise Cancelled('fake stop')
    def travel(self,x,y):
        self.check_cancel();self.limits.check(Position(x,y,self.p.z));self.log.append(('travel',x,y,self.p.z));self.p=Position(x,y,self.p.z)
    def close(self):self.armed=False;self.lease.close()
    def stop(self):self.stopped=True;self.armed=False;self.log.append('stop');return []


@pytest.fixture
def engine(tmp_path,source):
    folder=tmp_path/'data'/'revisit-20261015-000000-12345678';folder.mkdir()
    request={'source':source,'placement_mode':'reloaded','expires_at':time.time()+300,'limits':None}
    profile={'objective':{'magnification':20,'position':2,'name':'20x'},'camera_transform':source['transform'],
             'camera_resources':{'ff_white_balance':'1,1,1'},'job_template':'raw','context':{'white_level':4095}}
    scopes=[]
    def factory(config):
        assert config['zen']['job_template'] is None
        s=Scope();scopes.append(s);return s
    value=RevisitSession(tmp_path,folder,request,scope_factory=factory,profile_loader=lambda:profile,
                         hold_check=lambda:None,scan_check=lambda:None)
    value.test_scopes=scopes
    return value


def command(action,**args):return {'action':action,'id':'a'*32,**args}


def test_construction_offline_connect_leases_before_readback(engine):
    assert engine.test_scopes==[]
    engine.connect()
    assert engine.scope.log[0]=='lease'
    assert engine.state['state']=='registration_required'
    assert engine.scope.armed
    with pytest.raises(SafetyError,match='Register'):engine.execute(command('move'))
    assert not any(isinstance(v,tuple) for v in engine.scope.log)


def test_unchanged_revisit_preserves_fresh_z_not_saved_z(engine):
    engine.request['placement_mode']='unchanged';engine.connect()
    engine.scope.p=Position(100.,200.,678.)
    engine.execute(command('move'))
    assert engine.scope.p==Position(300.,400.,678.)
    receipt=json.loads((engine.folder/'moves'/('a'*32+'.json')).read_text())
    assert receipt['target_um']['z']==678. and receipt['identity_verified'] is False


def test_reload_register_exact_source_pixels_release_reconnect_then_move(engine):
    engine.connect()
    engine.execute(command('anchor',source_field='chip001/tile_000001',source_pixel_xy=[99.5,49.5]))
    engine.execute(command('register',orientation_unchanged=True))
    first=engine.scope
    engine.execute(command('release'))
    assert first.lease.file is None and engine.state['state']=='released'
    engine.execute(command('reconnect',operator_confirmed=True))
    engine.scope.p=Position(999.,999.,77.)
    engine.execute(command('move'))
    assert engine.scope.p==Position(300.,400.,77.)
    assert len(engine.anchors)==1


def test_stop_or_hold_change_prevents_move(engine):
    engine.request['placement_mode']='unchanged';engine.connect()
    (engine.folder/'STOP').write_text('stop')
    with pytest.raises(Cancelled):engine.execute(command('move'))
    assert not any(isinstance(v,tuple) for v in engine.scope.log)


def test_source_change_prevents_move(engine,tmp_path):
    engine.request['placement_mode']='unchanged';engine.connect()
    (tmp_path/'data'/'scan-test'/'chip001'/'coverage-plan.json').write_text('{}')
    with pytest.raises(SafetyError,match='source changed'):engine.execute(command('move'))
    assert not any(isinstance(v,tuple) for v in engine.scope.log)


def test_bounds_and_expiry_prevent_motion(engine):
    engine.request['placement_mode']='unchanged'
    engine.request['limits']=asdict(Limits(0,200,0,300,0,500))
    engine.connect()
    with pytest.raises(SafetyError):engine.execute(command('move'))
    engine.request['expires_at']=time.time()-1
    with pytest.raises(Cancelled):engine.execute(command('move'))


def test_optical_geometry_mismatch_refuses_before_scope_creation(engine):
    profile=engine.profile_loader();profile['camera_transform']={**profile['camera_transform'],'matrix':[[1,0],[0,1]]}
    with pytest.raises(SafetyError,match='geometry'):engine.connect()
    assert engine.test_scopes==[]


def test_active_scan_and_unconfirmed_closed_owner_block(tmp_path,monkeypatch):
    import flakefinder.scan_service as service
    monkeypatch.setattr(service,'process_alive',lambda pid:False)
    run=tmp_path/'data'/'scan-test';run.mkdir(parents=True)
    atomic_json(tmp_path/'scan-control'/'current.json',{'id':'scan-test'})
    atomic_json(run/'scan-status.json',{'state':'running'})
    with pytest.raises(SafetyError):no_active_scan(tmp_path)
    atomic_json(run/'scan-status.json',{'state':'failed','hardware_closed':False})
    atomic_json(run/'worker-start.json',{'pid':42})
    with pytest.raises(SafetyError):no_active_scan(tmp_path)
    atomic_json(run/'scan-status.json',{'state':'failed','hardware_closed':True})
    no_active_scan(tmp_path)


def test_facade_requires_explicit_arm_and_never_launches_on_reads(tmp_path,source):
    calls=[]
    controller=RevisitControl(tmp_path,launcher=lambda *a,**k:calls.append(a),metadata_loader=lambda *a:source)
    assert controller.status()['state']=='idle'
    assert controller.metadata('scan-test',source['observation'])==source
    with pytest.raises(SafetyError):controller.begin('scan-test',source['observation'],placement_mode='unchanged',operator_confirmed=False)
    assert calls==[]


def test_begin_creates_separate_pid_authorization_preserves_holds(tmp_path,source,monkeypatch):
    from flakefinder import storage
    import flakefinder.scan_service as service
    monkeypatch.setattr(storage,'verify_project',lambda **k:None)
    monkeypatch.setattr(storage,'project_root',lambda:tmp_path)
    monkeypatch.setattr(storage,'operator_holds',lambda:[{'path':'hold','sha256':'pinned','active':True}])
    monkeypatch.setattr(service,'process_alive',lambda pid:bool(pid))
    calls=[]
    def launch(*args,**kwargs):
        calls.append((args,kwargs));return type('Child',(),{'pid':123})()
    controller=RevisitControl(tmp_path,launcher=launch,metadata_loader=lambda *a:source)
    result=controller.begin('scan-test',source['observation'],placement_mode='reloaded',operator_confirmed=True)
    folder=controller.folder(result['session_id'])
    auth=json.loads((folder/'acquisition-authorization.json').read_text())
    assert auth['holds']==[{'path':'hold','sha256':'pinned'}]
    assert calls[0][1]['env']['FLAKEFINDER_RUN_AUTHORIZATION']==str(folder/'acquisition-authorization.json')
    assert 'FLAKEFINDER_RUN_AUTHORIZATION' not in __import__('os').environ
    assert result['state']=='queued'
    with pytest.raises(SafetyError):controller.begin('scan-test',source['observation'],placement_mode='unchanged',operator_confirmed=True)


def test_source_mutation_and_restarted_session_are_never_automatically_armed(tmp_path,source,monkeypatch):
    import flakefinder.scan_service as service
    monkeypatch.setattr(service,'process_alive',lambda pid:False)
    folder=tmp_path/'data'/'revisit-20261015-000000-12345678';folder.mkdir(parents=True)
    atomic_json(folder/'scan-status.json',{'state':'ready','hardware_closed':False})
    atomic_json(folder/'worker-start.json',{'pid':123})
    control=RevisitControl(tmp_path)
    assert control.status(folder.name)['state']=='interrupted'
    with pytest.raises(SafetyError):control.move(folder.name)


def test_worker_stop_interrupts_motion_closes_lease_and_cannot_restart(engine,monkeypatch):
    import threading
    import flakefinder.revisit_control as module
    import flakefinder.scan_service as service
    engine.request.update(id=engine.folder.name,kind='operator_graphene_revisit',
                          placement_mode='unchanged',source_files={'fixture':'immutable'})
    atomic_json(engine.folder/'scan-request.json',engine.request)
    authorization=engine.folder/'acquisition-authorization.json'
    atomic_json(authorization,{'request_sha256':digest(engine.folder/'scan-request.json'),'nonce':'once'})
    monkeypatch.setenv('FLAKEFINDER_RUN_AUTHORIZATION',str(authorization))
    monkeypatch.setattr(service,'source_files',lambda path:{'fixture':'immutable'})
    monkeypatch.setattr(module,'RevisitSession',lambda *args:engine)
    entered=threading.Event()
    factory=engine.scope_factory
    def scope_factory(config):
        scope=factory(config)
        def travel(x,y):
            entered.set()
            deadline=time.monotonic()+5
            while time.monotonic()<deadline:
                scope.check_cancel();time.sleep(.01)
            pytest.fail('worker did not interrupt pending motion')
        scope.travel=travel
        return scope
    engine.scope_factory=scope_factory
    atomic_json(engine.folder/'commands'/('a'*32+'.json'),command('move'))
    errors=[]
    def worker():
        try:module.run_worker(engine.project,engine.folder.name)
        except BaseException as exc:errors.append(exc)
    thread=threading.Thread(target=worker)
    thread.start()
    assert entered.wait(3)
    atomic_json(engine.folder/'STOP',{'operator_stop':True})
    thread.join(timeout=4)
    assert not thread.is_alive() and not errors
    state=json.loads((engine.folder/'scan-status.json').read_text())
    assert state['state']=='stopped' and state['hardware_closed'] is True
    assert engine.test_scopes[0].lease.file is None
    assert 'stop' in engine.test_scopes[0].log
    with pytest.raises(FileExistsError):module.run_worker(engine.project,engine.folder.name)


@pytest.mark.parametrize('state,closed',[('queued',True),('ready',False),('released',True),('failed',False)])
def test_scan_start_symmetric_guard_includes_released_session(tmp_path,monkeypatch,state,closed):
    from flakefinder.scan_service import ScanService
    folder=tmp_path/'data'/'revisit-20261015-000000-12345678';folder.mkdir(parents=True)
    atomic_json(tmp_path/'scan-control'/'revisit-current.json',{'id':folder.name})
    atomic_json(folder/'scan-status.json',{'state':state,'hardware_closed':closed})
    service=ScanService(tmp_path,launcher=lambda *a,**k:pytest.fail('unexpected scan launch'))
    with pytest.raises(ValueError,match='revisit session'):service.start('not-read-because-revisit-active')


def test_closed_revisit_does_not_block_scan_start(tmp_path,monkeypatch):
    from flakefinder.scan_service import ScanService
    folder=tmp_path/'data'/'revisit-20261015-000000-12345678';folder.mkdir(parents=True)
    atomic_json(tmp_path/'scan-control'/'revisit-current.json',{'id':folder.name})
    atomic_json(folder/'scan-status.json',{'state':'closed','hardware_closed':True})
    service=ScanService(tmp_path)
    def folder_after_guard(_):raise RuntimeError('passed revisit guard')
    monkeypatch.setattr(service,'folder',folder_after_guard)
    with pytest.raises(RuntimeError,match='passed revisit guard'):service.start('offline-test')
