"""Typed failed startup/donor contracts with synthetic files, no hardware."""
import copy,json,time
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
import numpy as np
import pytest
from flakefinder import scan_continuation as cont,startup_continuation as startup,storage,vendor_backlog
from flakefinder.model import SafetyError,Frame,Position
from flakefinder.imaging import save_frame
from flakefinder.reference_repair import PLAN,preserved_search
from test_scan_continuation import previous,write,iso,PRIOR,NEW
from test_reference_repair import supervisor_cancel

CHILD='scan-20261010-020000-33333333'

@pytest.fixture
def failed_start(previous,monkeypatch):
    project,old,last=previous;supervisor_cancel(project,old)
    base=(old/PLAN).parent;base.mkdir(parents=True)
    for name in ('focused','chip-a','chip-b','back-a','back-b'):
        save_frame(base,name,Frame(np.full((16,16,3),1000,np.uint16),Position(**last),(.5,.5),1,time.time()-20,{}))
    write(base/'focus-result.json',{'success':True,'z_um':last['z']});write(base/'selection.json',{'box_px':[0,0,16,16]})
    plan={'schema':1,'kind':'focused_reference_with_independent_controls_v1','prior_request_sha256':cont.digest(old/'scan-request.json'),
          'focused_frame':'focused.tiff','focus_result':'focus-result.json','selection':'selection.json','position_um':last,
          'chip_controls':['chip-a.tiff','chip-b.tiff'],'backing_controls':['back-a.tiff','back-b.tiff'],'fresh_focus_score_fraction':.95,
          'files_sha256':{p.name:cont.digest(p) for p in base.iterdir()}}
    write(old/PLAN,plan)
    donor=cont.build_contract(project,PRIOR,7920.,z_max_um=8120.,focus_travel_policy='controller')
    run=project/'data'/NEW;run.mkdir()
    release={'files':{'scan_worker.py':'f'*64}}
    settings=cont.continuation_settings(project,donor)
    request={'id':NEW,'schema':1,'settings':settings,'source_release':release,'continuation':donor}
    write(run/'scan-request.json',request);sha=cont.digest(run/'scan-request.json')
    end=datetime.fromisoformat(cont.read(old/'scan-status.json')['finished_at'].replace('Z','+00:00')).timestamp()
    error='Unacknowledged vendor output limit: 4 jobs, 12 bytes. Persist/review pending work before another capture; no files were deleted.'
    status={'id':NEW,'pid':123456,'state':'failed','hardware_closed':True,'source_release':release,'request_sha256':sha,
            'error_type':'AcquisitionError','error':error,'failed_phase':'reference_exposure','exposure_attempt':1,
            'exposure_ms':settings['exposure_ms'],'watchdog_reason':None,'stop_errors':[],'started_at':iso(end+.1),'finished_at':iso(end+1)}
    write(run/'scan-status.json',status)
    write(run/'worker-start.json',{'id':NEW,'pid':123456,'request_sha256':sha,'source_release':release,'worker_sha256':'f'*64})
    write(run/'recovery-needed.json',{'run_id':NEW,'request_sha256':sha,'source_release':release,'hardware_closed':True,
          'failed_phase':'reference_exposure','error_type':'AcquisitionError','error':error,
          'traceback':'_capture_reference _capture_job _check_pending_vendor_output vendor_lifecycle.py in check_pending'})
    write(run/'heartbeat.json',{'phase':'reference_exposure','pid':123456,'at':end+.8})
    write(run/'launch.json',{'pid':123457});write(run/'acquisition-authorization.json',{'holds':[]})
    write(run/'continuation-verification.json',{'continuation':donor,'checkpoint':{'verified_before_arm':True}})
    for name,value in [('camera-profile-used.json',cont.read(old/'camera-profile-used.json')),('session-instruction.json',{}),('focus-travel.json',{})]:write(run/name,value)
    write(old/'supervision/adaptive-focus-continuation.json',{'new_run':NEW,'new_request_sha256':sha,'parent_request_sha256':donor['prior_files_sha256']['scan-request.json']})
    events=[{'time':end+.3,'event':'request','position':last,'target':last,'error':None},{'time':end+.4,'event':'settled','position':last,'target':last,'error':None}]
    (project/'data/motion.jsonl').write_text(''.join(json.dumps(e)+'\n' for e in events))
    root=project/'data/zen-captures';root.mkdir()
    for identifier in ('a'*32,'b'*32,'c'*32,'d'*32):
        (root/identifier).mkdir();(root/identifier/'image.czi').write_bytes(b'raw')
    write(root.parent/'vendor-retained-reviews'/('a'*32+'.json'),{})
    monkeypatch.setattr(vendor_backlog,'validated_reviews',lambda _: {'a'*32:{}})
    monkeypatch.setattr(storage,'operator_holds',lambda:[])
    return project,old,run,donor,status

def build(f):return cont.build_contract(f[0],NEW,7920.,z_max_um=8120.,focus_travel_policy='controller')

def consume(f,c):
    project,old,run,donor,status=f;child=project/'data'/CHILD
    write(child/'scan-request.json',{'continuation':c})
    write(run/'supervision/adaptive-focus-continuation.json',{'new_run':CHILD,'new_request_sha256':cont.digest(child/'scan-request.json'),'parent_request_sha256':cont.digest(run/'scan-request.json')})
    return child

def test_failed_startup_retains_immediate_parent_and_exact_donor(failed_start):
    f=failed_start;c=build(f)
    assert c['prior_run_id']==NEW and startup.donor_contract(c)==f[3]
    assert c['reference_correction']==f[3]['reference_correction'] and c['deadline_at']==f[3]['deadline_at']
    assert c['prior_coordinate_frame_id']==f[3]['prior_coordinate_frame_id']
    assert c['motion_checkpoint']['closure_at']>f[3]['motion_checkpoint']['closure_at']
    assert cont.verify_contract(f[0],c)[0]==cont.read(f[1]/'scan-config.json')
    consume(f,c)
    with pytest.raises(SafetyError,match='consumed'):build(f)
    assert cont.verify_contract(f[0],c,consumer_run_id=CHILD)[0]==cont.read(f[1]/'scan-config.json')
    with pytest.raises(SafetyError):cont.verify_contract(f[0],c,consumer_run_id='scan-20261010-030000-44444444')

@pytest.mark.parametrize('change',['stop','phase','type','request','donor','closure','motion_after','unfinished','active','holds','no_review','backlog','child_request'])
def test_failed_startup_rejects_ambiguous_or_changed_evidence(failed_start,monkeypatch,change):
    f=failed_start;project,old,run,donor,status=f;c=build(f)
    if change=='stop':write(run/'STOP',{'reason':'Operator pressed Stop'})
    elif change in ('phase','type','closure'):
        status[{'phase':'failed_phase','type':'error_type','closure':'hardware_closed'}[change]]={'phase':'capture','type':'OtherError','closure':False}[change];write(run/'scan-status.json',status)
    elif change=='request':write(run/'scan-request.json',{})
    elif change=='donor':write(old/PLAN,{})
    elif change in ('motion_after','unfinished'):
        stamp=datetime.fromisoformat(status['finished_at'].replace('Z','+00:00')).timestamp()+(.1 if change=='motion_after' else -.1)
        p=donor['reference_correction']['position_um']
        with (project/'data/motion.jsonl').open('a') as stream:stream.write(json.dumps({'time':stamp,'event':'request','position':p,'target':p,'error':None})+'\n')
    elif change=='active':monkeypatch.setattr(cont,'process_alive_strict',lambda _:True)
    elif change=='holds':monkeypatch.setattr(storage,'operator_holds',lambda:[{'path':'new','sha256':'changed'}])
    elif change=='no_review':monkeypatch.setattr(vendor_backlog,'validated_reviews',lambda _: {})
    elif change=='backlog':(project/'data/zen-captures'/('d'*32)/'image.czi').write_bytes(b'changed')
    else:
        child=consume(f,c);write(child/'scan-request.json',{'changed':True})
        with pytest.raises(SafetyError):cont.verify_contract(project,c,consumer_run_id=CHILD)
        return
    with pytest.raises((SafetyError,KeyError)):build(f)

def test_startup_stationary_gate_requires_five_current_xyz_samples(failed_start,monkeypatch):
    f=failed_start;c=build(f);cfg=cont.read(f[1]/'scan-config.json');profile=cont.read(f[1]/'camera-profile-used.json')
    position=Position(**c['motion_checkpoint']['position']);samples=[]
    class Scope:
        def check_cancel(self):pass
        def position(self):samples.append(position);return position
        def hardware_controls(self):return self
        def objectives(self):return {'position':2,'objectives':[profile['objective']]}
        def lamp_percent(self):return c['last_capture']['optics']['illumination_percent']
    monkeypatch.setattr(cont.time,'sleep',lambda _:None)
    proof=cont.verify_stationary_checkpoint(Scope(),position,profile,cfg,c)
    assert len(samples)==5
    assert proof['stationary_readbacks_required']==5 and len(proof['stationary_readbacks_um'])==5
    class Drift(Scope):
        def position(self):return Position(position.x+.51,position.y,position.z)
    with pytest.raises(SafetyError):cont.verify_stationary_checkpoint(Drift(),position,profile,cfg,c)
