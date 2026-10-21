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


def test_current_chip_resume_finishes_before_rectangle_discovery(donor,monkeypatch):
    from flakefinder.chip_campaign import CampaignOptions
    from dataclasses import asdict
    project,folder=donor;contract=handoff.build_contract(project,folder.name)
    out=folder.parent/'scan-20261013-220000-12345678';out.mkdir()
    batch=handoff.read(folder/'batch.json');batch.update(current_chip='chip001',
        options=asdict(CampaignOptions(5000)),full_chip_options={})
    monkeypatch.setattr(handoff,'prepare_run',lambda *args:batch)
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
    atomic_json(out/'batch.json',state);atomic_json(out/'rectangle-handoff-proof.json',{'kind':handoff.KIND})
    monkeypatch.setattr(campaign,'move_z_bounded',lambda *a,**k:pytest.fail('Saved raw reanalysis issued a Z move'))
    scope.capture=lambda:pytest.fail('Saved raw reanalysis recaptured the seed')
    calls=[]
    def reanalyse(*args,**kwargs):
        calls.append((args,kwargs))
        assert args[5].rectangle_mode and args[5].rectangle_additional_probes==4
        assert kwargs['resume'] and kwargs['continue_unresolved'] and kwargs['allow_source_change']
        return {'state':'unresolved','records':[],'estimated_outline':{'status':'unresolved'}}
    monkeypatch.setattr(campaign,'discover_sparse_outline',reanalyse)
    result=campaign.run_chip_campaign(cfg,reference,out,[0,100,0,100],options,
        continue_discovery=True,allow_source_change=True,rectangle_mode=True)
    assert len(calls)==1 and result['state']=='search_sampling_complete' and not scope.travels
