"""Explicit campaign continuation preserves completed evidence and active budgets."""
from dataclasses import asdict
from pathlib import Path
import copy,hashlib,json,time

import numpy as np
import pytest

import flakefinder.chip_campaign as campaign
from flakefinder.full_chip import FullChipOptions
from flakefinder.imaging import save_frame
from flakefinder.layout import SubstrateReference,SUBSTRATE
from flakefinder.model import CameraTransform,Frame,Limits,Position,SafetyError,Cancelled,atomic_json


@pytest.fixture
def saved_campaign(tmp_path):
    transform=CameraTransform([[1,0],[0,1]],(1,1),True)
    limits=Limits(0,100,0,100,0,100)
    frame=Frame(np.full((64,64,3),[1800,1500,1950],np.uint16),Position(50,50,20),(1,1),0,
                metadata={'white_level':4095,'source':'TEST'})
    save_frame(tmp_path/'reference','raw',frame);reference=(tmp_path/'reference/raw.tiff').resolve()
    out=tmp_path/'campaign';out.mkdir()
    options=campaign.CampaignOptions(60,detectable_patch_diameter_um=10,walltime_budget_s=3600)
    full=asdict(FullChipOptions());bounds=[0.,100.,0.,100.]
    cfg={'backend':'zen','zen':{'job_template':'camera','job_output_dir':str(tmp_path/'vendor')},
         'limits':asdict(limits),'camera_transform':asdict(transform),'full_chip':full,'context':{'sample_id':'test'}}
    search=campaign.plan_campaign_search(bounds,60,[50,50],10)
    state={'state':'chip_complete_discovery_pending','current_chip':None,'coordinate_frame_id':'fresh-test',
        'options':asdict(options),'full_chip_options':full,'search_bounds_um':bounds,'reference_path':str(reference),
        'reference_sha256':campaign.file_hash(reference),'reference_position_um':asdict(frame.position),
        'reference_context':SubstrateReference.from_frame(frame).context,'seed_focus_bundle_provenance':None,
        'seed_focus':{'z_um':20},'elapsed_seconds':120.,'started_at':time.time()-86400,
        'algorithm_source_sha256':'old-code','chip_states':{},'seed_records':[],'boundary_attempts':[]}
    for name,value in [('batch.json',state),('configuration.json',cfg),('search-plan.json',search),
                       ('layout.json',{'coordinate_frame_id':'fresh-test','chips':[]})]:atomic_json(out/name,value)
    return cfg,reference,out,options,frame,transform,search


class FakeScope:
    def __init__(self,frame):self.frame=frame;self.pos=Position(50,50,20);self.closed=False;self.travels=[];self.stops=0
    def check_cancel(self):pass
    def arm(self,limits):self.limits=limits;self.armed=True
    def position(self):return self.pos
    def travel(self,x,y):self.travels.append([x,y]);self.pos=Position(x,y,self.pos.z)
    def capture(self):return Frame(self.frame.rgb.copy(),self.pos,self.frame.pixel_size_um,0,metadata=self.frame.metadata.copy())
    def stop(self):self.stops+=1
    def close(self):self.closed=True


def install_fake(monkeypatch,frame):
    scope=FakeScope(frame)
    monkeypatch.setattr(campaign,'connect',lambda cfg:scope)
    monkeypatch.setattr(campaign,'move_z_bounded',lambda scope,z:None)
    monkeypatch.setattr(campaign,'_seed_classification',lambda *a,**k:{'classification':'non_substrate_sample'})
    return scope


def add_completed_chip(saved):
    cfg,reference,out,options,frame,transform,search=saved
    folder=out/'chip001';folder.mkdir()
    save_frame(folder/'tiles','tile_000000',frame)
    labels=np.full((16,16),SUBSTRATE,np.uint8);(folder/'boundary-labels').mkdir()
    np.savez_compressed(folder/'boundary-labels/tile_000000.npz',labels=labels)
    plan={'tile_count':1,'tile_centres_um':[[50,50]],'seed_xy_um':[50,50],
        'scan_plan':{'bounds_um':[10,90,10,90],'polygon_um':[[10,10],[90,10],[90,90],[10,90]]},
        'options':asdict(FullChipOptions()),'transform':asdict(transform),'camera_shape':list(frame.rgb.shape),
        'support_evidence':{'support_cell_um':2}}
    plan['fingerprint']=hashlib.sha256(json.dumps(plan,sort_keys=True).encode()).hexdigest()
    atomic_json(out/'plans/chip001.json',plan)
    row={'position':asdict(frame.position),'hashes':{r:campaign.file_hash(folder/r) for r in
        ('tiles/tile_000000.json','boundary-labels/tile_000000.npz')}}
    manifest={'coverage_plan_fingerprint':plan['fingerprint'],'planned_area_acquired':True,
        'chip_extent_validated':False,'completed':{'tile_000000':row}}
    atomic_json(folder/'manifest.json',manifest)
    evidence=campaign.EvidenceMap([2,98,2,98],2)
    evidence.add(labels,[50,50],frame.rgb.shape,transform)
    np.savez_compressed(folder/'coverage-evidence.npz',flags=evidence.flags,cell_um=2,bounds_um=evidence.bounds)
    state=json.loads((out/'batch.json').read_text());state['chip_states']={'chip001':{'state':'complete_with_boundary_limits'}}
    atomic_json(out/'batch.json',state)
    layout=json.loads((out/'layout.json').read_text());layout['chips']=[{'id':'chip001'}];atomic_json(out/'layout.json',layout)
    return folder


def test_continue_replays_coverage_and_active_budget_without_rewriting_saved_data(saved_campaign,monkeypatch):
    cfg,reference,out,options,frame,transform,search=saved_campaign
    chip=add_completed_chip(saved_campaign);original=(chip/'tiles/tile_000000.tiff').read_bytes()
    original_config=(out/'configuration.json').read_bytes();cfg=copy.deepcopy(cfg);cfg['zen']['keep_vendor_results']=False
    scope=install_fake(monkeypatch,frame)
    result=campaign.run_chip_campaign(cfg,reference,out,[0,100,0,100],options,continue_discovery=True,
                                     continuation_max_chips=64,continuation_walltime_s=9*3600,allow_source_change=True)
    assert result['state']=='search_sampling_complete' and scope.closed and not scope.stops
    assert result['coordinate_frame_id']=='fresh-test' and result['options']['max_chips']==64
    assert result['options']['walltime_budget_s']==120+9*3600 and 120<=result['elapsed_seconds']<150
    assert result['budget_transitions'][0]['additional_active_seconds']==9*3600
    assert result['configuration_transitions'][0]['change']=={'zen.keep_vendor_results':False}
    assert (out/'configuration.json').read_bytes()==original_config
    assert campaign._same_json(json.loads((out/result['configuration_file']).read_text()),cfg)
    skipped=[r for r in result['seed_records'] if r['classification']=='skipped_observed_connected_substrate']
    assert skipped and all(r['requested_xy_um'] not in scope.travels for r in skipped)
    assert len(result['seed_records'])==search['probe_count'] and result['remaining_lattice_indices']==[]
    assert (chip/'tiles/tile_000000.tiff').read_bytes()==original
    assert result['continuation_attempts'][0]['coverage_replay'][0]['registered_fields_verified']==1


@pytest.mark.parametrize('change',['config','source','bounds','stop','labels','pending'])
def test_continuation_rejects_changed_evidence_before_write_or_connect(saved_campaign,monkeypatch,change):
    cfg,reference,out,options,frame,transform,search=saved_campaign
    chip=add_completed_chip(saved_campaign);bounds=[0,100,0,100]
    if change=='config':cfg=copy.deepcopy(cfg);cfg['context']['sample_id']='changed'
    if change=='bounds':bounds=[1,100,0,100]
    if change=='stop':(out/'STOP').touch()
    if change=='labels':np.savez_compressed(chip/'boundary-labels/tile_000000.npz',labels=np.zeros((16,16),np.uint8))
    if change=='pending':
        state=json.loads((out/'batch.json').read_text());state['remaining_lattice_indices']=[];atomic_json(out/'batch.json',state)
    before=(out/'batch.json').read_bytes()
    monkeypatch.setattr(campaign,'connect',lambda cfg:pytest.fail('Rejected continuation connected hardware'))
    with pytest.raises((SafetyError,Cancelled)):
        campaign.run_chip_campaign(cfg,reference,out,bounds,options,continue_discovery=True,allow_source_change=change!='source')
    assert (out/'batch.json').read_bytes()==before
    assert not (out/'configuration-revisions').exists()


def test_interrupted_travel_does_not_consume_lattice_index(saved_campaign,monkeypatch):
    cfg,reference,out,options,frame,transform,search=saved_campaign;scope=install_fake(monkeypatch,frame)
    def interrupted(x,y):raise Cancelled('test pause during movement')
    scope.travel=interrupted
    result=campaign.run_chip_campaign(cfg,reference,out,[0,100,0,100],options,continue_discovery=True,allow_source_change=True)
    assert result['state']=='stopped' and result['seed_records']==[]
    assert result['remaining_lattice_indices']==list(range(search['probe_count'])) and scope.stops==1
    scope=install_fake(monkeypatch,frame)
    result=campaign.run_chip_campaign(cfg,reference,out,[0,100,0,100],options,continue_discovery=True)
    assert result['state']=='search_sampling_complete' and len(scope.travels)==search['probe_count']


def test_retention_storage_removes_only_accumulated_scientific_vendor_copies(saved_campaign):
    cfg,_,out,_,_,_,_=saved_campaign
    plan={'tile_count':100,'anchor_xy_um':[[0,0],[1,0],[0,1],[1,1]],'options':asdict(FullChipOptions())}
    kwargs={'geometry':{'geometry_rejected_tiles':0},'disk_usage':lambda p:type('Usage',(),{'free':10**15})()}
    retained=campaign.estimate_campaign_storage(cfg,plan,30000000,out,**kwargs)
    cfg=copy.deepcopy(cfg);cfg['zen']['keep_vendor_results']=False
    released=campaign.estimate_campaign_storage(cfg,plan,30000000,out,**kwargs)
    assert retained['volumes'][0]['estimated_new_bytes']-released['volumes'][0]['estimated_new_bytes']>=99*30000000*1.25
    assert released['reserved_local_focus_attempts']==2 and released['scientific_vendor_peak_copies']==1
    assert released['planning_policy']=='expected_focus_with_per_capture_reserve_guard'

@pytest.mark.parametrize('typed',[True,False])
def test_only_typed_focus_failure_continues_after_stop_latch(saved_campaign,monkeypatch,typed):
    cfg,reference,out,options,frame,transform,search=saved_campaign
    scope=install_fake(monkeypatch,frame);scope.cancelled=False
    def check():
        if scope.cancelled:raise Cancelled('adapter cancellation latch')
    def arm(limits):scope.limits=limits;scope.cancelled=False;scope.armed=True
    scope.check_cancel=check;scope.arm=arm
    selected=[i for i,xy in enumerate(search['probe_centres_um']) if all(0<v<100 for v in xy)][:2]
    # Checkpoint all other lattice points so this focused test exercises two chips.
    state=json.loads((out/'batch.json').read_text())
    state['seed_records']=[{'index':i,'requested_xy_um':xy,'classification':'non_substrate_sample'}
                           for i,xy in enumerate(search['probe_centres_um']) if i not in selected]
    atomic_json(out/'batch.json',state)
    monkeypatch.setattr(campaign,'_seed_classification',lambda *a,**k:{'classification':'substrate_seed'})
    outline={'status':'estimated','polygon_um':[[1,1],[99,1],[99,99],[1,99]]}
    monkeypatch.setattr(campaign,'discover_sparse_outline',lambda *a,**k:{'state':'estimated','records':[], 'estimated_outline':outline})
    monkeypatch.setattr(campaign,'build_full_chip_plan',lambda *a,**k:{'tile_count':1,'focus_anchor_mode':'chip_edges',
        'scan_plan':{'polygon_um':[[30,30],[38,30],[38,38],[30,38]]}})
    monkeypatch.setattr(campaign,'focus_geometry_diagnostics',lambda plan:{'geometry_rejected_tiles':0})
    monkeypatch.setattr(campaign,'estimate_campaign_storage',lambda *a,**k:{'sufficient':True})
    attempts=[]
    def fail(scope,plan,ref,folder,**kwargs):
        attempts.append(folder.name);scope.cancelled=True
        atomic_json(folder/'manifest.json',{'state':'failed','completed':{},'tile_count':1})
        exception=campaign.FocusSupportError if typed else campaign.AcquisitionError
        raise exception('unresolved focus' if typed else 'hardware error')
    monkeypatch.setattr(campaign,'run_full_chip',fail)
    if typed:
        result=campaign.run_chip_campaign(cfg,reference,out,[0,100,0,100],options,continue_discovery=True,allow_source_change=True)
        assert len(attempts)==2 and result['state']=='search_sampling_complete'
        assert all(chip['state']=='focus_unresolved' for chip in result['chip_states'].values())
        assert result['current_chip'] is None
    else:
        with pytest.raises(campaign.AcquisitionError,match='hardware error'):
            campaign.run_chip_campaign(cfg,reference,out,[0,100,0,100],options,continue_discovery=True,allow_source_change=True)
        assert len(attempts)==1
        result=json.loads((out/'batch.json').read_text());assert result['state']=='failed'


def test_deferred_discovery_reuses_seed_and_boundary_without_recapturing(saved_campaign,monkeypatch):
    cfg,reference,out,options,frame,transform,search=saved_campaign;scope=install_fake(monkeypatch,frame)
    index=next(i for i,xy in enumerate(search['probe_centres_um']) if all(0<v<100 for v in xy))
    xy=search['probe_centres_um'][index]
    seed=Frame(frame.rgb.copy(),Position(*xy,20),frame.pixel_size_um,0,metadata=frame.metadata.copy())
    save_frame(out/'search/frames','seed_000000',seed)
    discovery=out/'discovery/candidate001';discovery.mkdir(parents=True)
    atomic_json(discovery/'discovery.json',{'state':'estimated','records':[],
        'estimated_outline':{'status':'estimated','polygon_um':[[1,1],[99,1],[99,99],[1,99]]}})
    record={'index':index,'requested_xy_um':xy,'position_um':asdict(seed.position),'classification':'substrate_seed',
        'image':'search/frames/seed_000000.tiff','hashes':{r:campaign.file_hash(out/r) for r in
        ('search/frames/seed_000000.tiff','search/frames/seed_000000.json')}}
    state=json.loads((out/'batch.json').read_text());state['seed_records']=[record]+[
        {'index':i,'requested_xy_um':xy,'classification':'non_substrate_sample'} for i,xy in enumerate(search['probe_centres_um']) if i!=index]
    state['boundary_attempts']=[{'id':'candidate001','seed_record_index':0,'seed_xy_um':xy,
        'folder':str(discovery),'state':'deferred_storage_budget'}]
    atomic_json(out/'batch.json',state)
    seed_before=(out/record['image']).read_bytes()
    scope.capture=lambda:pytest.fail('Deferred seed was recaptured')
    monkeypatch.setattr(campaign,'discover_sparse_outline',lambda *a,**k:pytest.fail('Finished edges were reacquired'))
    def no_support(*a,**k):raise campaign.AcquisitionError('test unresolved geometric support')
    monkeypatch.setattr(campaign,'build_full_chip_plan',no_support)
    result=campaign.run_chip_campaign(cfg,reference,out,[0,100,0,100],options,continue_discovery=True,allow_source_change=True)
    assert result['state']=='search_sampling_complete' and not scope.travels
    assert len(result['seed_records'])==search['probe_count'] and len(result['boundary_attempts'])==1
    assert (out/record['image']).read_bytes()==seed_before

@pytest.fixture
def failed_seed_campaign(saved_campaign):
    cfg,reference,out,options,frame,transform,search=saved_campaign
    index=next(i for i,xy in enumerate(search['probe_centres_um']) if all(0<v<100 for v in xy));xy=search['probe_centres_um'][index]
    seed=Frame(frame.rgb.copy(),Position(*xy,20),frame.pixel_size_um,0,metadata=frame.metadata.copy())
    save_frame(out/'search/frames','seed_000000',seed)
    folder=out/'discovery/candidate001';save_frame(folder/'frames','probe_000000',seed)
    (folder/'labels').mkdir();labels=np.zeros((16,16),np.uint8);labels[5:12,5:12]=SUBSTRATE
    np.savez_compressed(folder/'labels/probe_000000.npz',labels=labels)
    record={'phase':'seed','requested_xy_um':xy,'position_um':[*xy,20],
        'image':'frames/probe_000000.tiff','labels_file':'labels/probe_000000.npz','classification':'unknown',
        'hashes':{r:campaign.file_hash(folder/r) for r in ('frames/probe_000000.tiff','frames/probe_000000.json','labels/probe_000000.npz')}}
    payload={'schema':1,'algorithm':'sparse_coarse_bracket_halfplanes_v1','algorithm_source_sha256':'legacy',
        'edge_model_sha256':'edge-model','seed_xy_um':xy,'options':asdict(campaign.SparseEdgeOptions([0,100,0,100])),
        'context':cfg['context'],'transform':asdict(transform),'source_shape':list(frame.rgb.shape),
        'reference':SubstrateReference.from_frame(frame).to_dict(),'reference_rgb':[1800,1500,1950],
        'reference_pixels_sha256':hashlib.sha256(frame.rgb.tobytes()).hexdigest(),'limits':cfg['limits'],
        'field_rectangle_um':[64,64],'assumption':'one convex chip','coverage_kind':'estimated','all_chips_found':False,'coverage_requirement':'raster validation'}
    message='Seed centre is not verified substrate under this profile.'
    discovery={**payload,'fingerprint':hashlib.sha256(json.dumps(payload,sort_keys=True).encode()).hexdigest(),
        'state':'failed','error':message,'records':[record],'rays':{}}
    atomic_json(folder/'discovery.json',discovery)
    state=json.loads((out/'batch.json').read_text())
    state.update(state='failed',error=message)
    state['seed_records']=[{'index':index,'requested_xy_um':xy,'position_um':asdict(seed.position),'classification':'substrate_seed',
        'image':'search/frames/seed_000000.tiff','attempt_id':'candidate001','hashes':{r:campaign.file_hash(out/r) for r in
        ('search/frames/seed_000000.tiff','search/frames/seed_000000.json')}}]
    state['boundary_attempts']=[{'id':'candidate001','seed_record_index':0,'seed_xy_um':xy,'folder':str(folder),'state':'discovering'}]
    atomic_json(out/'batch.json',state)
    return saved_campaign,folder


def test_explicit_seed_only_failure_recovery_replays_and_keeps_budget(failed_seed_campaign,monkeypatch):
    saved,folder=failed_seed_campaign;cfg,reference,out,options,frame,transform,search=saved
    scope=install_fake(monkeypatch,frame);original_discovery=(folder/'discovery.json').read_bytes();calls=[]
    def replay(scope,xy,transform,reference,output,edge_options,**kwargs):
        assert output==folder and kwargs['resume'] and kwargs['continue_unresolved'];calls.append(output)
        return {'state':'unresolved','reason':'seed_not_verified','records':json.loads(original_discovery)['records']}
    monkeypatch.setattr(campaign,'discover_sparse_outline',replay)
    result=campaign.run_chip_campaign(cfg,reference,out,[0,100,0,100],options,continue_discovery=True,
        allow_source_change=True,recover_seed_rejection=True)
    assert len(calls)==1 and result['state']=='search_sampling_complete'
    assert result['options']['walltime_budget_s']==3600 and 120<=result['elapsed_seconds']<150
    assert not result.get('budget_transitions') and result['classification_recoveries'][0]['attempt_id']=='candidate001'
    assert result['boundary_attempts'][0]['state']=='unresolved'
    assert (folder/'discovery.json').read_bytes()==original_discovery


@pytest.mark.parametrize('change',['generic_error','second_frame','tampered_evidence','missing_flag','missing_source_flag','renew_budget'])
def test_seed_failure_recovery_rejects_other_failures_or_budget_renewal(failed_seed_campaign,monkeypatch,change):
    saved,folder=failed_seed_campaign;cfg,reference,out,options,frame,transform,search=saved
    if change=='generic_error':
        state=json.loads((out/'batch.json').read_text());state['error']='Stage movement failed';atomic_json(out/'batch.json',state)
    elif change=='second_frame':
        d=json.loads((folder/'discovery.json').read_text());d['records'].append(d['records'][0]);atomic_json(folder/'discovery.json',d)
    elif change=='tampered_evidence':(folder/'labels/probe_000000.npz').write_bytes(b'changed')
    before=(out/'batch.json').read_bytes()
    monkeypatch.setattr(campaign,'connect',lambda cfg:pytest.fail('Invalid recovery reached hardware'))
    with pytest.raises(SafetyError):
        campaign.run_chip_campaign(cfg,reference,out,[0,100,0,100],options,continue_discovery=True,
            allow_source_change=change!='missing_source_flag',recover_seed_rejection=change!='missing_flag',
            continuation_walltime_s=9*3600 if change=='renew_budget' else None)
    assert (out/'batch.json').read_bytes()==before
