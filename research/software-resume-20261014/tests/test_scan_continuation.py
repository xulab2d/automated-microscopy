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


def test_controller_policy_pins_checkpoint_and_preserves_original_deadline(previous):
    project,old,last=previous
    (project/'data/motion.jsonl').write_text('')
    c=cont.build_contract(project,PRIOR,7920.,z_max_um=8120.,focus_travel_policy='controller')
    assert c['motion_checkpoint']['position']==last
    assert c['limits']['z_min']==7920. and c['focus_travel_policy']=='controller'
    assert c['search_bounds_um']==[4000,16000,-8000,0]
    assert cont.continuation_settings(project,c)['focus_travel_policy']=='controller'
    cont.verify_contract(project,c)
    with pytest.raises(SafetyError,match='prior checkpoint envelope'):
        cont.build_contract(project,PRIOR,7800.,focus_travel_policy='controller')


def test_full_controller_travel_does_not_overflow_inactive_window_settings(previous):
    project,old,_=previous
    (project/'data/motion.jsonl').write_text('')
    c=cont.build_contract(project,PRIOR,7920.,z_max_um=8120.,focus_travel_policy='controller')
    c['limits'].update(z_min=-25000.,z_max=25000.)
    before=cont.read(old/'scan-request.json')['settings']
    actual=cont.continuation_settings(project,c)
    assert actual['z_below_um']==before['z_below_um']
    assert actual['z_above_um']==before['z_above_um']
    assert actual['focus_travel_policy']=='controller'


def make_memory_failure(project,old):
    (old/'STOP').unlink();(project/'data/motion.jsonl').write_text('')
    status=cont.read(old/'scan-status.json')
    recovery={'state':'exhausted','elapsed_s':120.01,'updated_at':status['finished_at'],
        'pending_phase':'sparse_edge_focus',
        'error':'Insufficient available RAM before camera capture: 1288818688 bytes available, 1610612736 required.'}
    error='Memory recovery exhausted after 120.0s: '+recovery['error']
    status.update(state='failed',error_type='MemoryHeadroomError',error=error,stop_errors=[],
        failed_phase='recovering_capture_memory',memory_recovery=recovery)
    write(old/'scan-status.json',status);write(old/'memory-recovery.json',recovery)
    batch=cont.read(old/'batch.json');batch.update(state='failed',phase='stopped',error=error);write(old/'batch.json',batch)


def test_typed_pre_capture_failure_preserves_checkpoint_and_pins_evidence(previous):
    project,old,last=previous;make_memory_failure(project,old)
    c=cont.build_contract(project,PRIOR,7920.,focus_travel_policy='controller')
    assert c['continuation_kind']=='diagnosed_pre_capture_memory_recovery'
    assert c['motion_checkpoint']['position']==last
    assert 'memory-recovery.json' in c['prior_files_sha256']
    cont.verify_contract(project,c)
    with pytest.raises(SafetyError,match='not exited'):
        cont.build_contract(project,PRIOR,7920.,focus_travel_policy='controller',process_checker=lambda _:True)

def test_commit_capacity_failure_uses_same_strict_memory_continuation(previous):
    project,old,last=previous;make_memory_failure(project,old)
    recovery=cont.read(old/'memory-recovery.json');recovery['error']='Insufficient allocation capacity before camera capture: 1 bytes available, 311855360 estimated required.'
    status=cont.read(old/'scan-status.json');status.update(memory_recovery=recovery,error='Memory recovery exhausted after 120.0s: '+recovery['error'])
    batch=cont.read(old/'batch.json');batch['error']=status['error']
    write(old/'memory-recovery.json',recovery);write(old/'scan-status.json',status);write(old/'batch.json',batch)
    c=cont.build_contract(project,PRIOR,7920.,focus_travel_policy='controller')
    assert c['continuation_kind']=='diagnosed_pre_capture_memory_recovery'


@pytest.mark.parametrize('problem',['stop','wrong_type','wrong_phase','unclosed','inconsistent_receipt','unfinished_motion'])
def test_pre_capture_failure_requires_exact_closed_evidence(previous,problem):
    project,old,last=previous;make_memory_failure(project,old)
    status=cont.read(old/'scan-status.json')
    if problem=='stop':write(old/'STOP',{'reason':'Operator pressed Stop'})
    elif problem=='wrong_type':status['error_type']='MemoryError'
    elif problem=='wrong_phase':status['failed_phase']='camera_job_wait'
    elif problem=='unclosed':status['hardware_closed']=False
    elif problem=='inconsistent_receipt':write(old/'memory-recovery.json',{'state':'waiting'})
    else:
        at=cont.latest_saved_capture(old)['at']
        (project/'data/motion.jsonl').write_text(json.dumps({'time':at+1,'event':'request','position':last,'target':{**last,'z':last['z']+1},'error':None})+'\n')
    write(old/'scan-status.json',status)
    with pytest.raises(SafetyError):cont.build_contract(project,PRIOR,7920.,focus_travel_policy='controller')


def test_exact_supervisor_stop_at_target_can_verify_stationary_checkpoint(previous):
    project,old,last=previous
    latest=cont.latest_saved_capture(old);at=latest['at'];target={**last,'y':last['y']+10}
    stop=cont.read(old/'STOP');stop['at']=at+1.5;write(old/'STOP',stop)
    events=[{'time':at+1,'event':'request','position':last,'target':target,'error':None},
            {'time':at+2,'event':'failed','position':target,'target':target,'error':'STOP requested: '+str(old/'STOP')}]
    path=project/'data/motion.jsonl';path.write_text(''.join(json.dumps(v)+'\n' for v in events))
    c=cont.build_contract(project,PRIOR,7920.,focus_travel_policy='controller')
    assert c['motion_checkpoint']['kind']=='cooperative_stop_at_target'
    assert c['motion_checkpoint']['position']==target
    cont.verify_contract(project,c)
    events[-1]['position']=last;path.write_text(''.join(json.dumps(v)+'\n' for v in events))
    with pytest.raises(SafetyError,match='Failed motion'):
        cont.build_contract(project,PRIOR,7920.,focus_travel_policy='controller')


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
        elif problem=='z':scope.actual=Position(last['x'],last['y'],8121.)
        else:scope.lamp=81.
        owned.append(scope);return scope
    result=worker.run_scan_worker(project,NEW,scope_factory=factory,profile_loader=profile,
        campaign_runner=lambda *a,**k:pytest.fail('Must not scan'),hold_check=lambda:None,resource_reader=resources)
    assert result['state']=='failed' and result['hardware_closed']
    assert 'arm' not in owned[0].events and 'capture' not in owned[0].events


def test_finished_z_move_after_last_capture_retains_xy_frame(previous):
    project,old,last=previous
    contract=cont.build_contract(project,PRIOR,7800)
    cfg=cont.read(old/'scan-config.json')
    scope=MovingFake(cfg,last);scope.lease.acquire()
    scope.actual=Position(last['x'],last['y'],last['z']+.3)
    proof=cont.verify_stationary_checkpoint(scope,scope.position(),profile(),cfg,contract)
    assert proof['last_capture_z_delta_um']==pytest.approx(.3)
    assert proof['verified_before_arm'] and not scope.armed


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


def resource_stop(previous):
    project,old,last=previous
    status=cont.read(old/'scan-status.json')
    status.update(state='failed',error_type='FutureCancelledError',error='',watchdog_reason=cont.RAM_WATCHDOG_REASON)
    write(old/'scan-status.json',status)
    closed=datetime.fromisoformat(status['finished_at'].replace('Z','+00:00')).timestamp()
    write(old/'watchdog.json',{'reason':cont.RAM_WATCHDOG_REASON,'pid':status['pid'],'automatic_restart':False,'triggered_at':iso(closed-.1)})
    (old/'STOP').write_text(cont.RAM_WATCHDOG_REASON+'\n',encoding='utf-8')
    batch=cont.read(old/'batch.json');batch.update(state='failed',phase='stopped',error='');write(old/'batch.json',batch)
    target={**last,'x':last['x']+100,'y':last['y']+200}
    events=[{'time':closed-3,'event':'request','position':last,'target':target,'error':None},
        {'time':closed-2,'event':'vendor_tolerance_warning','position':target,'target':target,'error':'Position could not be reached.'},
        {'time':closed-1,'event':'settled','position':target,'target':target,'error':None}]
    path=project/'data/motion.jsonl';path.write_text(''.join(json.dumps(e)+'\n' for e in events),encoding='utf-8')
    return path,events,target,closed


def test_resource_cancel_same_interval_uses_hash_pinned_settled_xy(previous):
    project,old,last=previous;path,events,target,closed=resource_stop(previous)
    cfg=cont.read(old/'scan-config.json');contract=cont.build_contract(project,PRIOR,cfg['limits']['z_min'])
    assert contract['motion_checkpoint']['position']==target
    assert contract['motion_checkpoint']['events']==events
    assert contract['motion_checkpoint']['prefix_sha256']==cont.digest(path)
    assert contract['limits']==cfg['limits'] and contract['search_bounds_um']==[4000,16000,-8000,0]
    assert contract['deadline_at']==datetime.fromisoformat(cont.read(old/'scan-status.json')['started_at'].replace('Z','+00:00')).timestamp()+3600
    cont.verify_contract(project,contract)
    scope=MovingFake(cfg,target);scope.lease.acquire()
    proof=cont.verify_stationary_checkpoint(scope,scope.position(),profile(),cfg,contract)
    assert proof['physical_checkpoint_kind']=='settled_motion' and not scope.armed
    scope.actual=Position(**last)
    with pytest.raises(SafetyError,match='Current XY'):cont.verify_stationary_checkpoint(scope,scope.position(),profile(),cfg,contract)
    path.write_text(path.read_text().replace('Position could not be reached.','Altered warning.'),encoding='utf-8')
    with pytest.raises(SafetyError,match='revalidated evidence'):cont.verify_contract(project,contract)


@pytest.mark.parametrize('problem',('wrong_stop','wrong_pid','unclosed','motion_failed','after_closure','disconnected'))
def test_resource_recovery_refuses_unproved_stop_or_motion(previous,problem):
    project,old,last=previous;path,events,target,closed=resource_stop(previous)
    if problem=='wrong_stop':(old/'STOP').write_text('Operator stop',encoding='utf-8')
    elif problem=='wrong_pid':
        d=cont.read(old/'watchdog.json');d['pid']+=1;write(old/'watchdog.json',d)
    else:
        if problem=='unclosed':events.pop()
        elif problem=='motion_failed':events[-1]['event']='failed';events[-1]['error']='Hardware failure'
        elif problem=='after_closure':events.append({**events[0],'time':closed+1,'position':target})
        elif problem=='disconnected':events[0]['position']={**last,'x':last['x']+3}
        path.write_text(''.join(json.dumps(e)+'\n' for e in events),encoding='utf-8')
    with pytest.raises(SafetyError):cont.build_contract(project,PRIOR,7920.)


def recovered_xy_events(previous):
    project,old,last=previous
    path,_,_,closed=resource_stop(previous)
    target={**last,'y':last['y']-4.75}
    error='ZEISS MoveTo: Position could not be reached.'
    base=dict(position=last,target=target,error=error,axis='y',rpc_elapsed_s=.068)
    events=[dict(time=closed-3,event='request',position=last,target=target,error=None),
        dict(time=closed-2.9,event='vendor_position_rejected',attempt=1,**base),
        dict(time=closed-2.6,event='stationary_xy_retry',attempt=2,
             stationary_readbacks=[dict(last) for _ in range(5)],retries_in_window=1,**base),
        dict(time=closed-2.5,event='vendor_motion_response',attempt=2,axis='y',
             position=target,target=target,error=None,position_changed=True),
        dict(time=closed-2,event='settled',position=target,target=target,error=None)]
    path.write_text(''.join(json.dumps(e)+'\n' for e in events),encoding='utf-8')
    return path,events,target


def test_successfully_recovered_xy_motion_remains_valid_continuation_evidence(previous):
    project,old,last=previous;path,events,target=recovered_xy_events(previous)
    before=path.read_bytes();contract=cont.build_contract(project,PRIOR,7920.)
    assert contract['motion_checkpoint']['position']==target
    assert contract['motion_checkpoint']['events']==events
    cont.verify_contract(project,contract)
    assert path.read_bytes()==before


def test_real_adapter_log_from_fake_stationary_retry_is_readable(previous):
    from test_zen_xy_recovery import scope_for_failure
    project,_,_=previous
    scope,start,target,_,_,_,_=scope_for_failure()
    scope.motion_log=project/'data/motion.jsonl'
    scope._record_motion=type(scope)._record_motion.__get__(scope)
    latest={'position':asdict(start),'at':time.time()-1,'sha256':'a'*64}
    scope.move(target)
    checkpoint=cont.settled_motion_checkpoint(project,latest,time.time()+1,asdict(scope.limits))
    assert checkpoint['position']==asdict(target)
    assert [e['event'] for e in checkpoint['events']]==[
        'request','vendor_position_rejected','stationary_xy_retry','vendor_motion_response','settled']


@pytest.mark.parametrize('problem',[
    'z_axis','slow_rpc','partial_move','wrong_target','drifting_sample','missing_samples',
    'missing_retry','missing_response','wrong_response','no_settlement','failed_after_retry'])
def test_incomplete_or_inconsistent_xy_retry_cannot_be_continued(previous,problem):
    project,old,last=previous;path,events,target=recovered_xy_events(previous)
    if problem=='z_axis':events[1]['axis']='z'
    elif problem=='slow_rpc':events[1]['rpc_elapsed_s']=1.1
    elif problem=='partial_move':events[1]['position']={**last,'y':last['y']-.1}
    elif problem=='wrong_target':events[2]['target']={**target,'y':target['y']+1}
    elif problem=='drifting_sample':events[2]['stationary_readbacks'][2]['x']+=.1
    elif problem=='missing_samples':events[2]['stationary_readbacks'].pop()
    elif problem=='missing_retry':events.pop(2)
    elif problem=='missing_response':events.pop(3)
    elif problem=='wrong_response':events[3]['attempt']=1
    elif problem=='no_settlement':events.pop()
    elif problem=='failed_after_retry':events[-1].update(event='failed',error='Hardware failure')
    path.write_text(''.join(json.dumps(e)+'\n' for e in events),encoding='utf-8')
    with pytest.raises(SafetyError):cont.build_contract(project,PRIOR,7920.)


def focused_handoff(previous):
    project,old,last=previous
    status=cont.read(old/'scan-status.json');status.update(state='failed',error_type='SafetyError',
        error=cont.FOCUS_HANDOFF_ERROR,stop_errors=[],watchdog_reason=None);write(old/'scan-status.json',status)
    batch=cont.read(old/'batch.json');batch.update(state='failed',phase='stopped',error=cont.FOCUS_HANDOFF_ERROR);write(old/'batch.json',batch)
    source=cont.read(old/'discovery/edge-focus/last.json')
    frame=Frame(np.full((128,160,3),2000,np.uint16),Position(**last),(.5,.5),6,source['acquired_at']+1,source['metadata'])
    save_frame(old/'initial-edge-focus','focused-full-field',frame)
    write(old/cont.FOCUS_HANDOFF_FILES[0],{'success':True,'outcome':'resolved_peak','z_um':last['z']})
    write(old/cont.FOCUS_HANDOFF_FILES[1],{'state':'focused','result':{'success':True},'confirmation':{'z_um':last['z']}})
    (project/'data/motion.jsonl').write_text('',encoding='utf-8')


def test_confirmed_focus_handoff_recovery_pins_all_sources_and_same_bounds(previous):
    project,old,last=previous;focused_handoff(previous)
    c=cont.build_contract(project,PRIOR,7920.)
    assert c['continuation_kind']=='confirmed_focus_handoff_recovery' and c['initial_focus_reuse']['position']==last
    assert c['limits']==cont.read(old/'scan-config.json')['limits']
    assert all(name in c['prior_files_sha256'] for name in cont.FOCUS_HANDOFF_FILES)
    cont.verify_contract(project,c)
    raw=old/cont.FOCUS_HANDOFF_FILES[2]
    with raw.open('ab') as stream:stream.write(b'changed')
    with pytest.raises(SafetyError,match='evidence changed'):cont.verify_contract(project,c)


@pytest.mark.parametrize('problem',('unresolved','height','confirmation','other_error'))
def test_focus_handoff_requires_exact_clean_error_and_verified_focus(previous,problem):
    project,old,last=previous;focused_handoff(previous)
    if problem=='other_error':
        status=cont.read(old/'scan-status.json');status['error']='Other hardware or software failure';write(old/'scan-status.json',status)
    elif problem=='confirmation':
        d=cont.read(old/cont.FOCUS_HANDOFF_FILES[1]);d['confirmation']['z_um']+=1;write(old/cont.FOCUS_HANDOFF_FILES[1],d)
    else:
        d=cont.read(old/cont.FOCUS_HANDOFF_FILES[0]);d.update({'success':False} if problem=='unresolved' else {'z_um':last['z']+1});write(old/cont.FOCUS_HANDOFF_FILES[0],d)
    with pytest.raises(SafetyError):cont.build_contract(project,PRIOR,7920.)


def preview_persistence_failure(previous):
    project,old,last=previous;status=cont.read(old/'scan-status.json')
    closed=datetime.fromisoformat(status['finished_at'].replace('Z','+00:00')).timestamp()
    config=cont.read(old/'scan-config.json');identifier='a'*32
    vendor=Path(config['zen']['job_output_dir'])/identifier;vendor.mkdir(parents=True)
    czi=vendor/'image.czi';czi.write_bytes(b'original scientific vendor data')
    thumb=vendor/'image.czi.thumb.png';thumb.write_bytes(b'original vendor thumbnail')
    observations=old/'discovery/candidate001/edge-focus/observations';observations.mkdir(parents=True)
    destination=observations/(identifier+'.vendor-previews')/thumb.name
    jpg=observations/(identifier+'.jpg');jpg.write_bytes(b'durable frame preview')
    capture={'schema':1,'capture_id':identifier,'adapter_session_id':'b'*32,
        'result_folder':str(vendor),'job_completed':True,'job_unloaded':True,
        'raw_dtype':'uint16','raw_shape':[128,160,3],'raw_rgb_sha256':'c'*64}
    source=cont.read(old/'discovery/edge-focus/last.json')
    receipt=observations/(identifier+'.json')
    write(receipt,{'schema':1,'kind':'persisted_vendor_observation','capture':capture,
        'position':last,'pixel_size_um':[.5,.5],'sequence':6,'acquired_at':closed-3,
        'created_at':closed-2.5,'metadata':{**source['metadata'],'zen_vendor_result':capture},
        'preview':{'path':str(jpg),'bytes':jpg.stat().st_size,'sha256':cont.digest(jpg)}})
    error=str(FileNotFoundError(2,'No such file or directory',str(destination)))
    status.update(state='failed',error_type='FileNotFoundError',error=error,watchdog_reason=None,
        stop_errors=[],stop_error=None,cleanup_error=None);write(old/'scan-status.json',status)
    batch=cont.read(old/'batch.json');batch.update(state='failed',phase='stopped',error=error);write(old/'batch.json',batch)
    target={**last,'z':last['z']+.1}
    events=[{'time':closed-2,'event':'request','position':last,'target':target,'error':None},
        {'time':closed-1,'event':'settled','position':target,'target':target,'error':None}]
    motion=project/'data/motion.jsonl';motion.write_text(''.join(json.dumps(e)+'\n' for e in events),encoding='utf-8')
    return receipt,jpg,czi,thumb,destination,target


def test_vendor_preview_recovery_preserves_originals_and_settled_motion(previous):
    project,old,_=previous;receipt,jpg,czi,thumb,destination,target=preview_persistence_failure(previous)
    originals={p:p.read_bytes() for p in (receipt,jpg,czi,thumb,old/'STOP')}
    c=cont.build_contract(project,PRIOR,7920.)
    assert c['continuation_kind']=='diagnosed_vendor_preview_persistence_recovery'
    assert c['limits']==cont.read(old/'scan-config.json')['limits']
    assert c['motion_checkpoint']['position']==target and c['motion_checkpoint']['kind']=='settled_motion'
    assert c['deadline_at']==datetime.fromisoformat(cont.read(old/'scan-status.json')['started_at'].replace('Z','+00:00')).timestamp()+3600
    proof=c['preview_persistence_evidence']
    assert proof['receipt_sha256']==cont.digest(receipt) and proof['preview_sha256']==cont.digest(jpg)
    assert [p['sha256'] for p in proof['original_vendor_files']]==[cont.digest(czi),cont.digest(thumb)]
    cont.verify_contract(project,c)
    assert all(p.read_bytes()==data for p,data in originals.items()) and not destination.exists()
    with pytest.raises(SafetyError,match='not exited'):
        cont.build_contract(project,PRIOR,7920.,process_checker=lambda pid:True)
    czi.write_bytes(b'changed vendor data')
    with pytest.raises(SafetyError,match='revalidated evidence'):cont.verify_contract(project,c)


@pytest.mark.parametrize('problem',('other_errno','other_path','outside_discovery','other_capture',
    'jpg_missing','jpg_changed','czi_missing','thumbnail_missing','unloaded_false','receipt_after_closure',
    'batch_disagrees','watchdog','cleanup','hardware_unclosed','unfinished_motion'))
def test_vendor_preview_recovery_rejects_ambiguous_or_missing_evidence(previous,problem):
    project,old,_=previous;receipt,jpg,czi,thumb,destination,_=preview_persistence_failure(previous)
    status=cont.read(old/'scan-status.json')
    if problem in ('other_errno','other_path','outside_discovery','other_capture'):
        if problem=='other_errno':status['error']=str(FileNotFoundError(3,'No such file or directory',str(destination)))
        else:
            if problem=='other_path':bad=destination.with_suffix('.tiff')
            elif problem=='outside_discovery':bad=old/'calibration/observations'/destination.parent.name/destination.name
            else:bad=destination.parent.parent/('f'*32+'.vendor-previews')/destination.name
            status['error']=str(FileNotFoundError(2,'No such file or directory',str(bad)))
        batch=cont.read(old/'batch.json');batch['error']=status['error'];write(old/'batch.json',batch)
    elif problem=='jpg_missing':jpg.unlink()
    elif problem=='jpg_changed':jpg.write_bytes(b'changed')
    elif problem=='czi_missing':czi.unlink()
    elif problem=='thumbnail_missing':thumb.unlink()
    elif problem in ('unloaded_false','receipt_after_closure'):
        d=cont.read(receipt)
        if problem=='unloaded_false':
            d['capture']['job_unloaded']=False;d['metadata']['zen_vendor_result']['job_unloaded']=False
        else:d['created_at']=datetime.fromisoformat(status['finished_at'].replace('Z','+00:00')).timestamp()+1
        write(receipt,d)
    elif problem=='batch_disagrees':
        b=cont.read(old/'batch.json');b['error']='Different error';write(old/'batch.json',b)
    elif problem=='watchdog':status['watchdog_reason']='Hardware did not respond'
    elif problem=='cleanup':status['cleanup_error']='Hardware cleanup failed'
    elif problem=='hardware_unclosed':status['hardware_closed']=False
    elif problem=='unfinished_motion':
        motion=project/'data/motion.jsonl';motion.write_text(motion.read_text().splitlines()[0]+'\n',encoding='utf-8')
    write(old/'scan-status.json',status)
    with pytest.raises(SafetyError):cont.build_contract(project,PRIOR,7920.)
