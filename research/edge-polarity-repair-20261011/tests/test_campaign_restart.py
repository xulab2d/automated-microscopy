"""Immutable restart-import contracts; no hardware connection or image replay."""
from dataclasses import asdict
import hashlib,json
from pathlib import Path
import numpy as np,pytest
import flakefinder.campaign_restart as restart
from flakefinder.model import Frame,Position,CameraTransform,SafetyError,Cancelled,atomic_json
from flakefinder.layout import SubstrateReference

def fixture(tmp_path):
    campaign=tmp_path/'campaign';source=campaign/'discovery/candidate010';source.mkdir(parents=True)
    frame=Frame(np.full((64,64,3),[1800,1500,1950],np.uint16),Position(20,20,10),(1,1),0,metadata={'white_level':4095})
    transform=CameraTransform([[1,0],[0,1]],(1,1),True)
    (source/'raw.tiff').write_bytes(b'immutable raw bytes');(source/'raw.json').write_text('{}')
    np.savez_compressed(source/'labels.npz',labels=np.ones((16,16),np.uint8));np.savez_compressed(source/'evidence.npz',flags=np.ones((16,16),np.uint8))
    original={'fingerprint':'original-v1-identity','seed_xy_um':[20,20],'options':{},'context':{},'transform':asdict(transform),
        'source_shape':[64,64,3],'reference':SubstrateReference.from_frame(frame).to_dict(),'reference_rgb':[1800,1500,1950],
        'reference_pixels_sha256':'pixels','limits':{},'records':[{'name':'raw','image':'raw.tiff','labels_file':'labels.npz',
        'position_um':[20,20,10],'edge':{'old':'unchanged'},'hashes':{name:restart.file_hash(source/name)for name in ('raw.tiff','raw.json','labels.npz')}}]}
    atomic_json(source/'discovery.json',original)
    edge={'image':'raw.tiff','normal':[1,0],'offset_um':50,'points_um':[[50,0],[50,50]]}
    fit={'status':'estimated','registered_consistency':{'accepted':True},'polygon_um':[[0,0],[50,0],[50,50],[0,50]],
        'lines':[{'source_images':['raw.tiff']}]}
    replay={'discovery_sha256':{'candidate010':restart.file_hash(source/'discovery.json')},'fits':{'candidate010':fit},
        'rows':[{'attempt':'candidate010','image':'raw','edge':edge,'validation':{'accepted':True}}],'staged_sources':{'recorded':'algorithm'}}
    replay_path=tmp_path/'replay.json';atomic_json(replay_path,replay)
    return campaign,source,frame,transform,replay_path

def test_import_preserves_original_identity_and_files(tmp_path,monkeypatch):
    campaign,source,frame,transform,replay=fixture(tmp_path);before={p.name:p.read_bytes()for p in source.iterdir()}
    monkeypatch.setattr(restart,'build_full_chip_plan',lambda *a,**k:{'fingerprint':'new derived plan'})
    output=tmp_path/'new-lineage';plan,lineage=restart.import_validated_outline(campaign,'candidate010',replay,output,frame,transform,None,'new-chip')
    assert {p.name:p.read_bytes()for p in source.iterdir()}==before
    imported=restart.read(output/'discovery.json')
    assert imported['lineage']['source_fingerprint']=='original-v1-identity' and imported['fingerprint']!='original-v1-identity'
    assert imported['acquisition_resume_allowed'] is False
    assert imported['records'][0]['original_edge']=={'old':'unchanged'}
    assert Path(imported['records'][0]['image'])==source/'raw.tiff'

def test_tampered_raw_refuses_import_before_output(tmp_path,monkeypatch):
    campaign,source,frame,transform,replay=fixture(tmp_path);(source/'raw.tiff').write_bytes(b'changed')
    output=tmp_path/'new-lineage'
    with pytest.raises(SafetyError,match='raw/label evidence changed'):
        restart.import_validated_outline(campaign,'candidate010',replay,output,frame,transform,None,'new-chip')
    assert not output.exists()

def test_changed_stage_coordinate_frame_rejected_before_reference_load(tmp_path,monkeypatch):
    campaign=tmp_path/'campaign';atomic_json(campaign/'batch.json',{'coordinate_frame_id':'different'})
    monkeypatch.setattr(restart,'load_frame',lambda *a:pytest.fail('Old coordinate reference was loaded'))
    with pytest.raises(SafetyError,match='coordinate frame differs'):
        restart.create_restart_plan(campaign,tmp_path/'new',tmp_path/'replay',expected_coordinate_frame_id='current')

def test_model_dedup_requires_overlap_not_nearby_seed():
    a=[[0,0],[100,0],[100,100],[0,100]]
    assert restart.same_chip_models(a,[[1,1],[101,1],[101,101],[1,101]])
    assert not restart.same_chip_models(a,[[110,0],[210,0],[210,100],[110,100]])

def test_old_frame_id_is_insufficient_after_sample_removal(tmp_path):
    plan={'coordinate_frame_id':'historical-frame','execution_policy':{'placement_status':'historical_unverified'}}
    plan['fingerprint']=restart.digest(plan);atomic_json(tmp_path/'restart-plan.json',plan)
    class ForbiddenScope:
        def __getattr__(self,name):pytest.fail('Historical queue touched hardware: '+name)
    with pytest.raises(SafetyError,match='Historical placement'):
        restart.run_imported_task(ForbiddenScope(),tmp_path,'chip',config={},confirmed_coordinate_frame_id='historical-frame')
