import hashlib
import json
from pathlib import Path
import pytest
from flakefinder.project_catalog import ProjectCatalog
from flakefinder.dataset_cache import DatasetCache
from flakefinder.training_bundle import grouped_split, examples, export_bundle, train_cpu


@pytest.fixture
def catalog(tmp_path):
    root=tmp_path/'project'; root.mkdir()
    (root/'.flakefinder-project.json').write_text(json.dumps({'project_id':'test-project'}))
    chip=root/'data/demo/chip001'; (chip/'tiles').mkdir(parents=True)
    digest=hashlib.sha256(b'raw').hexdigest()
    manifest={'camera_shape':[12,20,3],'transform':{'pixel_size_um':[.1,.1]},'completed':{
        'tile_000000':{'position':{'x':1,'y':2,'z':3},'hashes':{'tiles/tile_000000.tiff':digest}}}}
    (chip/'manifest.json').write_text(json.dumps(manifest))
    (chip/'tiles/tile_000000.tiff').write_bytes(b'raw')
    (chip/'tiles/tile_000000.jpg').write_bytes(b'preview')
    return ProjectCatalog(root,'data/demo')


def test_manifest_index_and_path_confinement(catalog):
    result=catalog.catalog(); assert result['image_count']==1
    assert result['fields'][0]['source_identity']['shape_hw']==[12,20]
    for bad in ('../escape','C:/Windows','/escape','data/../../escape'):
        with pytest.raises(ValueError): catalog.resolve(bad)
    assert result['dataset_id']==catalog.catalog()['dataset_id']


def test_unassessable_is_not_an_empty_success(catalog):
    folder=catalog.campaign/'analysis-residue-sieves-campaign-01';folder.mkdir()
    (folder/'ledger.json').write_text(json.dumps({'chip001/tile_000000':{
        'excluded':True,'unassessable':True,'reason':'insufficient_spatial_support',
        'summary':{'counts':{},'assessed_regions':0}}}))
    assert catalog.catalog()['fields'][0]['analysis_status']=='unassessable'
    field=catalog.get_field('chip001/tile_000000')
    assert not field['assessment_completed'] and field['source_features_retained']
    assert field['analysis_exclusion_reason']=='insufficient_spatial_support'
    assert field['rows']==[]


def test_cache_is_verified_bounded_and_pinned(catalog,tmp_path):
    cache=DatasetCache(catalog,tmp_path/'cache',quota_bytes=3)
    row=catalog.catalog()['fields'][0]
    with cache.acquire(row['raw_rel'],row['source_sha256']) as source:
        assert source.read_bytes()==b'raw'
        (catalog.root/'other').write_bytes(b'new')
        with pytest.raises(OSError):
            with cache.acquire('other',hashlib.sha256(b'new').hexdigest()):pass
    with cache.acquire('other',hashlib.sha256(b'new').hexdigest()) as target:
        assert target.read_bytes()==b'new'
    assert not source.exists()
    assert catalog.resolve(row['raw_rel']).read_bytes()==b'raw'
    with pytest.raises(ValueError): DatasetCache(catalog,catalog.root/'bad-cache')
    with pytest.raises(ValueError):
        with cache.acquire('other','0'*64):pass


def test_group_splits_and_empty_training(catalog,tmp_path):
    split=grouped_split(['a','b','c','a']); assert split['a']==grouped_split(['c','a','b'])['a']
    assert set(split.values())=={'train','validation','test'}
    assert set(grouped_split(['a','b']).values())=={'train'}
    summary=export_bundle(catalog,{'regions':[]},tmp_path/'bundle')
    assert not summary['validation_ready'] and summary['examples']==0
    with pytest.raises(ValueError,match='independently labeled'):
        train_cpu(tmp_path/'bundle',tmp_path/'model.json')


def test_corrected_geometry_cannot_reuse_old_metrics(catalog):
    field=catalog.catalog()['fields'][0]
    field['rows']=[{'id':'r1','area_um2':50,'polygons_px':[[[1,1],[3,1],[3,3]]]}]
    field['metadata']={}
    catalog.get_field=lambda key:field
    human={'region_id':'r1','source_identity':field['source_identity'],'labels':{'semantic':'residue'},
        'human_geometry':[[[1,1],[4,1],[4,4]]],'label_origin':'human_review','revision':1,'head_sha256':'a'*64}
    human['source_proposal']={'proposal_fingerprint':hashlib.sha256(json.dumps(dict(id='r1',
        loops=field['rows'][0]['polygons_px'],bbox=None,mask_sha256=None),sort_keys=True).encode()).hexdigest()}
    result=examples(catalog,{'regions':[human]})[0]
    assert result['features']=={} and result['feature_remeasurement_required']
    field['rows'][0]['polygons_px'][0][0]=[2,2]
    with pytest.raises(ValueError,match='proposal geometry changed'):examples(catalog,{'regions':[human]})
    human['source_identity']={**human['source_identity'],'source_sha256':'0'*64}
    with pytest.raises(ValueError,match='Stale source'):examples(catalog,{'regions':[human]})


def test_transparent_cpu_training_and_leakage_guard(tmp_path):
    rows=[]
    for split in ('train','validation','test'):
        for i in range(12):
            rows.append(dict(split=split,group_id=split,labels={'semantic':'residue' if i<6 else 'flake_of_interest'},
                             features={'contrast_R':i/100.,'area_um2':25+i}))
    bundle=tmp_path/'bundle';bundle.mkdir()
    def write_rows():
        raw=''.join(json.dumps(r)+'\n' for r in rows).encode()
        (bundle/'examples.jsonl').write_bytes(raw)
        (bundle/'manifest.json').write_text(json.dumps({'examples_sha256':hashlib.sha256(raw).hexdigest()}))
    write_rows()
    result=train_cpu(bundle,tmp_path/'model.json')
    assert result['thickness_assignment_allowed'] is False
    assert result['reports']['test']['examples']==12 and len(result['coefficients'])==1
    rows[-1]['group_id']='train';write_rows()
    with pytest.raises(ValueError,match='Group leakage'):train_cpu(bundle,tmp_path/'leaked.json')
