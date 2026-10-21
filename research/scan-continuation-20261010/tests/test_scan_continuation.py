"""Offline continuation contracts; fake leased hardware only."""
from dataclasses import asdict
from datetime import datetime,timezone
import json,time
from pathlib import Path

import numpy as np
import pytest

from flakefinder import scan_continuation as cont,scan_worker as worker,storage
from flakefinder.scan_service import ScanService
from flakefinder.imaging import save_frame
from flakefinder.model import Position,Frame,SafetyError
from test_scan_worker import FakeScope,profile,settings,resources

PRIOR='scan-20261010-000000-11111111'
NEW='scan-20261010-010000-22222222'


def write(path,value):
    path.parent.mkdir(parents=True,exist_ok=True);path.write_text(json.dumps(value),encoding='utf-8')


def iso(at):return datetime.fromtimestamp(at,timezone.utc).isoformat().replace('+00:00','Z')


@pytest.fixture
def previous(tmp_path,monkeypatch):
    project=tmp_path/'p';old=project/'data'/PRIOR;old.mkdir(parents=True)
    monkeypatch.setattr(storage,'resolve_output_path',lambda path,**kwargs:Path(path).resolve())
    monkeypatch.setattr(storage,'managed_output_path',lambda path:Path(path).resolve())
    monkeypatch.setattr(cont,'process_alive_strict',lambda pid:False)
    monkeypatch.delenv('FLAKEFINDER_RUN_AUTHORIZATION',raising=False)
    now=time.time();origin=Position(10000,-4000,8000);config,bounds=worker.fresh_config(profile(),settings(),project,PRIOR,origin)
    optics={'white_level':4095,'objective_position':2,'objective':profile()['objective'],
        'camera_resources':config['zen']['camera_resources'],'camera_job_template':'Test camera','illumination_percent':80.}
    frame=Frame(np.full((128,160,3),2000,np.uint16),origin,(.5,.5),1,now-50,optics)
    save_frame(old/'calibration','substrate',frame)
    request={'schema':1,'id':PRIOR,'settings':settings(),'created_at':now-60,'source_release':'offline-fixture'}
    write(old/'scan-request.json',request);sha=cont.digest(old/'scan-request.json')
    write(old/'worker-start.json',{'id':PRIOR,'pid':54321,'request_sha256':sha})
    write(old/'scan-status.json',{'id':PRIOR,'pid':54321,'state':'stopped','campaign_state':'stopped',
        'hardware_closed':True,'request_sha256':sha,'started_at':iso(now-60),'finished_at':iso(now-5)})
    write(old/'STOP',{'reason':'Supervisor cooperative stop: focus peak outside approved interval.',
        'issued_by':'assistant_supervisor','hardware_fault':False,'clear_stop_automatically':False,'at':iso(now-6)})
    write(old/'scan-config.json',config);write(old/'camera-profile-used.json',profile())
    write(old/'batch.json',{'state':'stopped','current_chip':None,'error':'STOP requested: '+str(old/'STOP'),
        'coordinate_frame_id':'same-fresh-frame','search_bounds_um':bounds,'reference_position_um':asdict(origin)})
    reference=old/'calibration/substrate.tiff'
    write(old/'reference-validation.json',{'accepted':True,'raw_sha256':cont.digest(reference),
        'metadata_sha256':cont.digest(reference.with_suffix('.json'))})
    # An ROI's virtual centre is NOT the physical stage checkpoint.
    last=asdict(Position(10400,-4400,7980))
    write(old/'discovery/edge-focus/last.json',{'position':{'x':10402,'y':-4390,'z':7980},'pixel_size_um':[.5,.5],
        'metadata':{**optics,'physical_stage_position':last},'sequence':5,'acquired_at':now-10})
    return project,old,last


def test_contract_keeps_bounds_origin_upper_z_and_parent_stop(previous):
    project,old,last=previous;original_stop=(old/'STOP').read_bytes()
    c=cont.build_contract(project,PRIOR,7800.)
    assert c['search_bounds_um']==[4000,16000,-8000,0]
    assert c['reference_origin_um']=={'x':10000,'y':-4000,'z':8000}
    assert c['last_capture']['position']==last
    assert c['limits']['z_min']==7800 and c['limits']['z_max']==8120
    assert cont.verify_contract(project,c)[0]['limits']['z_min']==7920
    assert (old/'STOP').read_bytes()==original_stop
    with pytest.raises(SafetyError,match='only an explicit lower'):cont.build_contract(project,PRIOR,7800,z_max_um=8121)
    with pytest.raises(SafetyError,match='only an explicit lower'):cont.build_contract(project,PRIOR,7950)


@pytest.mark.parametrize('change',[{'hardware_closed':False},{'state':'failed'},
    {'error_type':'AcquisitionError','error':'ZEISS hardware fault'},{'stop_errors':['lost response']}])
def test_ambiguous_failure_or_unclosed_hardware_refuses(previous,change):
    project,old,_=previous;status=cont.read(old/'scan-status.json');status.update(change);write(old/'scan-status.json',status)
    with pytest.raises(SafetyError):cont.build_contract(project,PRIOR,7800)


def test_active_pid_and_changed_evidence_refuse(previous):
    project,old,_=previous
    with pytest.raises(SafetyError,match='not exited'):cont.build_contract(project,PRIOR,7800,process_checker=lambda pid:True)
    contract=cont.build_contract(project,PRIOR,7800)
    value=cont.read(old/'discovery/edge-focus/last.json');value['metadata']['physical_stage_position']['x']+=1
    write(old/'discovery/edge-focus/last.json',value)
    with pytest.raises(SafetyError,match='evidence changed'):cont.verify_contract(project,contract)


class MovingFake(FakeScope):
    def __init__(self,config,position):
        super().__init__(config);self.actual=Position(**position);self.events=[];self.lamp=80.
    def position(self):
        assert self.lease.acquired;return self.actual
    def lamp_percent(self):
        assert self.lease.acquired;self.events.append('read_lamp_before_arm' if not self.armed else 'read_lamp');return self.lamp
    def arm(self,limits):self.events.append('arm');super().arm(limits)
    def move_z(self,z):
        assert self.armed;self.check_cancel();target=Position(self.actual.x,self.actual.y,z);self.limits.check(target)
        assert abs(z-self.actual.z)<=self.limits.max_z_step_um
        self.actual=target;self.events.append('move_z')
    def travel(self,x,y):
        assert self.armed;self.check_cancel();self.actual=Position(x,y,self.actual.z);self.limits.check(self.actual);self.events.append('travel')
    def capture(self):
        self.events.append('capture');return super().capture()


def new_request(project,contract):
    run=project/'data'/NEW;run.mkdir()
    write(run/'scan-request.json',{'schema':1,'id':NEW,'created_at':time.time(),'source_release':'offline-fixture',
        'settings':cont.continuation_settings(project,contract),'continuation':contract})
    write(run/'scan-status.json',{'state':'draft'})
    return run


def test_worker_checks_before_arm_returns_reference_without_recentering(previous):
    project,old,last=previous;contract=cont.build_contract(project,PRIOR,7800);run=new_request(project,contract);owned=[];seen={}
    def factory(config):owned.append(MovingFake(config,last));return owned[-1]
    def campaign(config,reference,output,bounds,options,**kwargs):
        seen.update(bounds=bounds,config=config)
        assert kwargs['scope'].position()==Position(10000,-4000,8000)
        return {'state':'chip_budget_reached','chip_states':{}}
    outcome=worker.run_scan_worker(project,NEW,scope_factory=factory,profile_loader=profile,campaign_runner=campaign,
        hold_check=lambda:None,resource_reader=resources)
    assert outcome['state']=='chip_budget_reached' and outcome['hardware_closed']
    assert seen['bounds']==[4000,16000,-8000,0] and seen['config']['limits']['z_min']==7800
    events=owned[0].events
    assert events.index('read_lamp_before_arm')<events.index('arm')<events.index('travel')<events.index('capture')
    assert events.count('arm')==1 and (old/'STOP').exists()
    assert cont.read(run/'continuation-verification.json')['checkpoint']['verified_before_arm']


@pytest.mark.parametrize('problem',('position','z','lamp'))
def test_changed_stationary_checkpoint_never_arms(previous,problem):
    project,old,last=previous;run=new_request(project,cont.build_contract(project,PRIOR,7800));owned=[]
    def factory(config):
        scope=MovingFake(config,last)
        if problem=='position':scope.actual=Position(last['x']+2,last['y'],last['z'])
        elif problem=='z':scope.actual=Position(last['x'],last['y'],last['z']+.3)
        else:scope.lamp=81.
        owned.append(scope);return scope
    result=worker.run_scan_worker(project,NEW,scope_factory=factory,profile_loader=profile,
        campaign_runner=lambda *a,**k:pytest.fail('Must not scan'),hold_check=lambda:None,resource_reader=resources)
    assert result['state']=='failed' and result['hardware_closed']
    assert 'arm' not in owned[0].events and 'capture' not in owned[0].events


def test_lower_z_authorization_does_not_extend_original_deadline(previous):
    project,old,_=previous
    contract=cont.build_contract(project,PRIOR,7800)
    status=cont.read(old/'scan-status.json')
    assert contract['deadline_at']==datetime.fromisoformat(status['started_at'].replace('Z','+00:00')).timestamp()+3600
    status['started_at']=iso(time.time()-7200);write(old/'scan-status.json',status)
    with pytest.raises(SafetyError,match='deadline has passed'):cont.build_contract(project,PRIOR,7800)


def test_stationary_prepare_cannot_silently_return_with_motion(previous):
    project,old,last=previous;new_request(project,cont.build_contract(project,PRIOR,7800))
    with pytest.raises(SafetyError,match='stationary prepare-only'):
        worker.run_scan_worker(project,NEW,prepare_only=True,scope_factory=lambda _:pytest.fail('No client'),hold_check=lambda:None)


def test_service_creates_new_draft_and_never_launches_or_clears_stop(previous,monkeypatch):
    project,old,last=previous
    service=ScanService(project,resource_reader=lambda:{'storage_ok':True,'ram_available_bytes':8*1024**3,
        'cache_free_bytes':80*1024**3,'project_free_bytes':80*1024**3,'error':None},launcher=lambda *a,**k:pytest.fail('No launch'))
    monkeypatch.setattr(service,'holds',lambda:{'active':False,'records':[],'reason':''})
    result=service.plan_continuation(PRIOR,7800)
    request=service.request(result['run']['id'])
    assert request['continuation']['prior_run_id']==PRIOR and result['run']['id']!=PRIOR
    assert request['settings']['width_mm']==12 and request['settings']['height_mm']==8
    assert request['settings']['auto_exposure'] is False and (old/'STOP').exists()
    assert not (service.folder(result['run']['id'])/'worker-start.json').exists()
