"""Offline immutable upgrade, closure, evidence and deadline contracts."""
from datetime import datetime,timezone
from pathlib import Path
import json,time
import pytest
from flakefinder import rectangle_handoff as handoff
from flakefinder.model import SafetyError,atomic_json


@pytest.fixture
def donor(tmp_path,monkeypatch):
    project=tmp_path/'project';folder=project/'data'/'scan-20261013-212016-12345678';folder.mkdir(parents=True)
    request={'settings':{'hours':2},'source_release':{'files':{'old.py':'old'}}}
    atomic_json(folder/'scan-request.json',request);digest=handoff.file_hash(folder/'scan-request.json')
    atomic_json(folder/'scan-status.json',{'id':folder.name,'pid':100,'state':'stopped','hardware_closed':True,'request_sha256':digest})
    atomic_json(folder/'worker-start.json',{'id':folder.name,'pid':100,'request_sha256':digest,
        'started_at':datetime.fromtimestamp(time.time()-60,timezone.utc).isoformat()})
    raw=folder/'calibration/substrate.tiff';raw.parent.mkdir();raw.write_bytes(b'original verified reference')
    atomic_json(folder/'reference-validation.json',{'accepted':True,'raw_sha256':handoff.file_hash(raw)})
    atomic_json(folder/'scan-config.json',{'limits':{'x_min':0,'x_max':100,'y_min':0,'y_max':100,'z_min':0,'z_max':100}})
    atomic_json(folder/'camera-profile-used.json',{'calibration':'same'})
    atomic_json(folder/'configuration.json',{'saved':'config'})
    atomic_json(folder/'search-plan.json',{'probe_centres_um':[[1,1],[2,2]]})
    attempts=[]
    for i,state in enumerate(('unresolved','unresolved','discovering'),1):
        discovery=folder/'discovery'/f'candidate{i:03}'
        atomic_json(discovery/'discovery.json',{'state':state,'records':[{'preserved':'raw focus and edges'}]})
        atomic_json(discovery/'focus/focus-result.json',{'success':True,'z_um':20+i})
        (discovery/'raw.tiff').write_bytes(bytes([i])*16)
        attempts.append({'id':discovery.name,'folder':str(discovery),'state':state,'seed_record_index':i-1})
    (folder/'STOP').write_text('original STOP')
    (folder/'discovery/candidate003/STOP').write_text('original nested STOP')
    atomic_json(folder/'batch.json',{'coordinate_frame_id':'unchanged','reference_path':str(raw),
        'reference_sha256':handoff.file_hash(raw),'search_bounds_um':[0,100,0,100],
        'seed_records':[{'index':0},{'index':1},{'index':None}], 'remaining_lattice_indices':[2,3],
        'boundary_attempts':attempts,'chip_states':{},'state':'stopped'})
    atomic_json(folder/'layout.json',{'coordinate_frame_id':'unchanged','chips':[]})
    monkeypatch.setattr('flakefinder.scan_continuation.process_alive_strict',lambda pid:False)
    return project,folder


def test_copy_retains_all_candidates_focus_and_pending_search_without_touching_donor(donor):
    project,folder=donor;contract=handoff.build_contract(project,folder.name)
    before={name:handoff.file_hash(folder/name) for name in contract['prior_files_sha256']}
    out=folder.parent/'scan-20261013-220000-12345678';out.mkdir()
    atomic_json(out/'scan-request.json',{'rectangle_handoff':contract})
    state=handoff.prepare_run(project,out,contract)
    assert state['remaining_lattice_indices']==[2,3] and len(state['seed_records'])==3
    assert [a['pre_upgrade_state'] for a in state['boundary_attempts']]==['unresolved','unresolved','discovering']
    assert all(a['state']=='discovering' and Path(a['folder']).is_relative_to(out) for a in state['boundary_attempts'])
    assert state['reference_path']==str(folder/'calibration/substrate.tiff')
    assert state['rectangle_handoff']['deadline_at']==contract['deadline_at']
    assert (out/'reference-validation.json').read_bytes()==(folder/'reference-validation.json').read_bytes()
    assert not (out/'STOP').exists() and not (out/'discovery/candidate003/STOP').exists()
    assert (folder/'STOP').exists() and (folder/'discovery/candidate003/STOP').exists()
    assert all(handoff.file_hash(folder/name)==digest for name,digest in before.items())
    for i in range(1,4):
        relative=f'discovery/candidate{i:03}/focus/focus-result.json'
        assert (out/relative).read_bytes()==(folder/relative).read_bytes()
    with pytest.raises(SafetyError,match='already contains'):handoff.prepare_run(project,out,contract)


def test_next_handoff_keeps_original_reference_and_deadline(donor):
    project,folder=donor;contract=handoff.build_contract(project,folder.name)
    out=folder.parent/'scan-20261013-220000-12345678';out.mkdir()
    request={'rectangle_handoff':contract,'settings':contract['settings'],'source_release':{'files':{'new.py':'new'}}}
    atomic_json(out/'scan-request.json',request);handoff.prepare_run(project,out,contract)
    for name in ('scan-config.json','camera-profile-used.json'):
        atomic_json(out/name,handoff.read(folder/name))
    digest=handoff.file_hash(out/'scan-request.json')
    atomic_json(out/'scan-status.json',{'id':out.name,'pid':101,'state':'stopped','hardware_closed':True,'request_sha256':digest})
    atomic_json(out/'worker-start.json',{'id':out.name,'pid':101,'request_sha256':digest,
        'started_at':datetime.now(timezone.utc).isoformat()})
    again=handoff.build_contract(project,out.name)
    assert again['reference_path']==contract['reference_path'] and again['deadline_at']==contract['deadline_at']


def test_only_diagnosed_prepared_focus_failure_is_queued_without_rewriting_manifest(donor):
    project,folder=donor;batch=handoff.read(folder/'batch.json')
    error='No substrate-supported focus ROI or bounded local focus point for tile_000000; nearest is 2013.3 um away.'
    manifest={'state':'failed','anchors_prepared':True,'focus_points_um':[[1,1,20],[2,1,20],[2,2,20],[1,2,20]],
              'seed_focus_provenance':None,'completed':{'tile_000007':{'raw':'retained'}},'error':error,
              'coverage_plan_fingerprint':'plan','algorithm_source_sha256':'old-source'}
    atomic_json(folder/'chip001/manifest.json',manifest)
    atomic_json(folder/'plans/chip001.json',{'focus_anchor_mode':'chip_edges','fingerprint':'plan'})
    batch['chip_states']={'chip001':{'state':'focus_unresolved','error':error}}
    atomic_json(folder/'batch.json',batch)
    contract=handoff.build_contract(project,folder.name)
    assert [v['chip_id'] for v in contract['prepared_focus_retry_chips']]==['chip001']
    out=folder.parent/'scan-20261013-220000-12345678';out.mkdir()
    state=handoff.prepare_run(project,out,contract)
    assert state['current_chip']=='chip001' and state['handoff_resume_queue']==['chip001']
    assert (folder/'chip001/manifest.json').read_bytes()==(out/'chip001/manifest.json').read_bytes()
    for change in ({'anchors_prepared':False},{'error':'camera failure'},{'algorithm_source_sha256':handoff.file_hash(Path(handoff.__file__).with_name('full_chip.py'))}):
        atomic_json(folder/'chip001/manifest.json',{**manifest,**change})
        assert handoff.prepared_focus_retries(folder,batch)==[]


def test_current_chip_resume_finishes_before_rectangle_discovery(donor,monkeypatch):
    from flakefinder.chip_campaign import CampaignOptions
    from dataclasses import asdict
    project,folder=donor;contract=handoff.build_contract(project,folder.name)
    out=folder.parent/'scan-20261013-220000-12345678';out.mkdir()
    batch=handoff.read(folder/'batch.json');batch.update(current_chip='chip001',
        options=asdict(CampaignOptions(5000)),full_chip_options={})
    monkeypatch.setattr(handoff,'prepare_run',lambda *args,**kwargs:batch)
    monkeypatch.setattr(handoff,'verify_live',lambda *args:{'actual':'checkpoint'})
    monkeypatch.setattr('flakefinder.config.arm',lambda *args:None)
    monkeypatch.setattr('flakefinder.chip_campaign.validate_campaign_envelope',lambda *args:None)
    class Scope:
        def hardware_controls(self):return self
        def set_lamp_percent(self,value):return value
        def set_exposure_ms(self,value):pass
    calls=[]
    def runner(**kwargs):
        calls.append(kwargs)
        return {'state':'chip_complete_discovery_pending' if kwargs.get('resume') else 'search_sampling_complete','current_chip':None}
    config={'context':{'illumination_percent':80.,'exposure_ms':200.}}
    result=handoff.run_handoff(Scope(),project,out,contract,config,{}, {},lambda:None,lambda *a,**k:None,campaign_runner=runner)
    assert len(calls)==2 and calls[0]['resume'] and calls[1]['continue_discovery'] and calls[1]['rectangle_mode']
    assert 0<calls[1]['continuation_walltime_s']<=contract['deadline_at']-time.time()+1
    assert result['state']=='search_sampling_complete'


@pytest.mark.parametrize('change',['live','closure','raw','deadline'])
def test_changed_or_active_donor_cannot_be_imported(donor,change):
    project,folder=donor;contract=handoff.build_contract(project,folder.name)
    if change=='live':
        with pytest.raises(SafetyError,match='closed and absent'):
            handoff.verify_contract(project,contract,process_checker=lambda pid:True)
        return
    if change=='closure':
        value=handoff.read(folder/'scan-status.json');value['hardware_closed']=False;atomic_json(folder/'scan-status.json',value)
    elif change=='raw':(folder/'discovery/candidate002/raw.tiff').write_bytes(b'changed raw')
    else:
        value=handoff.read(folder/'worker-start.json');value['started_at']='2020-01-01T00:00:00+00:00';atomic_json(folder/'worker-start.json',value)
    with pytest.raises(SafetyError):handoff.verify_contract(project,contract)


def test_worker_handoff_skips_reference_bootstrap_and_closes_same_owner(run_folder,monkeypatch):
    from test_scan_worker import profile,settings,resources,FakeScope
    from flakefinder import scan_worker as worker
    project,out=run_folder;request=json.loads((out/'scan-request.json').read_text())
    contract={'settings':settings(),'deadline_at':time.time()+123,'prior_run_id':'saved'}
    request['rectangle_handoff']=contract;atomic_json(out/'scan-request.json',request)
    config,_=worker.fresh_config(profile(),settings(),project,out.name)
    monkeypatch.setattr(handoff,'verify_contract',lambda *a,**k:contract)
    monkeypatch.setattr(handoff,'source_config',lambda *a,**k:(config,profile()))
    calls=[]
    def execute(scope,*args,**kwargs):
        assert scope.lease.acquired and not scope.closed
        calls.append(scope)
        return {'state':'chip_budget_reached','chip_states':{},'all_chips_found':False}
    monkeypatch.setattr(handoff,'run_handoff',execute)
    monkeypatch.setattr(worker,'_capture_reference',lambda *a,**k:pytest.fail('Handoff captured a new reference'))
    result=worker.run_scan_worker(project,out.name,scope_factory=FakeScope,profile_loader=profile,
        hold_check=lambda:None,resource_reader=resources)
    assert result['state']=='chip_budget_reached' and result['hardware_closed'] and calls[0].closed
    assert not (out/'calibration').exists()


from test_scan_worker import run_folder


from test_chip_campaign_continuation import saved_campaign,install_fake


def test_campaign_reprocesses_saved_candidate_before_any_z_move(saved_campaign,monkeypatch):
    from dataclasses import asdict
    import flakefinder.chip_campaign as campaign
    from flakefinder.imaging import save_frame
    from flakefinder.model import Frame,Position
    from sparse_profile_fixture import discovery_contract
    cfg,reference,out,options,frame,transform,search=saved_campaign
    scope=install_fake(monkeypatch,frame)
    index=next(i for i,xy in enumerate(search['probe_centres_um']) if all(0<v<100 for v in xy))
    xy=search['probe_centres_um'][index]
    seed=Frame(frame.rgb.copy(),Position(*xy,20),frame.pixel_size_um,0,metadata=frame.metadata.copy())
    save_frame(out/'search/frames','seed_000000',seed)
    folder=out/'discovery/candidate001';folder.mkdir(parents=True)
    atomic_json(folder/'discovery.json',{**discovery_contract(frame),'state':'unresolved','records':[],
        'options':asdict(campaign.SparseEdgeOptions([0,100,0,100])), 'context':cfg['context']})
    state=handoff.read(out/'batch.json')
    state['seed_records']=[{'index':index,'requested_xy_um':xy,'position_um':asdict(seed.position),
        'classification':'substrate_seed','image':'search/frames/seed_000000.tiff','hashes':{}}]+[
        {'index':i,'requested_xy_um':point,'classification':'non_substrate_sample'}
        for i,point in enumerate(search['probe_centres_um']) if i!=index]
    state['boundary_attempts']=[{'id':'candidate001','seed_record_index':0,'seed_xy_um':xy,'folder':str(folder),'state':'discovering'}]
    atomic_json(out/'batch.json',state);atomic_json(out/'rectangle-handoff-proof.json',{'kind':handoff.KIND,'contract':{'deadline_at':time.time()+3600}})
    monkeypatch.setattr(campaign,'move_z_bounded',lambda *a,**k:pytest.fail('Saved raw reanalysis issued a Z move'))
    scope.capture=lambda:pytest.fail('Saved raw reanalysis recaptured the seed')
    calls=[]
    def reanalyse(*args,**kwargs):
        calls.append((args,kwargs))
        assert args[5].rectangle_mode and args[5].rectangle_additional_probes==0
        assert 0<kwargs['max_elapsed_s']<=3600 and 'max_total_captures' not in kwargs
        assert kwargs['resume'] and kwargs['continue_unresolved'] and kwargs['allow_source_change']
        return {'state':'unresolved','records':[],'estimated_outline':{'status':'unresolved'}}
    monkeypatch.setattr(campaign,'discover_sparse_outline',reanalyse)
    result=campaign.run_chip_campaign(cfg,reference,out,[0,100,0,100],options,
        continue_discovery=True,allow_source_change=True,rectangle_mode=True)
    assert len(calls)==1 and result['state']=='search_sampling_complete' and not scope.travels


def test_file_callbacks_preserve_contract_and_report_only_completed_work(donor):
    project,folder=donor;baseline=handoff.build_contract(project,folder.name);events=[];checks=[]
    assert handoff.build_contract(project,folder.name,check=lambda:checks.append(1),progress=events.append)==baseline
    assert len(checks)>len(events)>0
    assert all(e['file_complete'] and e['bytes_completed']==Path(e['file']).stat().st_size for e in events)
    assert handoff.verify_contract(project,baseline,progress=events.append)==baseline
    handoff.source_config(project,baseline,check=lambda:checks.append(1),progress=events.append)
    out=folder.parent/'scan-20261014-010000-12345678';out.mkdir()
    handoff.prepare_run(project,out,baseline,check=lambda:checks.append(1),progress=events.append)
    copies=[e for e in events if e['operation']=='copy']
    verified=[e for e in events if e['operation']=='verify_copy']
    assert copies and len(copies)==len(verified)
    assert all(Path(e['file']).is_relative_to(out) for e in copies)
    assert all(e['file_complete'] for e in copies+verified)


@pytest.mark.parametrize('operation',['hash','copy'])
@pytest.mark.parametrize('failure',['stop','deadline'])
def test_large_file_processing_checks_stop_and_deadline_between_blocks(tmp_path,monkeypatch,operation,failure):
    from flakefinder.model import Cancelled
    source=tmp_path/'source.tiff';source.write_bytes(b'x'*(12*1024*1024))
    target=tmp_path/'copied.tiff';original=handoff.file_hash(source);events=[];checks=[0]
    def check():
        checks[0]+=1
        if checks[0]>=3:
            if failure=='stop':raise Cancelled('test STOP while processing file')
            handoff._checkpoint(None,deadline=0)
    with pytest.raises(Cancelled if failure=='stop' else SafetyError,match='STOP|deadline'):
        if operation=='hash':handoff._hash_file(source,check=check,progress=events.append)
        else:handoff._copy_file(source,target,check=check,progress=events.append)
    assert events==[] and handoff.file_hash(source)==original
    if operation=='copy':assert target.stat().st_size==4*1024*1024


def test_cancellation_from_copy_progress_cannot_commit_handoff_proof(donor):
    from flakefinder.model import Cancelled
    project,folder=donor;contract=handoff.build_contract(project,folder.name)
    out=folder.parent/'scan-20261014-010000-12345678';out.mkdir()
    def progress(info):
        if info['operation']=='copy':raise Cancelled('STOP after copied file')
    with pytest.raises(Cancelled):handoff.prepare_run(project,out,contract,progress=progress)
    assert not (out/'rectangle-handoff-proof.json').exists()
    assert handoff.verify_contract(project,contract)==contract


def test_real_file_completion_refreshes_watchdog_but_checks_do_not(donor,monkeypatch):
    from flakefinder.scan_worker import WorkerWatchdog
    from test_scan_worker import resources
    project,folder=donor;run=folder.parent/'watch-test';run.mkdir();clock=[0.]
    watch=WorkerWatchdog(run,project,100000,lambda:None,resources,clock=lambda:clock[0])
    original=handoff._hash_file;progress=[]
    def slow_file(path,**kwargs):
        clock[0]+=100.
        return original(path,**kwargs)
    monkeypatch.setattr(handoff,'_hash_file',slow_file)
    def completed(info):
        progress.append(info);watch.progress('validating_handoff_files')
    def check():
        assert watch.tick();watch.check()
    handoff.build_contract(project,folder.name,check=check,progress=completed)
    assert clock[0]>180 and progress and not watch.triggered.is_set()
    previous=watch.last_progress;clock[0]+=100;check();assert watch.last_progress==previous
    clock[0]+=81;assert not watch.tick() and 'No acquisition progress' in watch.reason
