"""Offline driver contract tests: no camera, stage, or acquisition owner."""
import importlib.util,json,sys
from pathlib import Path
from types import SimpleNamespace
import numpy as np
import pytest

path=Path(__file__).resolve().parents[1]/'scripts/analyze_graphene_run.py'
spec=importlib.util.spec_from_file_location('offline_graphene_driver',path)
driver=importlib.util.module_from_spec(spec);spec.loader.exec_module(driver)


def test_reduction_means_masks_noise_and_odd_source_geometry():
    rng=np.random.default_rng(7);rgb=rng.normal(0,.01,(25,27,3)).astype(np.float32)
    valid=np.ones((25,27),bool);support=valid.copy();sat=np.zeros_like(valid)
    valid[0,0]=False;support[2,2]=False;sat[4,4]=True
    arrays={'contrast_rgb':rgb,'valid_mask':valid,'substrate_mask':support,'saturated_mask':sat,
        'noise_covariance':np.eye(3)*999,'pixel_size_um':np.array([.2,.3])}
    reduced,info=driver.reduce_arrays(arrays)
    assert reduced['contrast_rgb'].shape==(12,13,3)
    np.testing.assert_allclose(reduced['contrast_rgb'][1,1],rgb[2:4,2:4].mean(axis=(0,1)),atol=1e-9)
    assert not reduced['valid_mask'][0,0] and not reduced['substrate_mask'][1,1]
    assert reduced['saturated_mask'][2,2] and not reduced['substrate_mask'][2,2]
    np.testing.assert_allclose(reduced['pixel_size_um'],[.4,.6])
    assert np.max(reduced['noise_covariance'])<.001 and np.linalg.eigvalsh(reduced['noise_covariance']).min()>0
    assert info['omitted_bottom_rows']==info['omitted_right_columns']==1
    assert info['source_pixel_from_analysis_pixel']==[[2,0,.5],[0,2,.5]]
    assert rgb.shape==(25,27,3) and np.array_equal(arrays['valid_mask'],valid)


def test_source_coordinates_use_original_centre_and_registered_transform():
    labels=np.zeros((12,13),int);labels[4,6]=3
    region={'instance_label':3,'polygon_px':[[6,4],[7,4],[7,5]],'holes_px':[[[6,4]]]}
    entry={'transform':{'matrix':[[0,-.3],[-.2,0]]},'record':{'position':{'x':100,'y':200,'z':300}}}
    result=driver.source_geometry(region,labels,entry,{'factor':2,'source_shape_hw':[25,27]})
    assert result['revisit_source_pixel_xy']==[12.5,8.5]
    np.testing.assert_allclose(result['stage_xy_um'],[101.05,200.1])
    assert result['polygon_source_px'][1]==[14.5,8.5] and result['holes_source_px']==[[[12.5,8.5]]]


def test_insufficient_reduced_substrate_is_not_a_zero_background_pass():
    arrays={'contrast_rgb':np.zeros((20,20,3)),'valid_mask':np.ones((20,20),bool),
        'substrate_mask':np.zeros((20,20),bool),'saturated_mask':np.zeros((20,20),bool),'pixel_size_um':[1,1]}
    with pytest.raises(ValueError,match='Insufficient'):driver.reduce_arrays(arrays)


@pytest.mark.parametrize('selection',['tile_000999','tile_000000,tile_000000','../tile_000000'])
def test_only_exact_committed_tile_inputs(selection):
    with pytest.raises(ValueError):driver.select_fields({'completed':{'tile_000000':{}}},selection)


@pytest.fixture
def offline(tmp_path,monkeypatch):
    project=tmp_path/'project';run_id='scan-20261013-221727-131d9ced';run=project/'data'/run_id;chip=run/'chip001'
    chip.mkdir(parents=True)
    driver.watcher.save(project/'.flakefinder-project.json',{'project_id':'test'})
    driver.watcher.save(run/'scan-request.json',{'id':run_id,'settings':{'material':'graphene','oxide_nm':90}})
    driver.watcher.save(chip/'manifest.json',{'completed':{f'tile_{i:06d}':{'position':{'x':1,'y':2,'z':3}} for i in range(3)},
        'transform':{'matrix':[[1,0],[0,1]]},'context':{'material':'graphene','oxide_nm':90}})
    monkeypatch.setattr('flakefinder.storage.resolve_output_path',lambda p:Path(p))
    monkeypatch.setattr('flakefinder.acquisition_resources.available_memory_bytes',lambda:2*1024**3)
    monkeypatch.setattr(driver,'cpu_policy',lambda:{'threads':1,'priority':'below_normal'})
    monkeypatch.setattr(driver.watcher,'prepare_model',lambda _: {'material':'graphene','oxide_nm':90,'contrast_rgb':[[0,0,0]]})
    args=SimpleNamespace(project=project,run_id=run_id,chip='chip001',version='pilot-v1',tiles='all',factor=2,
        model=None,exploratory_context_mismatch=False,field_timeout=600,min_free_mib=768)
    return args,run,project/'analysis/graphene'/run_id/'chip001/pilot-v1'


def test_errors_are_distinct_from_empty_assessment_and_resume_is_pinned(offline,monkeypatch):
    args,run,out=offline;calls=[]
    def analyze(entry,model,attempt,args,check):
        calls.append(entry['tile'])
        if entry['tile']=='tile_000000':raise FileNotFoundError('missing committed metadata')
        if entry['tile']=='tile_000001':raise RuntimeError('segmentation failed')
        attempt.mkdir(parents=True);payload={'assessment_state':'assessed','regions':[]}
        driver.watcher.save(attempt/'field.json',payload);return payload
    monkeypatch.setattr(driver,'analyze_field',analyze)
    result=driver.run(args)
    assert result['counts']=={'assessed':1,'unassessable':1,'failed':1,'budget_exhausted':0,'invalid_source':0}
    ledger=driver.read(out/'ledger.json')
    assert ledger['tile_000000']['regions'] is None and ledger['tile_000002']['regions']==0
    driver.run(args);assert len(calls)==3
    args.factor=3
    with pytest.raises(ValueError,match='Pinned'):driver.run(args)
    assert not (run/'STOP').exists() and not (run/'analysis-graphene-incremental').exists()


def hbn_fixture(offline):
    args,run,_=offline
    for path,key in ((run/'scan-request.json','settings'),(run/'chip001/manifest.json','context')):
        value=driver.read(path);value[key]['material']='hBN';driver.watcher.save(path,value)
    args.model=run/'test-hbn-model.json'
    driver.watcher.save(args.model,{'material':'hBN','oxide_nm':90,'contrast_rgb':[[0,0,0]]})
    return args,run,args.project/'analysis/hbn'/args.run_id/'chip001/pilot-v1'


def test_hbn_saved_tiles_use_separate_namespace_and_explicit_model(offline,monkeypatch):
    args,run,out=hbn_fixture(offline)
    def analyze(entry,model,attempt,args,check):
        assert entry['context']['material']==model['material']=='hBN'
        attempt.mkdir(parents=True);payload={'assessment_state':'assessed','regions':[]}
        driver.watcher.save(attempt/'field.json',payload);return payload
    monkeypatch.setattr(driver,'analyze_field',analyze)
    assert driver.run(args)['counts']['assessed']==3
    assert (out/'ledger.json').is_file()
    assert not (args.project/'analysis/graphene').exists()
    assert not (run/'STOP').exists()


@pytest.mark.parametrize('model',[None,{'material':'graphene','oxide_nm':90}, {'material':'hBN','oxide_nm':285}])
def test_hbn_rejects_missing_or_wrong_material_model(offline,model):
    args,run,_=hbn_fixture(offline)
    if model is None:args.model=None
    else:driver.watcher.save(args.model,model)
    with pytest.raises(ValueError,match='explicit hBN|material/oxide'):driver.run(args)


def test_own_stop_preserves_source_and_uncommitted_field(offline,monkeypatch):
    args,run,out=offline;original=(run/'chip001/manifest.json').read_bytes()
    def stopped(entry,model,attempt,args,check):
        (out/'STOP').write_text('analysis only');check()
    monkeypatch.setattr(driver,'analyze_field',stopped)
    assert driver.run(args)['state']=='stopped'
    assert not (out/'ledger.json').exists() and not (run/'STOP').exists()
    assert (run/'chip001/manifest.json').read_bytes()==original


def test_committed_output_tampering_rejected_on_resume(offline,monkeypatch):
    args,run,out=offline
    monkeypatch.setattr(driver,'analyze_field',lambda *a:(_ for _ in ()).throw(FileNotFoundError('missing')))
    driver.run(args);ledger=driver.read(out/'ledger.json')
    (out/ledger['tile_000000']['field_json']).write_text('{}')
    with pytest.raises(ValueError,match='output changed'):driver.run(args)


def test_transient_read_retry_is_bounded_and_cancellable(monkeypatch):
    import errno
    calls=[];monkeypatch.setattr(driver.time,'sleep',lambda _:None)
    def transient():
        calls.append(1)
        if len(calls)<3:raise OSError(errno.EINVAL,'provider')
        return 'valid'
    assert driver.retry_read(transient)=='valid' and len(calls)==3
    def cancelled():raise driver.watcher.AnalysisStop('STOP')
    with pytest.raises(driver.watcher.AnalysisStop):driver.retry_read(transient,cancelled)


def test_last_field_global_manifest_change_is_fatal_not_unassessable(offline,monkeypatch):
    args,run,out=offline;args.tiles='tile_000002'
    def changed(entry,model,attempt,args,check):
        (run/'chip001/manifest.json').write_text('{}')
        raise FileNotFoundError('field interrupted')
    monkeypatch.setattr(driver,'analyze_field',changed)
    with pytest.raises(ValueError,match='manifest changed'):driver.run(args)
    assert not (out/'ledger.json').exists()
    assert driver.read(out/'status.json')['state']=='failed_closed'


def test_failure_taxonomy_preserves_quality_budget_and_source_distinctions():
    from flakefinder.model import AcquisitionError
    from flakefinder.terrace_domain import DomainExclusion
    cases=[(AcquisitionError('Substrate support cannot sustain spatial held-out fit validation.'),'unassessable'),
        (AcquisitionError('Nonpositive supported-domain background; fit rejected.'),'unassessable'),
        (DomainExclusion('edge','outside domain'),'unassessable'),
        (FileNotFoundError('missing sidecar'),'unassessable'),
        (driver.watcher.FieldBudget('elapsed'),'budget_exhausted'),
        (driver.SourceEvidenceError('hash mismatch'),'invalid_source'),
        (json.JSONDecodeError('corrupt JSON','?',0),'invalid_source'),
        (AcquisitionError('Missing or changed independently recorded substrate labels.'),'invalid_source'),
        (ValueError('unexpected shape bug'),'failed'),(AcquisitionError('unexpected decoder problem'),'failed')]
    for exc,expected in cases:assert driver.assessment_failure(exc)==expected


def test_source_hash_mismatch_has_typed_invalid_source(tmp_path):
    path=tmp_path/'raw.tiff';path.write_bytes(b'corrupt')
    entry={'chip':tmp_path,'source_hashes':{'raw.tiff':'0'*64}}
    with pytest.raises(driver.SourceEvidenceError,match='hash mismatch'):
        driver.verify_field(entry,lambda:None)


def test_budget_and_invalid_source_are_committed_without_false_zero_counts(offline,monkeypatch):
    args,run,out=offline;args.tiles='tile_000000,tile_000001'
    def error(entry,*rest):
        if entry['tile']=='tile_000000':raise driver.watcher.FieldBudget('timeout')
        raise driver.SourceEvidenceError('raw hash changed')
    monkeypatch.setattr(driver,'analyze_field',error)
    result=driver.run(args)
    assert result['counts']['budget_exhausted']==result['counts']['invalid_source']==1
    assert result['counts']['failed']==result['counts']['assessed']==0
    assert all(row['regions'] is None for row in driver.read(out/'ledger.json').values())


@pytest.mark.skipif(driver.os.name!='nt',reason='Windows-native current-process memory query')
def test_native_memory_without_psutil_has_pointer_safe_nonzero_counters(monkeypatch):
    monkeypatch.setitem(sys.modules,'psutil',None)
    info=driver.memory_snapshot()
    assert info['rss_bytes']>0 and info['private_bytes']>0
    assert info['process_peak_rss_bytes']>=info['rss_bytes']
    assert info['available_host_bytes']>0 and info['measurement'].startswith('Windows GetProcessMemoryInfo')
