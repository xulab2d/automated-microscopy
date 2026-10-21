"""Classified recovery, receipt integrity and one shared physical-capture budget."""
import copy,hashlib,json,time
from dataclasses import asdict,replace
from types import SimpleNamespace
from pathlib import Path
import numpy as np
import pytest
from flakefinder import reference_focus_recovery as refocus,scan_continuation as cont,reference_repair as repair
from flakefinder import focus_recovery,acquisition_budget,vendor_lifecycle,storage
from flakefinder.model import Frame,Position,SafetyError,Limits,AcquisitionError
from flakefinder.imaging import save_frame,load_frame
from test_scan_continuation import previous,write,iso,PRIOR,NEW
from test_startup_continuation import failed_start,build as startup_build,consume,CHILD

NEXT='scan-20261010-030000-44444444'
REAL_RECOVER=focus_recovery.recover_focus

def receipt(frame,raw,root):
    archive=raw.with_suffix('.zen-vendor.json')
    write(archive,{'status':'removed_after_verified_raw_save','capture':frame.metadata['zen_vendor_result'],
        'raw_tiff':str(raw),'raw_sidecar':str(raw.with_suffix('.json')),
        'raw_tiff_sha256':cont.digest(raw),'raw_sidecar_sha256':cont.digest(raw.with_suffix('.json'))})
    return {'status':'removed','raw_tiff':str(raw),'archive':str(archive),'archive_sha256':cont.digest(archive),
            'vendor_result_folder':frame.metadata['zen_vendor_result']['result_folder']}

def tagged(frame):
    return replace(frame,metadata={**frame.metadata,'zen_vendor_result':{'job_completed':True,'job_unloaded':True,
        'capture_id':'e'*32,'adapter_session_id':'b'*32,
        'result_folder':str(Path(frame.metadata.get('_synthetic_vendor_root',''))/('e'*32)),
        'raw_shape':list(frame.rgb.shape),'raw_dtype':str(frame.rgb.dtype),
        'raw_rgb_sha256':hashlib.sha256(np.ascontiguousarray(frame.rgb).tobytes()).hexdigest()}})

@pytest.fixture
def refused(failed_start,monkeypatch):
    project,old,parent,donor,status=failed_start;c=startup_build(failed_start);run=consume(failed_start,c)
    release={'files':{'scan_worker.py':'f'*64}}
    request={'schema':1,'id':CHILD,'continuation':c,'settings':cont.continuation_settings(project,c),'source_release':release}
    write(run/'scan-request.json',request);sha=cont.digest(run/'scan-request.json')
    consumed=cont.read(parent/'supervision/adaptive-focus-continuation.json');consumed['new_request_sha256']=sha
    write(parent/'supervision/adaptive-focus-continuation.json',consumed)
    end=time.time()-1;position=donor['reference_correction']['position_um']
    s={'id':CHILD,'pid':123460,'state':'failed','hardware_closed':True,'request_sha256':sha,'source_release':release,
       'error_type':'AcquisitionError','error':refocus.ERROR,'failed_phase':'reference_exposure','stop_errors':[],
       'watchdog_reason':None,'finished_at':iso(end),'started_at':iso(end-.8)}
    write(run/'scan-status.json',s)
    write(run/'worker-start.json',{'id':CHILD,'pid':s['pid'],'request_sha256':sha,'source_release':release,'worker_sha256':'f'*64})
    write(run/'recovery-needed.json',{'run_id':CHILD,'request_sha256':sha,'source_release':release,
        'error_type':'AcquisitionError','error':refocus.ERROR,'hardware_closed':True})
    write(run/'continuation-verification.json',{'continuation':c,'checkpoint':{'verified_before_arm':True,
        'stationary_readbacks_required':5,'stationary_readbacks_um':[position]*5}})
    for name,value in [('heartbeat.json',{}),('launch.json',{'pid':123461}),('acquisition-authorization.json',{'holds':[]}),
                       ('camera-profile-used.json',cont.read(old/'camera-profile-used.json')),('session-instruction.json',{}),
                       ('focus-travel.json',{}),('memory-guard/status.json',{'guard_pid':123462})]:write(run/name,value)
    optics={**donor['last_capture']['optics'],'_synthetic_vendor_root':str(project/'data/zen-captures')}
    frame=tagged(Frame(np.ones((16,16,3),np.uint16),Position(**position),(.5,.5),1,end-.1,optics))
    raw=run/'calibration/reference-attempt-01.tiff';save_frame(raw.parent,raw.stem,frame)
    ack=receipt(frame,raw,run)
    write(run/'exposure-adjustment.json',{'enabled':False,'attempts':[{'raw_path':str(raw),
        'raw_sha256':cont.digest(raw),'metadata_sha256':cont.digest(raw.with_suffix('.json')),'vendor_receipt':ack}]})
    quality={'accepted':False,'controls':[{'accepted':True}]*4,'focus':{'accepted':False,'minimum_score_fraction':.95}}
    write(run/'reference-correction-verification.json',quality)
    monkeypatch.setattr(repair,'check_controls',lambda *a,**k:copy.deepcopy(quality))
    # Authorized child motion after its failed parent closed; the historical
    # prefix is admitted only through the child's consumed verified contract.
    with (project/'data/motion.jsonl').open('a') as stream:
        for event in ('request','settled'):stream.write(json.dumps({'time':end-.5,'event':event,'position':position,'target':position,'error':None})+'\n')
    return project,old,parent,run,donor,c,s

def rebuild(f,**kwargs):return cont.build_contract(f[0],CHILD,7920.,z_max_um=8120.,focus_travel_policy='controller',**kwargs)

def test_refusal_contract_preserves_nested_donor_but_current_full_motion(refused):
    f=refused;project,old,parent,run,donor,historical,s=f;c=rebuild(f)
    assert c['prior_run_id']==CHILD and c['reference_focus_recovery']['donor_contract']==donor
    assert c['reference_focus_recovery']['verified_historical_startup']==historical
    assert c['motion_checkpoint']['prefix_bytes']>historical['motion_checkpoint']['prefix_bytes']
    assert c['deadline_at']==donor['deadline_at'] and c['search_bounds_um']==donor['search_bounds_um']
    assert cont.verify_contract(project,c)[0]==cont.read(old/'scan-config.json')
    child=project/'data'/NEXT;write(child/'scan-request.json',{'continuation':c})
    write(run/'supervision/adaptive-focus-continuation.json',{'new_run':NEXT,'new_request_sha256':cont.digest(child/'scan-request.json'),
        'parent_request_sha256':cont.digest(run/'scan-request.json')})
    with pytest.raises(SafetyError,match='consumed'):rebuild(f)
    cont.verify_contract(project,c,consumer_run_id=NEXT)
    with pytest.raises(SafetyError):cont.verify_contract(project,c,consumer_run_id=NEW)
    write(child/'scan-request.json',{'changed':True})
    with pytest.raises(SafetyError):cont.verify_contract(project,c,consumer_run_id=NEXT)

@pytest.mark.parametrize('change',['stop','unknown','controls','focus','raw','ack','proof','historical_receipt','historical_prefix','after_close','active','new_hold'])
def test_refusal_rejects_unproved_or_changed_evidence(refused,monkeypatch,change):
    f=refused;project,old,parent,run,donor,historical,s=f
    if change=='stop':write(run/'STOP',{})
    elif change=='unknown':s['error']='unknown';write(run/'scan-status.json',s)
    elif change in ('controls','focus'):
        q=cont.read(run/'reference-correction-verification.json')
        if change=='controls':q['controls'][0]['accepted']=False
        else:q['focus']['accepted']=True
        write(run/'reference-correction-verification.json',q)
    elif change=='raw':(run/'calibration/reference-attempt-01.tiff').write_bytes(b'altered')
    elif change=='ack':write(run/'calibration/reference-attempt-01.zen-vendor.json',{})
    elif change=='proof':write(run/'continuation-verification.json',{})
    elif change=='historical_receipt':write(parent/'supervision/adaptive-focus-continuation.json',{'new_run':NEXT})
    elif change=='historical_prefix':
        with (project/'data/motion.jsonl').open('r+') as stream:stream.write(' ')
    elif change=='after_close':
        with (project/'data/motion.jsonl').open('a') as stream:stream.write(json.dumps({'time':time.time()+1,'event':'request','position':donor['last_capture']['position'],'target':donor['last_capture']['position'],'error':None})+'\n')
    elif change=='active':monkeypatch.setattr(cont,'process_alive_strict',lambda _:True)
    else:monkeypatch.setattr(storage,'operator_holds',lambda:[{'path':'new','sha256':'new'}])
    with pytest.raises((SafetyError,KeyError,ValueError)):rebuild(f)

class Scope:
    armed=True
    def __init__(self,frame):self.frame=frame;self.frames=0;self.limits=Limits(-100,100,-100,100,0,200,50,10)
    def check_cancel(self):pass
    def position(self):return self.frame.position
    def capture(self):self.frames+=1;return tagged(replace(self.frame,sequence=self.frames,acquired_at=time.time()))
    def move_z(self,z):self.frame=replace(self.frame,position=replace(self.frame.position,z=z))

@pytest.fixture
def acquisition(tmp_path,monkeypatch):
    monkeypatch.setattr(storage,'managed_output_path',lambda p:Path(p).resolve())
    monkeypatch.setattr(storage,'resolve_output_path',lambda p,**k:Path(p).resolve())
    old=tmp_path/'data'/PRIOR;run=tmp_path/'data'/NEXT;run.mkdir(parents=True);base=(old/repair.PLAN).parent;base.mkdir(parents=True)
    rgb=np.full((256,320,3),[1000,2000,1500],np.uint16);rgb[210:]=[300,350,180]
    from test_scan_worker import profile
    frame=Frame(rgb,Position(1,2,100),(.5,.5),1,time.time(),{'white_level':4095,'objective_position':2,
        'objective':profile()['objective'],'camera_resources':{'ff_exposure_ms':150.,'ff_white_balance':'1,1,1'},
        'camera_job_template':'Test camera','illumination_percent':80.,'_synthetic_vendor_root':str(tmp_path/'data/zen-captures')})
    save_frame(base,'focused',frame)
    for name,color in [('chip-a',[1000,2000,1500]),('chip-b',[1000,2000,1500]),('back-a',[300,350,180]),('back-b',[300,350,180])]:save_frame(base,name,replace(frame,rgb=np.full_like(rgb,color)))
    write(old/'scan-request.json',{})
    write(base/'focus-result.json',{'success':True,'z_um':100.});write(base/'selection.json',{'box_px':[130,190,190,230]})
    plan={'schema':1,'kind':'focused_reference_with_independent_controls_v1','prior_request_sha256':cont.digest(old/'scan-request.json'),
        'focused_frame':'focused.tiff','focus_result':'focus-result.json','selection':'selection.json','position_um':asdict(frame.position),
        'chip_controls':['chip-a.tiff','chip-b.tiff'],'backing_controls':['back-a.tiff','back-b.tiff'],'fresh_focus_score_fraction':.95,
        'files_sha256':{p.name:cont.digest(p) for p in base.iterdir()}}
    write(old/repair.PLAN,plan)
    donor={'prior_run_id':PRIOR,'reference_correction':plan,'prior_files_sha256':{}}
    contract={'continuation_kind':refocus.KIND,'prior_run_id':CHILD,'reference_correction':plan,
        'reference_focus_recovery':{'donor_contract':donor,'fixed_roi_box_px':[130,190,190,230],
            'maximum_seconds':600.,'maximum_captures':64,'minimum_prior_focus_score_fraction':.95}}
    write(run/'scan-request.json',{'continuation':contract})
    config={'camera_transform':{'matrix':[[.5,0],[0,.5]],'pixel_size_um':[.5,.5],'calibrated':True}}
    scope=Scope(frame)
    monkeypatch.setattr(vendor_lifecycle,'acknowledge_raw',lambda s,f,p:receipt(f,p,run))
    def initial(s,*a):return s.capture(),{'attempts':[{}]},{}
    def recover(s,*a,output=None,**k):
        assert acquisition_budget.scope_budget(s) is not None
        from flakefinder.motion import move_z_bounded
        move_z_bounded(s,132.)
        s.capture();s.capture_with_processing(lambda f:{})
        from flakefinder.autofocus import FocusResult
        result=FocusResult(True,132.,1.,1.,'independent synthetic peak',[],outcome='resolved_peak')
        write(output/'focus-result.json',asdict(result));write(output/'recovery.json',{'state':'focused','result':asdict(result),'confirmation':{'z_um':132.}})
        return result
    monkeypatch.setattr(focus_recovery,'recover_focus',recover)
    return old,run,contract,config,scope,initial

def acquire(a):
    old,run,c,config,scope,initial=a
    return refocus.acquire_reference(scope,config,{},run,scope.position(),lambda:None,lambda *a,**k:None,initial,c,
        lambda frame,proof:repair.verify_fresh(frame,old,run,measured_focus=proof,persist=False))

def test_shared_capture_counter_measured_gate_canonical_campaign_seed(acquisition):
    old,run,c,cfg,scope,_=acquisition;frame,audit,ack,proof=acquire(acquisition)
    assert scope.frames==proof['budget']['captures_started']==4
    assert proof['measured_position_um']['z']==132. and proof['original_position_um']['z']==100.
    with pytest.raises(SafetyError,match='position'):repair.verify_fresh(frame,old,run)
    assert repair.verify_fresh(frame,old,run,measured_focus=proof)['accepted']
    seed=refocus.campaign_seed(run,proof);assert seed['seed_focus_z_um']==132.
    from flakefinder.initial_edge_focus import reusable_focus_request
    future=reusable_focus_request(run.parent.parent,run.name)
    assert future['source_run']==run.name and future['source_sha256']['focus-result.json']==proof['result_sha256']
    assert load_frame(seed['seed_focus_frame']).position.z==132.

@pytest.mark.parametrize('change',['result','proof','budget','raw','ack','old_z','xy','optics','blur'])
def test_measured_proof_cannot_relax_reference_gates(acquisition,change):
    old,run,c,cfg,scope,_=acquisition;frame,audit,ack,proof=acquire(acquisition)
    if change=='result':write(run/'initial-edge-focus/focus-result.json',{'success':False})
    elif change=='proof':proof['fixed_roi_box_px']=[0,0,10,10]
    elif change=='budget':write(run/'reference-refocus-budget.json',{'captures_started':65})
    elif change=='raw':(run/'initial-edge-focus/focused-full-field.tiff').write_bytes(b'changed')
    elif change=='ack':write(run/'initial-edge-focus/focused-full-field.zen-vendor.json',{})
    elif change=='old_z':frame=replace(frame,position=replace(frame.position,z=100.))
    elif change=='xy':frame=replace(frame,position=replace(frame.position,x=2.))
    elif change=='optics':frame=replace(frame,metadata={**frame.metadata,'objective_position':4})
    else:frame=replace(frame,rgb=np.full_like(frame.rgb,1000))
    with pytest.raises((SafetyError,AcquisitionError)):repair.verify_fresh(frame,old,run,measured_focus=proof)

@pytest.mark.parametrize('mode',['count','deadline_before_final','deadline_during_final'])
def test_total_budget_includes_final_capture_and_persists_late_raw(acquisition,monkeypatch,mode):
    old,run,c,cfg,scope,_=acquisition;clock=[0.];real=acquisition_budget.AcquisitionBudget
    monkeypatch.setattr(acquisition_budget,'AcquisitionBudget',lambda seconds,captures:real(seconds,captures,clock=lambda:clock[0]))
    original=focus_recovery.recover_focus
    def recover(s,*a,**k):
        result=original(s,*a,**k)
        if mode=='count':
            while s.budget.captures<64:s.capture()
        elif mode=='deadline_before_final':clock[0]=600.
        return result
    monkeypatch.setattr(focus_recovery,'recover_focus',recover)
    original_capture=scope.capture
    def capture():
        frame=original_capture()
        if mode=='deadline_during_final' and scope.frames==4:clock[0]=600.
        return frame
    scope.capture=capture
    with pytest.raises(acquisition_budget.BudgetExhausted):acquire(acquisition)
    assert scope.frames=={'count':64,'deadline_before_final':3,'deadline_during_final':4}[mode]
    assert not (run/'reference-refocus-proof.json').exists()
    assert (run/'initial-edge-focus/focused-full-field.tiff').exists()==(mode=='deadline_during_final')
    assert cont.read(run/'reference-refocus-budget.json')['captures_started']==scope.frames

def test_local_focus_nested_donor_requires_verified_outer_proof(tmp_path):
    from test_focus_checkpoint import fixture
    from flakefinder.focus_checkpoint import find_local_focus
    folder,prior,base,frame,crop,donor=fixture(tmp_path);run=folder.parents[1]
    outer={'prior_run_id':CHILD,'reference_focus_recovery':{'donor_contract':donor}}
    write(run/'scan-request.json',{'continuation':outer})
    write(run/'continuation-verification.json',{'continuation':outer,'checkpoint':{'verified_before_arm':True}})
    assert find_local_focus(folder,{'prior_run_id':prior.name},[10,20],frame)['result']['z_um']==31
    assert find_local_focus(folder,{'prior_run_id':prior.name},[10,20],frame,partial=True) is None
    write(run/'continuation-verification.json',{'continuation':donor})
    with pytest.raises(SafetyError):find_local_focus(folder,{'prior_run_id':prior.name},[10,20],frame)

def test_real_adaptive_focus_uses_shared_counter_for_repeats_confirmation_and_final(acquisition,monkeypatch):
    import flakefinder.autofocus as af
    from test_focus_recovery import Stage
    old,run,c,config,_,initial=acquisition;scope=Stage()
    config['camera_transform']={'matrix':[[.17,0],[0,.17]],'pixel_size_um':[.17,.17],'calibrated':True}
    c['reference_focus_recovery']['fixed_roi_box_px']=[5,5,35,35]
    def metrics(rgb,mask=None,**kwargs):
        z=float(rgb[0,0,0]);return {'score':float(.00017+.0015*np.exp(-.5*((z-104.)/2.)**2)),
            'texture':.1,'noise':.001,'mean':1000.}
    monkeypatch.setattr(af,'focus_metrics',metrics);monkeypatch.setattr(focus_recovery,'focus_metrics',metrics)
    monkeypatch.setattr(focus_recovery,'recover_focus',REAL_RECOVER)
    monkeypatch.setattr(vendor_lifecycle,'acknowledge_raw',lambda *a:{'synthetic':True})
    frame,audit,ack,proof=refocus.acquire_reference(scope,config,{},run,scope.position(),lambda:None,lambda *a,**k:None,initial,c,
        lambda *a:{'accepted':True})
    result=cont.read(run/'initial-edge-focus/focus-result.json');report=cont.read(run/'initial-edge-focus/recovery.json')
    assert result['success'] and abs(frame.position.z-104.)<.3
    assert proof['budget']['captures_started']==scope.frames==result['budget']['captures_started']+1
    assert 10<scope.frames<64 and report['confirmation']
    assert report['samples'][0]['phase']=='initial' and report['samples'][1]['phase']=='initial_repeat'
    assert load_frame(run/'initial-edge-focus/verified_focus.tiff').metadata['focus_only_roi_source_px']==[5,5,35,35]
    assert cont.read(run/'initial-edge-focus/budget.json')['shared_with_parent'] is True

def test_worker_measured_reference_seeds_campaign_and_never_calls_old_focus(acquisition,monkeypatch):
    from flakefinder import scan_worker as worker,initial_edge_focus
    from flakefinder.focus_checkpoint import find_local_focus
    from flakefinder.memory_progress import prepare_progress
    from flakefinder.model import CameraTransform
    from test_scan_continuation import MovingFake
    from test_scan_worker import profile,settings,resources
    old,run,c,cfg,template,_=acquisition;project=run.parent.parent;opts=settings()
    config,bounds=worker.fresh_config(profile(),opts,project,run.name,template.position())
    donor=c['reference_focus_recovery']['donor_contract']
    c.update(schema=1,version=cont.VERSION,limits=config['limits'],search_bounds_um=bounds,
        reference_origin_um=asdict(template.position()),prior_coordinate_frame_id='fixed-frame',deadline_at=time.time()+3600,
        preserve_pre_raster_progress=True)
    donor['preserve_pre_raster_progress']=True
    # Thirteen original raw seeds and four superseded boundary attempts keep
    # candidate numbering and every lattice coordinate stable after new focus.
    search={'probe_count':784,'probe_centres_um':[[i,0] for i in range(784)],'start_xy_um':[1,2],'initial_nearest_order':list(range(784))}
    seeds=[]
    for i in range(13):
        name=f'search/frames/seed_{i:06d}';save_frame(old/Path(name).parent,Path(name).name,template.frame)
        paths=[name+suffix for suffix in ('.tiff','.json')]
        seeds.append({'index':None if i==0 else i-1,'image':paths[0],'requested_xy_um':[1,2],
            'hashes':{n:cont.digest(old/n) for n in paths},'classification':'substrate_seed'})
    write(old/'search-plan.json',search)
    write(old/'batch.json',{'coordinate_frame_id':'fixed-frame','search_bounds_um':bounds,'chip_states':{},'current_chip':None,
        'seed_records':seeds,'boundary_attempts':[{'id':f'candidate{i:03d}','state':'failed'} for i in range(1,5)]})
    donor['prior_files_sha256']={n:cont.digest(old/n) for n in ['search-plan.json','batch.json',*[n for seed in seeds for n in seed['hashes']]]}
    write(run/'scan-request.json',{'schema':1,'id':run.name,'created_at':time.time(),'source_release':'synthetic',
        'settings':opts,'continuation':c,'focus_first':{'reuse_verified_from':old.name}})
    write(run/'scan-status.json',{'state':'draft'})
    monkeypatch.setattr(cont,'verify_contract',lambda *a,**k:(config,profile()))
    monkeypatch.setattr(cont,'continuation_settings',lambda *a:opts)
    monkeypatch.setattr(cont,'verify_stationary_checkpoint',lambda *a:{'verified_before_arm':True})
    monkeypatch.setattr(initial_edge_focus,'focus_saved_edge',lambda *a,**k:pytest.fail('Old Z reuse must never run'))
    owned=[];seen={}
    class Moving(MovingFake):
        def capture(self):self.events.append('capture');return tagged(replace(template.frame,position=self.position(),acquired_at=time.time()))
    def factory(cfg):owned.append(Moving(cfg,asdict(template.position())));return owned[-1]
    def campaign(cfg,reference,output,actual_bounds,options,**kwargs):
        scope=kwargs['scope'];fresh=load_frame(reference)
        assert scope.position().z==fresh.position.z==kwargs['seed_focus_z_um']==132.
        assert cont.read(kwargs['seed_focus_evidence'])['z_um']==132.
        assert repair.preserved_search(run,{**search,'start_xy_um':[99,99]})==search
        state={'coordinate_frame_id':'fixed-frame','search_bounds_um':actual_bounds}
        imported=prepare_progress(run,state,cfg,fresh,CameraTransform(**cfg['camera_transform']),search)
        assert state['progress_continuation']['preserved_seed_count']==13
        assert len(imported['pending'])==772 and imported['discoveries'][0][1]['id']=='candidate005'
        assert all(a['state']=='superseded_reference_labels' for a in state['boundary_attempts'][:4])
        assert find_local_focus(run/'discovery/candidate005',cfg['context'],[1,2],fresh) is None
        seen['seed']=kwargs['seed_focus_z_um'];return {'state':'chip_budget_reached','chip_states':{}}
    outcome=worker.run_scan_worker(project,run.name,scope_factory=factory,profile_loader=profile,
        campaign_runner=campaign,hold_check=lambda:None,resource_reader=resources)
    assert outcome['state']=='chip_budget_reached',outcome
    assert outcome['hardware_closed'] and seen['seed']==132.
    assert cont.read(run/'reference-correction-verification.json')['accepted']

@pytest.mark.parametrize('change',['missing_session','wrong_folder','wrong_id','wrong_root','folder_exists','wrong_shape'])
def test_raw_receipt_requires_exact_job_session_and_project_folder(acquisition,change):
    old,run,c,cfg,scope,_=acquisition;frame,audit,ack,proof=acquire(acquisition)
    frame=copy.deepcopy(frame);ack=copy.deepcopy(ack);capture=frame.metadata['zen_vendor_result']
    if change=='missing_session':capture.pop('adapter_session_id')
    elif change=='wrong_folder':ack['vendor_result_folder']=str(run/'unrelated-absent')
    elif change=='wrong_id':capture['capture_id']='f'*32
    elif change=='wrong_root':
        capture['result_folder']=str(run/'wrong-root'/capture['capture_id']);ack['vendor_result_folder']=capture['result_folder']
    elif change=='folder_exists':Path(capture['result_folder']).mkdir(parents=True)
    else:capture['raw_shape']=[1,1,3]
    with pytest.raises(SafetyError):refocus.validate_raw_receipt(frame,run/'initial-edge-focus/focused-full-field.tiff',ack)

def test_late_control_verification_cannot_accept_or_seed_campaign(acquisition,monkeypatch):
    old,run,c,cfg,scope,initial=acquisition;clock=[0.];real=acquisition_budget.AcquisitionBudget
    monkeypatch.setattr(acquisition_budget,'AcquisitionBudget',lambda seconds,captures:real(seconds,captures,clock=lambda:clock[0]))
    def verify(frame,proof):
        assert scope.frames==4
        result=repair.verify_fresh(frame,old,run,measured_focus=proof,persist=False)
        clock[0]=600.1
        return result
    with pytest.raises(acquisition_budget.BudgetExhausted):
        refocus.acquire_reference(scope,cfg,{},run,scope.position(),lambda:None,lambda *a,**k:None,initial,c,verify)
    assert (run/'initial-edge-focus/focused-full-field.zen-vendor.json').exists()
    assert not (run/'reference-correction-verification.json').exists()
    assert cont.read(run/'reference-refocus-budget.json')['elapsed_seconds']==600.1
    proof=cont.read(run/'reference-refocus-proof.json')
    assert proof['acceptance_state']=='measurement_only'
    with pytest.raises((SafetyError,FileNotFoundError)):refocus.campaign_seed(run,proof)
