import hashlib
import json
import pytest

from flakefinder.graphene_review import GrapheneReview


def save(path,value):
    path.parent.mkdir(parents=True,exist_ok=True)
    raw=json.dumps(value).encode();path.write_bytes(raw)
    return hashlib.sha256(raw).hexdigest()


@pytest.fixture
def saved(tmp_path):
    run_id='scan-20261009-193203-2b4068ad';run=tmp_path/'data'/run_id
    digest=save(run/'scan-request.json',{'id':run_id,'settings':{'material':'graphene','oxide_nm':90,'sample_id':'Test graphene'}})
    output=run/'analysis-graphene-incremental';field='chip001/tile_000001';rows=[]
    for label,area,disposition in [(1,100,'shortlist'),(2,50,'uncertain'),(3,8,'uncertain')]:
        rows.append(dict(id=field+'/'+str(label),field=field,area_um2=area,rank=100-label,
            disposition=disposition,filter_reasons=[] if label==1 else ['color_unclassified'],
            sieve_results=[],preview='chip001/tiles/tile_000001.jpg',source_shape_hw=[100,100],
            bbox_px=[10,10,20,20],polygons_px=[[[10,10],[30,10],[30,30],[10,30]]]))
    relative='fields/example/field.json';sha=save(output/relative,{'field':field,'rows':rows})
    ledger={field:{'field_json':relative,'hashes':{relative:sha}}};save(output/'ledger.json',ledger)
    save(output/'status.json',{'run_id':run_id,'scan_request_sha256':digest,'state':'waiting_for_committed_tiles','processed_fields':1,'available_fields':2})
    save(run/'chip001'/'manifest.json',{'completed':{'tile_000001':{},'tile_000002':{}}})
    return GrapheneReview(tmp_path),run_id,run,output,ledger


def test_indexes_all_shards_not_top_catalog_and_keeps_pending_images(saved):
    reader,run_id,run,output,_=saved
    save(output/'catalog.json',{'rows':[]})
    value=reader.data(run_id,category='all',minimum_area=25,limit=1)
    assert value['filtered_count']==2 and len(value['rows'])==1
    assert value['counts']=={'shortlist':1,'uncertain':1,'rejected':1,'all':2}
    assert value['source_region_count']==3
    assert value['fields'][1]['state']=='pending'
    rejected=reader.data(run_id,category='rejected')
    assert rejected['rows'][0]['disposition']=='uncertain'
    assert rejected['rows'][0]['filter_reasons']==['color_unclassified']
    assert reader.data(run_id,category='all',minimum_area=0)['filtered_count']==3
    pending=reader.data(run_id,field='chip001/tile_000002')
    assert pending['source']['state']=='pending' and pending['rows']==[]


def test_missing_analysis_never_falls_back_to_historical_material(tmp_path):
    run_id='scan-20261009-193203-2b4068ad';run=tmp_path/'data'/run_id
    save(run/'scan-request.json',{'id':run_id,'settings':{'material':'graphene','oxide_nm':90}})
    value=GrapheneReview(tmp_path).data(run_id)
    assert value['status']['state']=='not_started' and value['rows']==[]
    save(run/'scan-request.json',{'id':run_id,'settings':{'material':'MoTe2','oxide_nm':285}})
    with pytest.raises(ValueError,match='graphene'):GrapheneReview(tmp_path).data(run_id)


def test_checksums_run_identity_and_path_confinement(saved):
    reader,run_id,run,output,ledger=saved
    with pytest.raises(ValueError):reader.data('../another-run')
    reader.data(run_id)
    (output/'fields/example/field.json').write_text('{"field":"wrong","rows":[]}')
    value=reader.data(run_id)
    assert not value['rows'] and any('checksum mismatch' in w for w in value['warnings'])
    escaped={'chip001/tile_000003':{'field_json':'../outside.json','hashes':{'../outside.json':'a'*64}}}
    save(output/'ledger.json',escaped)
    value=reader.data(run_id,category='all')
    assert value['source_region_count']==0 and value['warnings']
    save(output/'status.json',{'run_id':run_id,'scan_request_sha256':'b'*64})
    with pytest.raises(ValueError,match='pinned'):reader.data(run_id)


def test_unassessable_and_atomic_status_gap_remain_explicit(saved):
    reader,run_id,run,output,ledger=saved
    relative='fields/unassessable/field.json';field='chip001/tile_000002'
    sha=save(output/relative,{'field':field,'rows':[],'assessment_state':'unassessable','reason':'No independent substrate support'})
    ledger[field]={'field_json':relative,'hashes':{relative:sha}};save(output/'ledger.json',ledger)
    value=reader.data(run_id,field=field)
    assert value['source']['state']=='unassessable' and 'substrate' in value['source']['reason']
    (output/'status.json').unlink()
    stale=reader.data(run_id)
    assert stale['stale'] and stale['status']['state']=='waiting_for_committed_tiles'
