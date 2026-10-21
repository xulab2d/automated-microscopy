"""Resume an interrupted chip without acquiring or rewriting prior evidence."""
from dataclasses import asdict
import copy,hashlib,json
from types import SimpleNamespace

import numpy as np
import pytest

import flakefinder.chip_campaign as campaign
import flakefinder.full_chip as full
from flakefinder.imaging import save_frame
from flakefinder.layout import SubstrateReference
from flakefinder.model import CameraTransform,Frame,Limits,Position,SafetyError,atomic_json


@pytest.fixture
def interrupted(tmp_path):
    transform=CameraTransform([[1,0],[0,1]],(1,1),True);limits=Limits(0,100,0,100,0,100)
    frame=Frame(np.full((64,64,3),[1800,1500,1950],np.uint16),Position(50,50,20),(1,1),0,
                metadata={'white_level':4095,'source':'TEST'})
    save_frame(tmp_path/'reference','raw',frame);reference=(tmp_path/'reference/raw.tiff').resolve()
    folder=tmp_path/'campaign';chip=folder/'chip001';chip.mkdir(parents=True)
    discovery=tmp_path/'discovery';discovery.mkdir();atomic_json(discovery/'discovery.json',{})
    (discovery/'evidence.npz').write_bytes(b'unchanged evidence')
    opts=campaign.CampaignOptions(100);full_options=asdict(full.FullChipOptions())
    cfg={'backend':'simulator','limits':asdict(limits),'camera_transform':asdict(transform),'full_chip':full_options,'context':{'sample_id':'test'}}
    plan={'schema':1,'kind':'test_coverage','chip_id':'chip001','scan_plan':{'bounds_um':[10,90,10,90],'polygon_um':[[10,10],[90,10],[90,90],[10,90]]},
          'tile_centres_um':[[40,40],[60,60]],'tile_count':2,'options':full_options,'transform':asdict(transform),
          'camera_shape':list(frame.rgb.shape),'acquisition_settings':full._camera_context(frame),
          'focus_reference_pixels_sha256':hashlib.sha256(frame.rgb.tobytes()).hexdigest(),
          'discovery_folder':str(discovery),'discovery_sha256':campaign.file_hash(discovery/'discovery.json'),
          'evidence_sha256':campaign.file_hash(discovery/'evidence.npz'),'anchor_xy_um':[[20,20],[80,20],[80,80],[20,80]]}
    plan['fingerprint']=hashlib.sha256(json.dumps(plan,sort_keys=True).encode()).hexdigest()
    payload={'schema':1,'plan':plan['scan_plan'],'transform':plan['transform'],'limits':asdict(limits),'context':cfg['context'],
        'acquisition_settings':plan['acquisition_settings'],'camera_shape':plan['camera_shape'],'source':'TEST','material_model':None,
        'coverage_plan_fingerprint':plan['fingerprint'],'full_chip_options':full_options,'prior_focus_provenance':None,
        'seed_focus_provenance':None,'coverage_kind':plan['kind'],'algorithm_source_sha256':'old-full-code'}
    manifest={**payload,'fingerprint':hashlib.sha256(json.dumps(payload,sort_keys=True).encode()).hexdigest(),
        'state':'stopped','tile_count':2,'completed':{'tile_000000':{'retained':'raw evidence'}},'focus_points_um':[[20,20,20]]}
    state={'state':'stopped','current_chip':'chip001','coordinate_frame_id':'fresh-test','options':asdict(opts),
           'full_chip_options':full_options,'search_bounds_um':[0.,100.,0.,100.],'reference_path':str(reference),
           'reference_sha256':campaign.file_hash(reference),'reference_position_um':asdict(frame.position),
           'reference_context':SubstrateReference.from_frame(frame).context,'seed_focus_bundle_provenance':None,
           'elapsed_seconds':10,'algorithm_source_sha256':'old-campaign-code','remaining_lattice_probes':7,
           'chip_states':{'chip001':{'state':'stopped','completed_tiles':1,'total_tiles':2}}}
    for relative,value in [('batch.json',state),('configuration.json',cfg),('layout.json',{'coordinate_frame_id':'fresh-test','chips':[{'id':'chip001'}]}),
                           ('plans/chip001.json',plan),('chip001/manifest.json',manifest)]:atomic_json(folder/relative,value)
    (chip/'retained.tiff').write_bytes(b'original saved tile')
    return cfg,reference,folder,opts


def test_resume_preserves_saved_chip_and_marks_further_discovery_pending(interrupted,monkeypatch):
    cfg,reference,folder,opts=interrupted
    class Scope:
        def __init__(self):self.closed=False
        def arm(self,limits):self.limits=limits
        def position(self):return Position(50,50,20)
        def check_cancel(self):pass
        def stop(self):raise AssertionError('Successful resume must not stop hardware')
        def close(self):self.closed=True
    scope=Scope();monkeypatch.setattr(campaign,'connect',lambda config:scope)
    monkeypatch.setattr(campaign,'estimate_campaign_storage',lambda *a,**k:{'sufficient':True})
    def resume(scope_arg,plan,ref,chip,**kwargs):
        assert kwargs['resume'] and kwargs['allow_source_change'] and scope_arg is scope
        before=json.loads((chip/'manifest.json').read_text())
        assert before['focus_points_um']==[[20,20,20]] and len(before['completed'])==1
        before['completed']['tile_000001']={'new':'acquired'}
        before.update(state='complete_with_boundary_limits',planned_area_acquired=True,chip_extent_validated=False)
        atomic_json(chip/'manifest.json',before)
        kwargs['callback']('tile',None,{'index':2,'total':2})
        return before
    monkeypatch.setattr(campaign,'run_full_chip',resume)
    result=campaign.run_chip_campaign(cfg,reference,folder,[0,100,0,100],opts,resume=True,allow_source_change=True)
    assert scope.closed and result['state']=='chip_complete_discovery_pending'
    assert result['remaining_lattice_probes']==7 and result['chip_states']['chip001']['completed_tiles']==2
    assert result['algorithm_transitions'][0]['from_sha256']=='old-campaign-code'
    assert result['resume_attempts'][0]['verified_focus_points_retained']==1
    assert (folder/'chip001/retained.tiff').read_bytes()==b'original saved tile'


@pytest.mark.parametrize('change',['config','source_permission'])
def test_resume_rejects_changed_provenance_before_connect_or_writes(interrupted,monkeypatch,change):
    cfg,reference,folder,opts=interrupted;before=(folder/'batch.json').read_bytes()
    monkeypatch.setattr(campaign,'connect',lambda config:pytest.fail('Rejected resume opened hardware'))
    if change=='config':cfg=copy.deepcopy(cfg);cfg['context']['sample_id']='other'
    with pytest.raises(SafetyError):
        campaign.run_chip_campaign(cfg,reference,folder,[0,100,0,100],opts,resume=True,allow_source_change=change!='source_permission')
    assert (folder/'batch.json').read_bytes()==before
