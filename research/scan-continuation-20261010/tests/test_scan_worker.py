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
