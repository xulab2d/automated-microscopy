from pathlib import Path
import json,time
import numpy as np
import pytest
from flakefinder import scan_continuation as cont
from flakefinder.reference_repair import preserved_search,check_controls
from flakefinder.model import Frame,Position,SafetyError
from flakefinder.imaging import save_frame
from test_scan_continuation import previous,write,iso,PRIOR

def supervisor_cancel(project,old):
    status=cont.read(old/'scan-status.json');end=time.time()-2;reason='External STOP: '+str(old/'STOP')
    status.update(state='failed',error_type='FutureCancelledError',error='',watchdog_reason=reason,finished_at=iso(end))
    write(old/'scan-status.json',status)
    write(old/'STOP',{'reason':'Supervisor cooperative stop: offline geometry repair.','issued_by':'assistant_supervisor',
        'hardware_fault':False,'clear_stop_automatically':False,'at':end-1,'run_id':old.name,'request_sha256':cont.digest(old/'scan-request.json')})
    write(old/'watchdog.json',{'pid':status['pid'],'reason':reason,'triggered_at':iso(end-.5),'automatic_restart':False})
    b=cont.read(old/'batch.json');b.update(state='failed',phase='stopped',error='');write(old/'batch.json',b)
    (project/'data/motion.jsonl').write_text('')

def test_exact_supervisor_future_cancellation_keeps_checkpoint(previous):
    project,old,last=previous;supervisor_cancel(project,old)
    c=cont.build_contract(project,PRIOR,7920.,focus_travel_policy='controller')
    assert c['motion_checkpoint']['position']==last
    cont.verify_contract(project,c)

@pytest.mark.parametrize('change',['operator','hash','after','fault','watchdog','unclosed'])
def test_ambiguous_cancellation_never_restarts(previous,change):
    project,old,last=previous;supervisor_cancel(project,old)
    stop=cont.read(old/'STOP');s=cont.read(old/'scan-status.json')
    if change=='operator':stop['issued_by']='human'
    elif change=='hash':stop['request_sha256']='changed'
    elif change=='after':stop['at']=time.time()+100
    elif change=='fault':stop['hardware_fault']=True
    elif change=='watchdog':s['watchdog_reason']='Different reason'
    else:s['hardware_closed']=False
    write(old/'STOP',stop);write(old/'scan-status.json',s)
    with pytest.raises(SafetyError):cont.build_contract(project,PRIOR,7920.,focus_travel_policy='controller')

def test_reference_relocation_never_changes_lattice(tmp_path):
    old=tmp_path/'old';out=tmp_path/'new';old.mkdir();out.mkdir()
    search={'probe_centres_um':[[1,2],[3,4]],'start_xy_um':[0,0],'initial_nearest_order':[0,1]}
    write(old/'search-plan.json',search)
    write(out/'scan-request.json',{'continuation':{'reference_correction':True,'prior_run_id':'old',
        'prior_files_sha256':{'search-plan.json':cont.digest(old/'search-plan.json')}}})
    assert preserved_search(out,{**search,'start_xy_um':[6,7],'initial_nearest_order':[1,0]})==search
    with pytest.raises(SafetyError):preserved_search(out,{**search,'probe_centres_um':[[1,3],[3,4]]})

def test_independent_controls_reject_self_consistent_wrong_colour(tmp_path):
    meta={'white_level':4095};position=Position(1,2,3)
    rgb=np.full((128,160,3),[1000,2000,1500],np.uint16);rgb[90:]=[300,350,180]
    focused=Frame(rgb,position,(.5,.5),0,1.,meta);save_frame(tmp_path,'focused',focused)
    for name,color in [('chip-a',[1000,2000,1500]),('chip-b',[1000,2000,1500]),('back-a',[300,350,180]),('back-b',[300,350,180])]:
        save_frame(tmp_path,name,Frame(np.full_like(rgb,color),position,(.5,.5),0,1.,meta))
    write(tmp_path/'selection.json',{'box_px':[50,70,110,110]})
    plan={'focused_frame':'focused.tiff','position_um':{'x':1,'y':2,'z':3},'selection':'selection.json',
        'chip_controls':['chip-a.tiff','chip-b.tiff'],'backing_controls':['back-a.tiff','back-b.tiff']}
    assert check_controls(focused,tmp_path,plan)['accepted']
    wrong=Frame(np.full_like(rgb,[300,350,180]),position,(.5,.5),0,1.,meta)
    assert not check_controls(wrong,tmp_path,plan)['accepted']
