"""Offline contracts for fresh scan setup, authorization, cancellation and cleanup."""
from pathlib import Path
import json
import threading

import numpy as np
import pytest

from flakefinder.model import Frame, Position, SafetyError
from flakefinder import scan_worker as worker


def profile():
    return {'objective':{'position':2,'magnification':20.,'name':'Test manual 20x'},
        'camera_transform':{'matrix':[[.5,0],[0,.5]],'pixel_size_um':[.5,.5],'calibrated':True},
        'camera_resources':{'ff_exposure_ms':266.6,'ff_white_balance':'1,1,1'},
        'job_template':'Test camera','context':{'white_level':4095,'material':'MoTe2','oxide_nm':285},
        'calibration_position':{'x':999999,'y':999999,'z':999999}}


def settings():
    return {'sample_id':'new-graphene','material':'graphene','oxide_nm':90,'width_mm':12,'height_mm':8,
        'z_below_um':80,'z_above_um':120,'min_chip_mm':3,'max_chips':3,'hours':1,
        'max_tiles':1000,'exposure_ms':150,'light_percent':80,'dark_holder':True,'operator_ready':True,'auto_exposure':False}


def resources(_project):
    return {'free_disk_bytes':{'cache':100*1024**3},'available_ram_bytes':8*1024**3}


def test_controller_focus_policy_resolves_live_range_without_arming():
    class Controls:
        def focus_travel_range(self):return {'min_um':0.,'max_um':25000.}
    class Scope:
        def hardware_controls(self):return Controls()
        def position(self):return Position(1,2,8000.)
    cfg={'limits':{'z_min':7975.,'z_max':8025.,'x_min':-10.,'x_max':10.}}
    audit=worker.resolve_focus_travel(Scope(),cfg,{'focus_travel_policy':'controller'})
    assert cfg['limits']=={'z_min':0.,'z_max':25000.,'x_min':-10.,'x_max':10.}
    assert audit['read_before_arm'] and not audit['controller_settings_changed']
    cfg['limits']['z_min']=7975.
    assert worker.resolve_focus_travel(Scope(),cfg,{}) is None
    assert cfg['limits']['z_min']==7975.


class FakeLease:
    def __init__(self):self.acquired=False
    def acquire(self):self.acquired=True
    def close(self):self.acquired=False


class FakeScope:
    def __init__(self,config,*,clipped=False):
        self.lease=FakeLease();self.cancel=threading.Event();self.armed=False;self.closed=False
        self.camera_resources=dict(config['zen']['camera_resources']);self.lamp=40.;self.clipped=clipped;self.stop_count=0
    def position(self):
        assert self.lease.acquired, 'Lease must precede first hardware read'
        return Position(10000,-4000,8000)
    def arm(self,limits):
        self.lease.acquire();limits.check(self.position());self.limits=limits;self.armed=True;self.cancel.clear()
    def check_cancel(self):
        if self.cancel.is_set():raise worker.Cancelled('fake canceled')
    def hardware_controls(self):return self
    def objectives(self):
        assert self.lease.acquired
        return {'position':2,'objectives':[profile()['objective']]}
    def set_lamp_percent(self,value):
        assert self.armed;self.lamp=value;return value
    def set_exposure_ms(self,value):self.camera_resources['ff_exposure_ms']=float(value)
    def capture(self):
        self.check_cancel();rng=np.random.default_rng(7)
        rgb=np.uint16(np.clip(rng.normal(2000,4,(160,160,3)),0,4095))
        if self.clipped:rgb[:30]=4095
        return Frame(rgb,self.position(),(.5,.5),0,0,{'white_level':4095,'objective_position':2,
            'objective':profile()['objective'],'camera_resources':self.camera_resources.copy(),
            'camera_job_template':'Test camera','illumination_percent':self.lamp})
    def stop(self):self.stop_count+=1;self.cancel.set();self.armed=False;return []
    def close(self):self.closed=True;self.lease.close()


@pytest.fixture
def run_folder(tmp_path,monkeypatch):
    from flakefinder import storage
    project=tmp_path/'project';run=project/'data'/'fresh-01';run.mkdir(parents=True)
    (run/'scan-request.json').write_text(json.dumps({'schema':1,'id':'fresh-01','settings':settings(),
        'source_release':'isolated-test-release','created_at':'2026-10-10T00:00:00Z'}))
    (run/'scan-status.json').write_text(json.dumps({'state':'draft'}))
    monkeypatch.setattr(storage,'resolve_output_path',lambda path,**kwargs:Path(path).resolve())
    monkeypatch.setattr(storage,'managed_output_path',lambda path:Path(path).resolve())
    monkeypatch.delenv('FLAKEFINDER_RUN_AUTHORIZATION',raising=False)
    monkeypatch.setattr(worker,'WORKSPACE',tmp_path/'source')
    (tmp_path/'source').mkdir()
    return project,run


def test_fresh_run_uses_one_leased_scope_and_no_historical_coordinates(run_folder):
    project,run=run_folder;owned=[];seen={}
    def factory(config):
        assert 'limits' not in config and 'survey' not in config
        owned.append(FakeScope(config));return owned[-1]
    def campaign(config,reference,output,bounds,options,**kwargs):
        seen.update(config=config,bounds=bounds)
        assert kwargs['scope'] is owned[0] and set(kwargs)=={'scope','callback','full_chip_options'}
        assert config['zen']['keep_vendor_results'] is False and config['zen']['processing_overlap'] is False
        assert config['context']['material']=='graphene' and config['context']['oxide_nm']==90
        assert config['context']['exposure_ms']==150
        assert bounds==[4000,16000,-8000,0]
        assert config['limits']['x_min']==3970 and config['limits']['z_min']==7920 and config['limits']['z_max']==8120
        assert Path(reference).is_file()
        kwargs['callback']('tile',None,{'index':1,'total':10,'oversized_array':np.zeros((20,20))})
        return {'state':'chip_budget_reached','chip_states':{'chip001':{}},'all_chips_found':False}
    result=worker.run_scan_worker(project,'fresh-01',scope_factory=factory,profile_loader=profile,
        campaign_runner=campaign,hold_check=lambda:None,resource_reader=resources)
    assert result['state']=='chip_budget_reached' and result['hardware_closed'] and owned[0].closed
    assert result['progress']=={'index':1,'total':10}
    receipt=worker.read_json(run/'worker-start.json')
    assert receipt['pid']>0 and receipt['request_sha256']==worker.file_hash(run/'scan-request.json') and receipt['nonce']
    assert 'calibration_position' not in seen['config'] and 'initial_focus' not in seen['config']
    with pytest.raises(SafetyError,match='already started'):worker.load_request(project,'fresh-01')


def test_hold_and_low_resources_fail_before_scope_creation(run_folder):
    project,run=run_folder;calls=[]
    def held():
        assert worker.read_json(run/'worker-start.json')['pid']>0
        raise SafetyError('operator hold')
    result=worker.run_scan_worker(project,'fresh-01',scope_factory=lambda cfg:calls.append(cfg),
        hold_check=held,resource_reader=resources)
    assert result['state']=='failed' and result['error']=='operator hold' and calls==[]
    assert result['hardware_closed']
    with pytest.raises(MemoryError):worker.check_resources({'free_disk_bytes':{'cache':100*1024**3},'available_ram_bytes':worker.RAM_FLOOR-1})
    with pytest.raises(worker.AcquisitionError):worker.check_resources({'free_disk_bytes':{'cache':worker.DISK_FLOOR-1},'available_ram_bytes':8*1024**3})


def test_clipped_reference_refuses_discovery_and_closes_owned_scope(run_folder):
    project,run=run_folder;owned=[];campaign_calls=[]
    def factory(config):owned.append(FakeScope(config,clipped=True));return owned[-1]
    result=worker.run_scan_worker(project,'fresh-01',scope_factory=factory,profile_loader=profile,
        campaign_runner=lambda *a,**k:campaign_calls.append(a),hold_check=lambda:None,resource_reader=resources)
    assert result['state']=='failed' and 'clipped' in result['error'] and campaign_calls==[]
    assert owned[0].stop_count==1 and owned[0].closed and not (run/'scan-config.json').exists()


def test_watchdog_handles_parent_stop_absolute_deadline_and_stale_focus(tmp_path,monkeypatch):
    project=tmp_path/'project';run=project/'data'/'run';run.mkdir(parents=True)
    monkeypatch.setattr(worker,'WORKSPACE',tmp_path/'code');clock=[0.]
    watch=worker.WorkerWatchdog(run,project,1000,lambda:None,resources,clock=lambda:clock[0])
    assert watch.tick()
    watch.progress('focus_surface');clock[0]=239;assert watch.tick()
    clock[0]=241;assert not watch.tick() and watch.triggered.is_set() and 'limit 240' in watch.reason
    assert (run/'STOP').exists() and worker.read_json(run/'watchdog.json')['automatic_restart'] is False
    (run/'STOP').unlink();clock[0]=0
    deadline=worker.WorkerWatchdog(run,project,10,lambda:None,resources,clock=lambda:clock[0])
    clock[0]=11;assert not deadline.tick() and 'deadline' in deadline.reason
    (run/'STOP').unlink();(project/'STOP').write_text('operator')
    with pytest.raises(worker.Cancelled,match='STOP'):worker.WorkerWatchdog(run,project,100,lambda:None,resources).check()


def test_request_paths_and_readiness_are_not_guessed(run_folder):
    project,run=run_folder
    with pytest.raises(ValueError):worker.load_request(project,'../fresh-01')
    request=worker.read_json(run/'scan-request.json');request['settings']['operator_ready']=False
    (run/'scan-request.json').write_text(json.dumps(request))
    with pytest.raises(SafetyError,match='specimen-ready'):worker.load_request(project,'fresh-01')


def test_other_materials_keep_fresh_context_and_cleanup_failure_is_explicit(run_folder):
    project,run=run_folder;request=worker.read_json(run/'scan-request.json')
    request['settings'].update(material='hBN',oxide_nm=300)
    (run/'scan-request.json').write_text(json.dumps(request))
    def factory(config):
        scope=FakeScope(config)
        def broken_close():raise OSError('cleanup failed')
        scope.close=broken_close;return scope
    result=worker.run_scan_worker(project,'fresh-01',scope_factory=factory,profile_loader=profile,hold_check=lambda:None,
        resource_reader=resources,campaign_runner=lambda *a,**k:{'state':'search_sampling_complete','chip_states':{}})
    assert result['state']=='failed' and result['requires_attention'] and not result['hardware_closed']
    assert 'cleanup failed' in result['cleanup_error']
    assert worker.read_json(run/'scan-config.json')['context']['oxide_nm']==300


def test_installed_station_connection_is_copied_without_old_positions(tmp_path,monkeypatch):
    source=tmp_path/'source';configs=source/'configs';configs.mkdir(parents=True)
    bindings=tmp_path/'installed-bindings';bindings.mkdir();gateway=tmp_path/'gateway';gateway.mkdir()
    (configs/'lens.json').write_text(json.dumps(profile()))
    station={'schema':1,'id':'test-station','endpoint':'localhost:5002','bindings_dir':str(bindings),
        'gateway_dir':str(gateway),'objective_profile':'lens.json','old_xy':[999,999]}
    (configs/'microscope-station.json').write_text(json.dumps(station));monkeypatch.setattr(worker,'WORKSPACE',source)
    measured=worker.load_camera_profile();config,_=worker.fresh_config(measured,settings(),tmp_path,'run',Position(1,2,3))
    assert config['zen']['bindings_dir']==str(bindings.resolve()) and config['zen']['gateway_dir']==str(gateway.resolve())
    assert config['station_provenance']['sha256'] and 'old_xy' not in config and 'calibration_position' not in config


def test_prepare_only_preserves_reference_and_never_starts_motion_campaign(run_folder):
    project,run=run_folder;owned=[]
    def factory(config):owned.append(FakeScope(config));return owned[-1]
    def forbidden(*args,**kwargs):raise AssertionError('Prepare-only must not start campaign')
    result=worker.run_scan_worker(project,'fresh-01',scope_factory=factory,profile_loader=profile,
        campaign_runner=forbidden,hold_check=lambda:None,resource_reader=resources,prepare_only=True)
    assert result['state']=='prepared' and result['hardware_closed'] and owned[0].closed
    assert (run/'calibration'/'substrate.tiff').is_file() and not (run/'batch.json').exists()
    assert worker.read_json(run/'worker-start.json')['prepare_only'] is True


def test_flake_contaminated_centre_is_reported_without_rejecting_supported_reference(run_folder):
    project,run=run_folder
    class CentreFlakeScope(FakeScope):
        def capture(self):
            frame=super().capture()
            # A 20 um colored flake interrupts the central 40 um diagnostic
            # patch, while more than 90% of this 80 um field is substrate.
            frame.rgb[60:100,60:100]=[3200,1000,1000]
            return frame
    result=worker.run_scan_worker(project,'fresh-01',scope_factory=CentreFlakeScope,profile_loader=profile,
        hold_check=lambda:None,resource_reader=resources,prepare_only=True)
    assert result['state']=='prepared' and result['hardware_closed']
    quality=worker.read_json(run/'reference-validation.json')
    assert quality['accepted'] and quality['substrate_color_support_fraction']>.90
    assert quality['central_patch_policy']=='informational_only'
    assert quality['central_patch']['classification']!='substrate_seed'
    assert quality['central_patch']['patch_substrate_fraction']<.90
    assert (run/'calibration/substrate.tiff').is_file()


def test_explicit_retained_reference_bypasses_capture_and_bootstrap_with_fresh_bounds(run_folder,monkeypatch):
    from flakefinder import retained_reference,autonomous_reference
    project,run=run_folder;request=worker.read_json(run/'scan-request.json')
    request['settings']['autonomous_reference']=True
    request['reference_reuse']={'kind':'test-pinned-retained-reference'}
    (run/'scan-request.json').write_text(json.dumps(request))
    seen={}
    class NoCaptureScope(FakeScope):
        def capture(self):raise AssertionError('Retained-reference path must not recapture')
    def forbidden(*args,**kwargs):raise AssertionError('Retained reference must not run autonomous bootstrap')
    monkeypatch.setattr(autonomous_reference,'acquire_reference',forbidden)
    def retained(scope,config,project,run,contract,check,validate_final,*,bounds):
        assert contract==request['reference_reuse']
        assert bounds==[4000,16000,-8000,0]
        frame=FakeScope.capture(scope)
        # Saved-image acquisition origin is deliberately unlike current XYZ.
        frame.position=Position(12000,-5000,7998.)
        validate_final(frame)
        proof={'source_run':'explicit-donor','fresh_capture_performed':False}
        worker.atomic_json(run/'retained-reference-proof.json',proof)
        return frame,{'fresh_capture_performed':False},None,proof
    monkeypatch.setattr(retained_reference,'acquire_reference',retained)
    def campaign(config,reference,output,bounds,options,**kwargs):
        seen['bounds']=bounds
        return {'state':'chip_budget_reached','chip_states':{}}
    result=worker.run_scan_worker(project,'fresh-01',scope_factory=NoCaptureScope,profile_loader=profile,
        campaign_runner=campaign,hold_check=lambda:None,resource_reader=resources)
    assert result['state']=='chip_budget_reached' and result['hardware_closed']
    assert seen['bounds']==[4000,16000,-8000,0]
    quality=worker.read_json(run/'reference-validation.json')
    assert quality['accepted'] and quality['retained_reference']['fresh_capture_performed'] is False
    assert 'retained' in quality['qualification'] and 'autonomous_bootstrap' not in quality


def test_retained_reference_prepare_only_refuses_before_scope_creation(run_folder):
    project,run=run_folder;request=worker.read_json(run/'scan-request.json')
    request['reference_reuse']={'kind':'test'}
    (run/'scan-request.json').write_text(json.dumps(request));calls=[]
    with pytest.raises(SafetyError,match='explicit motion start'):
        worker.run_scan_worker(project,'fresh-01',prepare_only=True,scope_factory=lambda config:calls.append(config))
    assert calls==[]


def test_retained_helper_returns_to_verified_frame_without_capture(run_folder,monkeypatch):
    from flakefinder import retained_reference as retained
    from flakefinder.model import Limits
    project,run=run_folder;cfg,_=worker.fresh_config(profile(),settings(),project,run.name,Position(10000,-4000,8000))
    donor=project/'data'/'scan-test-donor'
    frame=Frame(np.full((160,160,3),2000,np.uint16),Position(10010,-4010,8002),(.5,.5),1,
        metadata={'camera_resources':{'ff_exposure_ms':150.}})
    donor_proof={'reference_controls':{'accepted':True},'focus_result':{'success':True}}
    monkeypatch.setattr(retained,'verify_contract',lambda *a:None)
    monkeypatch.setattr(retained,'_evidence',lambda *a:(donor,frame,{},None,donor_proof,{},150.))
    class MotionScope:
        armed=True
        limits=Limits(4000,16000,-8000,0,7920,8120)
        def __init__(self):self.p=Position(10000,-4000,8000);self.moves=[]
        def position(self):return self.p
        def check_cancel(self):pass
        def set_exposure_ms(self,value):self.exposure=value
        def move_z(self,z):self.moves.append(('z',z));self.p=Position(self.p.x,self.p.y,z)
        def travel(self,x,y):self.moves.append(('xy',x,y));self.p=Position(x,y,self.p.z)
        def capture(self):raise AssertionError('No new reference acquisition')
    scope=MotionScope()
    # Fresh config uses the dataclass tuple, while immutable requests are JSON
    # and reload pixel_size_um as a list. These identical transforms must match.
    assert isinstance(cfg['camera_transform']['pixel_size_um'],tuple)
    contract=json.loads(json.dumps({'prior_run_id':donor.name,'camera_transform':cfg['camera_transform'],
                                    'qualification':'test retained'}))
    assert isinstance(contract['camera_transform']['pixel_size_um'],list)
    returned,_,_,proof=retained.acquire_reference(scope,cfg,project,run,contract,lambda:None,lambda f:None,
                                                 bounds=[4000,16000,-8000,0])
    assert returned.position==frame.position==scope.position()
    assert [m[0] for m in scope.moves]==['z','xy']
    assert proof['stage_motion_performed'] and not proof['fresh_capture_performed']


def test_stationary_auto_exposure_is_bounded_saved_and_keeps_white_balance(run_folder):
    project,run=run_folder;request=worker.read_json(run/'scan-request.json');request['settings']['auto_exposure']=True
    (run/'scan-request.json').write_text(json.dumps(request));owned=[]
    class LinearScope(FakeScope):
        def capture(self):
            frame=super().capture()
            frame.rgb=np.uint16(np.clip(frame.rgb.astype(float)*self.camera_resources['ff_exposure_ms']/150,0,4095))
            return frame
    def factory(config):owned.append(LinearScope(config));return owned[-1]
    result=worker.run_scan_worker(project,'fresh-01',scope_factory=factory,profile_loader=profile,
        hold_check=lambda:None,resource_reader=resources,prepare_only=True)
    audit=worker.read_json(run/'exposure-adjustment.json')
    assert result['state']=='prepared' and 1<len(audit['attempts'])<=3 and audit['converged']
    assert audit['chosen_exposure_ms']>150 and not audit['white_balance_changed']
    assert all(Path(attempt['raw_path']).is_file() for attempt in audit['attempts'])
    saved=worker.read_json(run/'scan-config.json')
    assert saved['zen']['camera_resources']['ff_white_balance']=='1,1,1'
    assert saved['context']['exposure_ms']==audit['chosen_exposure_ms'] and owned[0].closed


def test_grid_travel_uses_live_controller_range_and_checks_coordinate_frame():
    class Scope:
        def hardware_controls(self):return self
        def position(self):return Position(50.,40.,100.)
        def stage_travel_range(self):return dict(x_min_um=0.,x_max_um=80000.,y_min_um=0.,y_max_um=60000.,x_position_um=self.x,y_position_um=40.)
    scope=Scope();scope.x=50.
    config={'zen':{'xy_backlash_um':30.},'limits':{}}
    bounds,audit=worker.resolve_grid_travel(scope,config,{'grid_4x4':True},[-1000.,90000.,-1000.,90000.])
    assert bounds==[31.,79999.,31.,59999.]
    assert config['limits']['x_min']==1. and not audit['controller_settings_changed']
    scope.x=1000.
    with pytest.raises(SafetyError,match='coordinate frames'):
        worker.resolve_grid_travel(scope,config,{'grid_4x4':True},bounds)


def test_autonomous_bootstrap_failure_never_enters_campaign(run_folder,monkeypatch):
    from flakefinder import autonomous_reference
    project,run=run_folder
    request=worker.read_json(run/'scan-request.json');request['settings']['autonomous_reference']=True
    (run/'scan-request.json').write_text(json.dumps(request))
    calls=[];owned=[]
    def factory(config):owned.append(FakeScope(config));return owned[-1]
    def bootstrap(*args,**kwargs):
        calls.append('bootstrap');raise worker.AcquisitionError('No independent bright-chip edge')
    monkeypatch.setattr(autonomous_reference,'acquire_reference',bootstrap)
    result=worker.run_scan_worker(project,'fresh-01',scope_factory=factory,profile_loader=profile,
        campaign_runner=lambda *a,**k:calls.append('campaign'),hold_check=lambda:None,resource_reader=resources)
    assert calls==['bootstrap'] and result['state']=='failed' and result['hardware_closed']
    assert not (run/'scan-config.json').exists()


def test_prepare_only_remains_stationary_with_autonomous_setting(run_folder,monkeypatch):
    from flakefinder import autonomous_reference
    project,run=run_folder
    request=worker.read_json(run/'scan-request.json');request['settings']['autonomous_reference']=True
    (run/'scan-request.json').write_text(json.dumps(request))
    def forbidden(*args,**kwargs):raise AssertionError('stationary check entered motion bootstrap')
    monkeypatch.setattr(autonomous_reference,'acquire_reference',forbidden)
    result=worker.run_scan_worker(project,'fresh-01',scope_factory=FakeScope,profile_loader=profile,
        campaign_runner=forbidden,hold_check=lambda:None,resource_reader=resources,prepare_only=True)
    assert result['state']=='prepared' and result['hardware_closed']


@pytest.mark.parametrize('handoff',[False,True])
@pytest.mark.parametrize('production',[False,True])
def test_memory_guard_runs_once_before_each_production_acquisition_path(run_folder,monkeypatch,handoff,production):
    import time
    from flakefinder import memory_cleanup,rectangle_handoff
    project,run=run_folder;request=worker.read_json(run/'scan-request.json')
    deadline=time.time()+137.;events=[];launches=[];owned=[]
    if handoff:
        contract={'prior_run_id':'prior-run','deadline_at':deadline,'settings':request['settings']}
        request['rectangle_handoff']=contract;(run/'scan-request.json').write_text(json.dumps(request))
        monkeypatch.setattr(rectangle_handoff,'verify_contract',lambda *a,**k:contract)
        cfg,_=worker.fresh_config(profile(),settings(),project,'fresh-01',Position(10000,-4000,8000))
        monkeypatch.setattr(rectangle_handoff,'source_config',lambda *a,**k:(cfg,profile()))
        def handoff_run(*args,**kwargs):
            events.append('acquire');return {'state':'search_sampling_complete','chip_states':{}}
        monkeypatch.setattr(rectangle_handoff,'run_handoff',handoff_run)
    def factory(config):owned.append(FakeScope(config));return owned[-1]
    from flakefinder import config as configuration
    monkeypatch.setattr(configuration,'connect',factory)
    def guard(path,end,**kwargs):
        state=worker.read_json(run/'scan-status.json')
        assert state['state']=='running' and state['pid']>0
        assert worker.read_json(run/'worker-start.json')['request_sha256']==state['request_sha256']
        launches.append(end);events.append('guard')
        assert path==run and kwargs['source']==worker.WORKSPACE
        if handoff:assert time.time()<end<=deadline
        return {'state':'launched','pid':12345}
    monkeypatch.setattr(memory_cleanup,'launch_memory_guard',guard)
    def campaign(*args,**kwargs):
        events.append('acquire');return {'state':'search_sampling_complete','chip_states':{}}
    result=worker.run_scan_worker(project,'fresh-01',scope_factory=None if production else factory,
        profile_loader=profile,campaign_runner=campaign,hold_check=lambda:None,resource_reader=resources)
    assert result['state']=='search_sampling_complete' and result['hardware_closed'] and owned[0].closed
    assert events==(['guard','acquire'] if production else ['acquire'])
    assert len(launches)==int(production)
    if production:assert result['memory_cleanup_launch']['state']=='launched'


def test_handoff_file_validation_honors_stop_before_scope_creation(run_folder,monkeypatch):
    import time
    from flakefinder import rectangle_handoff as handoff
    project,run=run_folder;request=worker.read_json(run/'scan-request.json')
    request['rectangle_handoff']={'settings':request['settings'],'deadline_at':time.time()+120,'prior_run_id':'closed-parent'}
    (run/'scan-request.json').write_text(json.dumps(request));scopes=[]
    def validate(project,contract,*,check,progress):
        assert (run/'worker-start.json').is_file()
        handoff._hash_file(run/'scan-request.json',check=check,progress=progress)
        state=worker.read_json(run/'scan-status.json')
        assert state['phase']=='validating_handoff_files' and state['file_progress']['file_complete']
        (run/'STOP').write_text('operator STOP');check()
    monkeypatch.setattr(handoff,'source_config',validate)
    result=worker.run_scan_worker(project,'fresh-01',scope_factory=lambda cfg:scopes.append(cfg),
        profile_loader=profile,hold_check=lambda:None,resource_reader=resources)
    assert result['state']=='stopped' and result['hardware_closed'] and scopes==[]


def test_handoff_status_writes_are_throttled_and_keep_latest_completed_file(run_folder,monkeypatch):
    import time
    from flakefinder import rectangle_handoff as handoff
    project,run=run_folder;request=worker.read_json(run/'scan-request.json')
    request['rectangle_handoff']={'settings':request['settings'],'deadline_at':time.time()+120,'prior_run_id':'closed-parent'}
    (run/'scan-request.json').write_text(json.dumps(request))
    original_watch=worker.WorkerWatchdog
    monkeypatch.setattr(worker,'WorkerWatchdog',lambda *a,**k:original_watch(*a,**k,clock=lambda:0.))
    persisted=[];write=worker.atomic_json
    def record_write(path,value):
        if Path(path).name=='scan-status.json' and value.get('phase')=='validating_handoff_files' and value.get('file_progress'):
            persisted.append(dict(value['file_progress']))
        return write(path,value)
    monkeypatch.setattr(worker,'atomic_json',record_write)
    paths=[run/f'input-{i}.txt' for i in range(4)]
    for i,path in enumerate(paths):path.write_text(str(i))
    cfg,_=worker.fresh_config(profile(),settings(),project,'fresh-01',Position(10000,-4000,8000))
    def validate(project,contract,*,check,progress):
        for path in paths:handoff._hash_file(path,check=check,progress=progress)
        return cfg,profile()
    monkeypatch.setattr(handoff,'source_config',validate)
    def execute(*a,**k):
        assert worker.read_json(run/'scan-status.json')['file_progress']['file']==str(paths[-1])
        return {'state':'search_sampling_complete','chip_states':{}}
    monkeypatch.setattr(handoff,'run_handoff',execute)
    result=worker.run_scan_worker(project,'fresh-01',scope_factory=FakeScope,profile_loader=profile,
        hold_check=lambda:None,resource_reader=resources)
    assert result['state']=='search_sampling_complete' and result['hardware_closed']
    assert len(persisted)==1 and result['file_progress']['file']==str(paths[-1])
