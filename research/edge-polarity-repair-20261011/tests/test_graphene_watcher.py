"""Offline committed-input, control, resume and coordinate tests; no clients."""
from pathlib import Path
from types import SimpleNamespace
import importlib.util
import json

import cv2
import numpy as np
import pytest

spec=importlib.util.spec_from_file_location('graphene_watcher',Path(__file__).resolve().parents[1]/'scripts/watch_graphene_scan.py')
watch=importlib.util.module_from_spec(spec);spec.loader.exec_module(watch)


@pytest.fixture(autouse=True)
def simulated_ram(monkeypatch):
    from flakefinder import acquisition_resources
    monkeypatch.setattr(acquisition_resources,'available_memory_bytes',lambda:8*1024**3)


def fixture(tmp_path):
    project=tmp_path/'project';run=project/'data'/'scan-test';chip=run/'chip001'
    (chip/'tiles').mkdir(parents=True);(chip/'boundary-labels').mkdir()
    watch.save(project/'.flakefinder-project.json',{'project_id':'test-project'})
    watch.save(run/'scan-request.json',{'id':'scan-test','settings':{'material':'graphene','oxide_nm':90}})
    watch.save(run/'batch.json',{'state':'search_sampling_complete','coordinate_frame_id':'fresh-frame'})
    rgb=np.uint16(np.random.default_rng(7).normal(2000,4,(128,160,3)))
    rgb[40:65,45:80]=np.uint16(rgb[40:65,45:80]*[.9,.85,.94])
    # Second object is intentionally outside the conditional model; retain it.
    rgb[85:108,100:130]=np.uint16(rgb[85:108,100:130]*[1.25,.75,1.2])
    raw=chip/'tiles/tile_000001.tiff';assert cv2.imwrite(str(raw),rgb[:,:,::-1])
    watch.save(raw.with_suffix('.json'),{'position':{'x':100,'y':200,'z':300},'pixel_size_um':[.5,.5],
        'sequence':1,'acquired_at':1,'metadata':{'white_level':4095,'objective':{'magnification':20,'na':.4}}})
    labels=np.ones((128,160),np.uint8);labels[35:70,40:85]=0;labels[80:113,95:135]=0
    np.savez_compressed(chip/'boundary-labels/tile_000001.npz',labels=labels)
    record={'position':{'x':100,'y':200,'z':300},'hashes':{p.relative_to(chip).as_posix():watch.digest(p)
        for p in (raw,raw.with_suffix('.json'),chip/'boundary-labels/tile_000001.npz')}}
    watch.save(chip/'manifest.json',{'context':{'material':'graphene','oxide_nm':90},'camera_shape':[128,160,3],
        'transform':{'matrix':[[.5,0],[0,.5]],'pixel_size_um':[.5,.5],'calibrated':True},
        'plan':{'polygon_um':[[20,120],[180,120],[180,280],[20,280]]},'completed':{'tile_000001':record}})
    # A saved TIFF without a manifest commit is not work.
    (chip/'tiles/tile_000002.tiff').write_bytes(b'incomplete')
    return project,run,chip


def model():
    return {'kind':'conditional_optical_curve','material':'graphene','oxide_nm':90,
        'contrast_convention':'(substrate-flake)/substrate','thickness_nm':[0,.34,.68],
        'contrast_rgb':[[0,0,0],[.1,.15,.06],[.2,.3,.12]],
        'calibration_status':'synthetic_only','response':{'kind':'synthetic'},'layer_assignment_allowed':False,'caveats':['Synthetic test.']}


def test_pinned_request_commit_and_source_mutation(tmp_path):
    project,run,chip=fixture(tmp_path)
    assert watch.request_identity(project,'scan-test')[1]['project_id']=='test-project'
    with pytest.raises(ValueError,match='simple run'):watch.request_identity(project,'../scan-test')
    entries=watch.committed_fields(run);assert len(entries)==1
    watch.verify_source(entries[0])
    manifest=watch.read(chip/'manifest.json');manifest['transform']['matrix'][0][0]=.6
    watch.save(chip/'manifest.json',manifest)
    with pytest.raises(ValueError,match='geometry/context'):watch.verify_source(entries[0])


def test_field_source_coordinates_unknowns_and_negative_controls(tmp_path):
    from flakefinder.material_contrast import MaterialContrastClassifier
    project,run,chip=fixture(tmp_path);entry=watch.committed_fields(run)[0]
    result=watch.analyze_field(entry,watch.input_context(entry,'scan-test'),
        MaterialContrastClassifier(model(),model_covariance=np.eye(3)*.01**2),tmp_path/'result',lambda:None)
    assert result['counts']['shortlist']>=1 and result['counts']['uncertain']>=1
    assert result['background_control']['substrate_pixels']>1000
    assert result['material_identity_confirmed'] is False and not result['layer_assignment_allowed']
    assert not list((tmp_path/'result').glob('*.tiff'))
    with np.load(tmp_path/'result/masks.npz') as saved:
        assert saved['substrate_mask'].any() and saved['parent_labels'].max()>=2
        for row in result['rows']:
            x,y=row['revisit_source_pixel_xy'];assert saved['labels'][int(y),int(x)]==row['instance_label']
            assert row['stage_xy_um']==pytest.approx([100+.5*(x-79.5),200+.5*(y-63.5)])
            assert row['layer_number'] is None


def test_resume_idempotent_and_coordinate_frame_pinned(tmp_path,monkeypatch):
    project,run,chip=fixture(tmp_path)
    monkeypatch.setattr(watch,'check_resources',lambda output:None)
    monkeypatch.setattr(watch,'prepare_model',lambda output,model_path=None:model())
    args=SimpleNamespace(project=project,run_id='scan-test',model=None,field_timeout=60,poll_seconds=1,once=True)
    first=watch.run(args);out=run/'analysis-graphene-incremental'
    before=watch.digest(out/'ledger.json')
    second=watch.run(args)
    assert first['processed_fields']==second['processed_fields']==1
    assert first['counts']==second['counts'] and watch.digest(out/'ledger.json')==before
    assert len(list((out/'fields').iterdir()))==1
    assert watch.read(out/'plan.json')['coordinate_frame_id']=='fresh-frame'
    watch.save(run/'batch.json',{'state':'search_sampling_complete','coordinate_frame_id':'changed'})
    with pytest.raises(ValueError,match='coordinate frame'):watch.run(args)
    assert watch.read(out/'status.json')['state']=='failed_closed'


def test_analysis_stop_never_touches_scanner_stop_or_source(tmp_path,monkeypatch):
    project,run,chip=fixture(tmp_path);out=run/'analysis-graphene-incremental';out.mkdir()
    (out/'STOP').write_text('analysis only');(run/'STOP').write_text('operator scanner hold')
    raw_hash=watch.digest(chip/'tiles/tile_000001.tiff')
    monkeypatch.setattr(watch,'check_resources',lambda output:None)
    args=SimpleNamespace(project=project,run_id='scan-test',model=None,field_timeout=60,poll_seconds=1,once=True)
    assert watch.run(args)['state']=='stopped'
    assert (out/'STOP').read_text()=='analysis only' and (run/'STOP').read_text()=='operator scanner hold'
    assert watch.digest(chip/'tiles/tile_000001.tiff')==raw_hash and not (out/'ledger.json').exists()


def test_low_ram_yields_without_model_or_field_allocation(tmp_path,monkeypatch):
    project,run,chip=fixture(tmp_path)
    monkeypatch.setattr(watch,'check_resources',lambda output:(_ for _ in ()).throw(watch.MemoryPaused('low RAM')))
    monkeypatch.setattr(watch,'prepare_model',lambda *args:pytest.fail('Must not allocate model while yielding'))
    args=SimpleNamespace(project=project,run_id='scan-test',model=None,field_timeout=60,poll_seconds=1,once=True)
    assert watch.run(args)['state']=='resource_paused'
    assert not (run/'analysis-graphene-incremental/ledger.json').exists()


def test_actual_resource_threshold_keeps_two_gib_analysis_allowance(tmp_path,monkeypatch):
    from flakefinder import acquisition_resources,storage
    monkeypatch.setattr(storage,'verify_project',lambda **kwargs:tmp_path)
    monkeypatch.setattr(watch.shutil,'disk_usage',lambda path:SimpleNamespace(free=20*1024**3))
    monkeypatch.setattr(acquisition_resources,'available_memory_bytes',lambda:int(3.9*1024**3))
    with pytest.raises(watch.MemoryPaused,match='4GiB'):watch.check_resources(tmp_path)
    monkeypatch.setattr(acquisition_resources,'available_memory_bytes',lambda:int(4.1*1024**3))
    watch.check_resources(tmp_path)


def test_midfield_memory_abort_leaves_no_commit(tmp_path,monkeypatch):
    from flakefinder import acquisition_resources
    project,run,chip=fixture(tmp_path)
    monkeypatch.setattr(watch,'check_resources',lambda output:None)
    monkeypatch.setattr(watch,'prepare_model',lambda output,model_path=None:model())
    monkeypatch.setattr(acquisition_resources,'available_memory_bytes',lambda:int(2.4*1024**3))
    args=SimpleNamespace(project=project,run_id='scan-test',model=None,field_timeout=60,poll_seconds=1,once=True)
    state=watch.run(args)
    assert state['state']=='resource_paused' and state['processed_fields']==0
    assert not (run/'analysis-graphene-incremental/ledger.json').exists()


def test_budget_abort_not_committed_as_empty_success(tmp_path,monkeypatch):
    project,run,chip=fixture(tmp_path)
    monkeypatch.setattr(watch,'check_resources',lambda output:None)
    monkeypatch.setattr(watch,'prepare_model',lambda output,model_path=None:model())
    monkeypatch.setattr(watch,'analyze_field',lambda *args:(_ for _ in ()).throw(watch.FieldBudget('synthetic time budget')))
    args=SimpleNamespace(project=project,run_id='scan-test',model=None,field_timeout=60,poll_seconds=1,once=True)
    result=watch.run(args);out=run/'analysis-graphene-incremental';commit=watch.read(out/'ledger.json')['chip001/tile_000001']
    field=watch.read(out/commit['field_json'])
    assert result['unassessable_fields']==1 and field['assessment_state']=='analysis_budget_exhausted'
    assert field['source_features_not_disproved'] is True


def test_segmentation_cancel_checked_before_work():
    from flakefinder.material_terraces import segment_material_terraces
    def stop():raise watch.AnalysisStop('test')
    with pytest.raises(watch.AnalysisStop):
        segment_material_terraces(None,None,None,None,None,None,check_cancel=stop)


def test_conditional_float32_thickness_does_not_shift_grid_bin():
    from flakefinder.material_contrast import MaterialContrastClassifier
    classifier=MaterialContrastClassifier(model(),model_covariance=np.eye(3)*.01**2)
    result=classifier.classify([[.1,.15,.06],[.2,.3,.12]],np.eye(3)*.002**2)
    assert result['class_index'].tolist()==[2,3]


def test_default_ensemble_uses_explicit_conditional_vendor_response(tmp_path):
    from flakefinder.material_contrast import MaterialContrastClassifier
    prediction=watch.prepare_model(tmp_path)
    assert prediction['material']=='graphene' and prediction['oxide_nm']==90
    assert len(prediction['scenario_provenance'])==36
    assert prediction['watcher_assumptions']['gamma_measured_for_this_run'] is False
    assert prediction['watcher_assumptions']['vendor_response_sha256']
    assert not prediction['layer_assignment_allowed']
    result=MaterialContrastClassifier(prediction,model_covariance=np.eye(3)*.01**2)([[0,0,0]],np.eye(3)*.002**2)
    assert result['class_index'].tolist()==[0] and not result['material_compatible'][0]
