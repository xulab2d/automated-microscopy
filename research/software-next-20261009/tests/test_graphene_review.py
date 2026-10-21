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


@pytest.fixture
def offline(tmp_path):
    run_id='scan-20261013-221727-131d9ced';run=tmp_path/'data'/run_id
    request_hash=save(run/'scan-request.json',{'id':run_id,'settings':{'material':'graphene','oxide_nm':90}})
    chip='chip001';tile='tile_000001';field=chip+'/'+tile
    source_hashes={f'tiles/{tile}.tiff':'a'*64,f'tiles/{tile}.json':'b'*64,f'boundary-labels/{tile}.npz':'c'*64}
    record={'hashes':source_hashes};manifest={'completed':{tile:record,'tile_000002':record}}
    manifest_hash=save(run/chip/'manifest.json',manifest)
    relative=f'analysis/graphene/{run_id}/{chip}/full-chip-conditional-v1';output=tmp_path/relative
    model={'material':'graphene','oxide_nm':90};save(output/'model.json',model)
    config={'tiles':[tile,'tile_000002'],'factor':2}
    plan={'pin':{'identity':{'run_id':run_id,'scan_request_sha256':request_hash},'chip':chip,
        'manifest_sha256':manifest_hash,'config':config,'config_sha256':GrapheneReview._canonical(config)},
        'model_sha256':GrapheneReview._canonical(model)}
    plan_hash=save(output/'plan.json',plan)
    selector={'schema':1,'run_id':run_id,'analysis_relative_path':relative,'plan_sha256':plan_hash}
    save(run/'graphene-analysis-selection.json',selector)
    regions=[]
    for label,selected,useful in [(1,True,True),(2,True,False),(3,False,True)]:
        regions.append({'instance_label':label,'area_um2':100.,'material_pixel_fraction':.7,'selected_for_review':selected,
            'sieve_results':[{'name':'useful_size','status':'pass' if useful else 'below_priority'}],
            'filter_reasons':[] if selected and useful else ['saved_filter'],
            'polygon_px':[[1,1],[4,1],[4,4]],'polygon_source_px':[[2.5,2.5],[8.5,2.5],[8.5,8.5]],
            'holes_source_px':[[[4.5,4.5],[6.5,4.5],[6.5,6.5]]],'source_shape_hw':[20,20],
            'stage_xy_um':[10.,20.]})
    payload={'field':field,'assessment_state':'assessed','regions':regions,'source_hashes':source_hashes,
        'source_record_sha256':GrapheneReview._canonical(record),'source_transform':{'matrix':[[1,0],[0,1]]},
        'classifier':{'model_sha256':plan['model_sha256']},'reduction':{'factor':2}}
    name='fields/one/field.json';sha=save(output/name,payload)
    second='fields/two/field.json';sha2=save(output/second,{'field':chip+'/tile_000002','assessment_state':'unassessable',
        'reason':'Missing required source evidence','regions':None,
        'source_hashes':{'tiles/tile_000002.tiff':None,'tiles/tile_000002.json':None,'boundary-labels/tile_000002.npz':None}})
    ledger={tile:{'state':'assessed','field_json':name,'hashes':{name:sha}},
        'tile_000002':{'state':'unassessable','field_json':second,'hashes':{second:sha2}}}
    save(output/'ledger.json',ledger);save(output/'status.json',{'state':'complete','finished_fields':2,'selected_fields':2})
    return {'reader':GrapheneReview(tmp_path),'run_id':run_id,'run':run,'output':output,'selector':selector,
        'plan':plan,'ledger':ledger,'payload':payload,'manifest':manifest}


def test_offline_mapping_status_uncertain_and_source_geometry(offline):
    f=offline;value=f['reader'].data(f['run_id'],category='all',minimum_area=0)
    assert value['status']['processed_fields']==2 and value['status']['assessed_fields']==1
    assert value['status']['unassessable_fields']==1 and value['status']['failed_fields']==0
    assert value['analysis_source']['kind']=='offline_selected' and value['indexed_fields']==2
    assert value['counts']=={'shortlist':1,'uncertain':2,'rejected':2,'all':3}
    row=value['rows'][0]
    assert row['id']=='chip001/tile_000001/1' and row['polygon_px']==f['payload']['regions'][0]['polygon_source_px']
    assert row['bbox_px']==[2.5,2.5,7.,7.] and len(row['polygons_px'])==2
    assert row['preview_url'].endswith('/data/'+f['run_id']+'/chip001/tiles/tile_000001.jpg')
    assert row['source_tiff_sha256']=='a'*64 and row['layer_number'] is None
    unassessable=f['reader'].data(f['run_id'],field='chip001/tile_000002')
    assert unassessable['source']['state']=='unassessable' and unassessable['rows']==[]
    for compact,_ in f['reader']._index.values():
        assert all(set(r)=={'id','field','disposition','area_um2','rank','filtered','contrast_rgb','optical_evidence'} for r in compact)


@pytest.mark.parametrize('target',['plan','model','manifest','request'])
def test_offline_pins_fail_closed_after_initial_read(offline,target):
    f=offline;f['reader'].data(f['run_id'])
    path={'plan':f['output']/'plan.json','model':f['output']/'model.json',
        'manifest':f['run']/'chip001/manifest.json','request':f['run']/'scan-request.json'}[target]
    value=json.loads(path.read_bytes());value['changed']=True;save(path,value)
    with pytest.raises(ValueError,match='checksum|pinned'):f['reader'].data(f['run_id'])


def test_offline_rejects_wrong_config_and_unselected_ledger_field(offline):
    f=offline;f['plan']['pin']['config']['factor']=3
    f['selector']['plan_sha256']=save(f['output']/'plan.json',f['plan'])
    save(f['run']/'graphene-analysis-selection.json',f['selector'])
    with pytest.raises(ValueError,match='config checksum'):f['reader'].data(f['run_id'])
    f['plan']['pin']['config_sha256']=GrapheneReview._canonical(f['plan']['pin']['config'])
    f['selector']['plan_sha256']=save(f['output']/'plan.json',f['plan']);save(f['run']/'graphene-analysis-selection.json',f['selector'])
    f['ledger']['tile_999999']=f['ledger']['tile_000001'];save(f['output']/'ledger.json',f['ledger'])
    with pytest.raises(ValueError,match='outside selected'):f['reader'].data(f['run_id'])


@pytest.mark.parametrize('change',['field','source','model','geometry'])
def test_offline_field_schema_cannot_bypass_pin_with_new_shard_hash(offline,change):
    f=offline;payload=f['payload']
    if change=='field':payload['field']='chip999/tile_000001'
    if change=='source':payload['source_hashes']['tiles/tile_000001.tiff']='d'*64
    if change=='model':payload['classifier']['model_sha256']='d'*64
    if change=='geometry':payload['regions'][0]['polygon_source_px'][0]=[1000,0]
    commit=f['ledger']['tile_000001'];commit['hashes'][commit['field_json']]=save(f['output']/commit['field_json'],payload)
    save(f['output']/'ledger.json',f['ledger'])
    value=f['reader'].data(f['run_id'],category='all')
    assert value['rows']==[] and value['warnings'] and value['fields'][0]['state']=='record_unavailable'


def test_offline_shard_tamper_and_path_escape(offline):
    f=offline;f['reader'].data(f['run_id'])
    (f['output']/'fields/one/field.json').write_text('{}')
    value=f['reader'].data(f['run_id']);assert not value['rows'] and any('checksum' in w for w in value['warnings'])
    f['selector']['analysis_relative_path']='../escape';save(f['run']/'graphene-analysis-selection.json',f['selector'])
    with pytest.raises(ValueError,match='inside'):f['reader'].data(f['run_id'])


def test_related_handoff_is_link_only_and_requires_exact_request_pin(offline):
    f=offline;root=f['run'].parent.parent;new_id='scan-20261014-105506-f3cb07a9';new=root/'data'/new_id
    donor_hash=hashlib.sha256((f['run']/'scan-request.json').read_bytes()).hexdigest()
    request={'id':new_id,'settings':{'material':'graphene'},'rectangle_handoff':{
        'prior_run_id':f['run_id'],'prior_files_sha256':{'scan-request.json':donor_hash}}}
    save(new/'scan-request.json',request)
    value=f['reader'].data(new_id)
    assert value['run_id']==new_id and value['rows']==[] and value['status']['state']=='not_started'
    assert value['related_analysis']['run_id']==f['run_id']
    request['rectangle_handoff']['prior_files_sha256']['scan-request.json']='d'*64;save(new/'scan-request.json',request)
    assert f['reader'].data(new_id)['related_analysis'] is None


def test_full_field_cache_is_bounded_and_not_duplicated_in_snapshots(tmp_path):
    reader=GrapheneReview(tmp_path)
    for n in range(15):
        field=f'chip001/tile_{n:06d}';name=f'fields/{n}/field.json'
        sha=save(tmp_path/name,{'field':field,'rows':[{'id':field+'/1','field':field}]})
        reader._shard(tmp_path,field,{'field_json':name,'hashes':{name:sha}})
    assert len(reader._shards)==12 and reader._snapshots=={}


def test_missing_offline_shard_keeps_other_fields_and_recorded_completion(offline):
    f=offline;(f['output']/'fields/one/field.json').unlink()
    value=f['reader'].data(f['run_id'],category='all')
    assert value['status']['processed_fields']==2 and value['status']['assessed_fields']==1
    assert value['indexed_fields']==1 and value['fields'][0]['state']=='record_unavailable'
    assert value['fields'][1]['state']=='unassessable' and value['warnings']
    assert value['source_region_count']==0 and value['rows']==[]


def test_color_plot_includes_small_unselected_and_no_old_ledger_versions(saved):
    import base64,struct
    reader,run_id,run,output,ledger=saved
    rel=ledger['chip001/tile_000001']['field_json']
    payload=json.loads((output/rel).read_text())
    for i,row in enumerate(payload['rows']):row['contrast_rgb']=[.1+i*.01,.2,.3]
    sha=save(output/rel,payload);ledger['chip001/tile_000001']['hashes'][rel]=sha
    save(output/'ledger.json',ledger)
    result=reader.color_data(run_id)
    assert result['count']==3 and len(result['proposals'])==1
    values=struct.unpack('<15f',base64.b64decode(result['points']))
    assert values[13]==8 and values[14]==0
    payload['rows']=payload['rows'][:1]
    sha=save(output/rel,payload);ledger['chip001/tile_000001']['hashes'][rel]=sha
    save(output/'ledger.json',ledger)
    assert reader.color_data(run_id)['count']==1
    save(output/'ledger.json',{})
    assert reader.color_data(run_id)['count']==0


def test_review_refinement_is_pinned_reversible_and_independent_of_color(offline):
    f=offline;reader=f['reader'];run=reader.root/'data'/f['run_id']
    selection=json.loads((run/'graphene-analysis-selection.json').read_text())
    ledger=json.loads((f['output']/'ledger.json').read_text())
    commit=ledger['tile_000001'];field='chip001/tile_000001';rid=field+'/1'
    derivative={'schema':1,'run_id':f['run_id'],'plan_sha256':selection['plan_sha256'],
        'source_fields':{field:commit['hashes'][commit['field_json']]},
        'regions':{rid:{'likely_artifact':True,'artifact_flags':['diffuse_shadow'],
            'polygons_px':[[[3,3],[8,3],[8,8]]],'refinement_status':'refined'}}}
    path=f['output'].parent/'review-v2'/'refinement.json';sha=save(path,derivative)
    selector={'schema':1,'run_id':f['run_id'],'plan_sha256':selection['plan_sha256'],
        'refinement':{'path':path.relative_to(reader.root).as_posix(),'sha256':sha}}
    save(run/'graphene-review-selection.json',selector)
    hidden=reader.data(f['run_id'])
    assert hidden['counts']['shortlist']==0 and hidden['counts']['uncertain']==2
    assert hidden['review_processing']['hidden_artifacts']==1
    visible=reader.data(f['run_id'],hide_artifacts=False)
    assert visible['rows'][0]['id']==rid and visible['rows'][0]['refinement']['likely_artifact']
    assert visible['rows'][0]['polygon_px']==f['payload']['regions'][0]['polygon_source_px']
    exact=reader.data(f['run_id'],category='all',observation=rid)
    assert len(exact['rows'])==1
    derivative['source_fields'][field]='0'*64
    selector['refinement']['sha256']=save(path,derivative);save(run/'graphene-review-selection.json',selector)
    with pytest.raises(ValueError,match='source field checksum'):reader.data(f['run_id'])


def test_grouped_counts_and_observation_toggle_preserve_originals(saved,monkeypatch):
    reader,run_id,run,output,ledger=saved
    group={'id':'g1','representative_id':'chip001/tile_000001/2',
        'observation_ids':['chip001/tile_000001/2','chip001/tile_000001/3']}
    monkeypatch.setattr(reader,'_review_annotations',lambda *args: ({},{'groups':[group],'minimum_area_um2':0}))
    grouped=reader.data(run_id,category='uncertain',minimum_area=0)
    assert grouped['filtered_count']==1 and grouped['rows'][0]['id']==group['representative_id']
    assert len(grouped['rows'][0]['dedup']['observation_ids'])==2
    assert reader.data(run_id,category='uncertain',minimum_area=0,group_duplicates=False)['filtered_count']==2
    exact=reader.data(run_id,category='all',observation=group['observation_ids'][1])
    assert exact['rows'][0]['id']==group['observation_ids'][1]


def test_color_and_counts_drop_disappeared_previously_indexed_shard(saved):
    reader,run_id,run,output,ledger=saved
    rel=ledger['chip001/tile_000001']['field_json']
    payload=json.loads((output/rel).read_text())
    for row in payload['rows']:row['contrast_rgb']=[.1,.2,.3]
    sha=save(output/rel,payload);ledger['chip001/tile_000001']['hashes'][rel]=sha
    save(output/'ledger.json',ledger)
    assert reader.color_data(run_id)['count']==3
    (output/rel).unlink()
    result=reader.color_data(run_id)
    assert result['count']==0 and result['warnings']
    assert reader.data(run_id)['source_region_count']==0


def test_compact_page_and_persistent_index_avoid_full_shard_reads(saved,monkeypatch):
    reader,run_id,run,output,ledger=saved
    first=reader.data(run_id,category='all',detail=False,include_fields=False)
    assert first['fields'] is None and first['fields_version']
    assert all(not any('polygon' in k or k in ('measurement','sieve_results') for k in row) for row in first['rows'])
    fresh=GrapheneReview(reader.root)
    monkeypatch.setattr(fresh,'_shard',lambda *a: (_ for _ in ()).throw(AssertionError('cached summary must not load geometry')))
    assert fresh.data(run_id,category='all',detail=False)['rows']==first['rows']
    (output/ledger['chip001/tile_000001']['field_json']).unlink()
    assert fresh.data(run_id,category='all',detail=False)['rows']==[]
    exact=reader.data(run_id,observation=first['rows'][0]['id'])
    assert exact['rows']==[]


def test_refinement_stream_generation_summary_lazy_detail_and_size_policy(offline):
    f=offline;reader=f['reader'];field='chip001/tile_000001';rid=field+'/3'
    # An old unclassified region's fake zero-width failure remains in source bytes.
    f['payload']['regions'][2]['filter_reasons']=['color_unclassified','below_useful_size']
    commit=f['ledger']['tile_000001'];source_sha=save(f['output']/commit['field_json'],f['payload'])
    commit['hashes'][commit['field_json']]=source_sha;save(f['output']/'ledger.json',f['ledger'])
    folder=f['output'].parent/'refinement-v2';base=f['selector']['plan_sha256']
    plan_sha=save(folder/'plan.json',{'run_id':f['run_id'],'source_plan_sha256':base})
    full={'rank':.2,'useful_size':True,'refinement_status':'refined','polygons_px':[[[1,1],[2,1],[2,2]]]}
    shard={'schema':2,'run_id':f['run_id'],'plan_sha256':base,'refinement_plan_sha256':plan_sha,
        'field':field,'source_field_sha256':source_sha,'regions':{rid:full}}
    shard_sha=save(folder/'fields/one.json',shard)
    entry={k:v for k,v in full.items() if k!='polygons_px'}
    entry.update(shard_path='fields/one.json',shard_sha256=shard_sha,source_field_sha256=source_sha)
    index={'schema':2,'run_id':f['run_id'],'plan_sha256':base,'refinement_plan_sha256':plan_sha,
        'source_fields':{field:source_sha},'regions':{rid:entry},'progress':{'fields_complete':1,'total_fields':2}}
    sha=save(folder/'index-1.json',index)
    pointer={'index_path':'index-1.json','sha256':sha,'refinement_plan_sha256':plan_sha}
    save(folder/'latest.json',pointer)
    save(f['run']/'graphene-review-selection.json',{'schema':1,'run_id':f['run_id'],'plan_sha256':base,
        'refinement':{'kind':'stream','path':folder.relative_to(reader.root).as_posix(),'plan_sha256':plan_sha}})
    result=reader.data(f['run_id'],category='uncertain',detail=False)
    row=next(r for r in result['rows'] if r['id']==rid)
    assert not reader._refinement_shards
    assert row['original_filter_reasons']==['color_unclassified','below_useful_size']
    assert row['display_filter_reasons']==['color_unclassified']
    assert row['size_assessment']['area_qualified'] and row['size_assessment']['width_is_diagnostic_not_rejection']
    assert result['refinement_progress']['fields_complete']==1
    exact=reader.data(f['run_id'],category='all',observation=rid,detail=True)
    assert exact['rows'][0]['refinement']['polygons_px']==full['polygons_px']
    index['regions'][rid]['likely_artifact']=True
    pointer.update(index_path='index-2.json',sha256=save(folder/'index-2.json',index));save(folder/'latest.json',pointer)
    assert reader.data(f['run_id'],category='uncertain',detail=False)['rows'][0]['id']!=rid
    pointer['sha256']='0'*64;save(folder/'latest.json',pointer)
    with pytest.raises(ValueError,match='checksum'):reader.data(f['run_id'],detail=False)



def test_joint_score_known_thin_bulk_dust_order_and_continuous_unknowns():
    from scipy.stats import chi2
    radius=float(chi2.ppf(.9973,3))
    def score(distance,support,geometry,area=100.,llr=100.):
        row={'area_um2':area,'optical_evidence':{'reference_distance':distance,'match_radius_squared':radius,
            'support_fraction':support,'log_likelihood_ratio':llr,'minimum_log_likelihood_ratio':2.995732273553991}}
        return GrapheneReview._joint_score(row,{'rank':geometry})
    clean=score(3.47,1.,1.324);subtle=score(4.24,1.,.746)
    bulk1=score(23.81,0.,3.023);bulk2=score(16.29,0.,2.235);dust=score(1.69,1.,.00684)
    assert clean['combined_score']>subtle['combined_score']>bulk2['combined_score']>bulk1['combined_score']>dust['combined_score']>0
    assert score(4,0,1)['combined_score']>score(5,0,1)['combined_score']>0
    assert score(4,1,1)['combined_score']>score(4,0,1)['combined_score']
    assert score(4,1,2)['combined_score']>score(4,1,1)['combined_score']
    assert score(4,1,1,llr=100)['combined_score']==score(4,1,1,llr=10000)['combined_score']
    assert score(4,1,1,llr=-10)['combined_score']<score(4,1,1,llr=100)['combined_score']
    # Geometry already contains size; a second area factor must not be applied.
    assert score(4,1,1,area=25)['combined_score']==score(4,1,1,area=1000)['combined_score']
    missing=GrapheneReview._joint_score({'area_um2':100.},{})
    assert missing['boundary_quality_factor']==.5 and missing['combined_score']>0 and len(missing['evidence_missing'])==4


def test_single_joint_ranking_retains_source_scores_and_filter_counts(saved,monkeypatch):
    reader,run_id,run,output,ledger=saved
    field='chip001/tile_000001';rel=ledger[field]['field_json'];payload=json.loads((output/rel).read_text())
    specs=[(0.,23.81,0.,3.023),(25.,3.47,1.,1.324),(40.,4.24,1.,None)]
    for row,(old,distance,support,geometry) in zip(payload['rows'],specs):
        row.update(rank=old,area_um2=100.,disposition='uncertain',sieve_results=[{'name':'material_color_population',
            'reference_distance':distance,'support_fraction':support,'log_likelihood_ratio_vs_substrate':100.}])
    payload['classifier']={'statistics':{'match_quantile':.9973,'minimum_log_likelihood_ratio':2.995732273553991}}
    ledger[field]['hashes'][rel]=save(output/rel,payload);save(output/'ledger.json',ledger)
    bulk,thin,pending=[r['id'] for r in payload['rows']]
    annotation={'schema':2,'_version':'joint-ranking-test','regions':{
        bulk:{'rank':3.023,'likely_artifact':False},thin:{'rank':1.324,'likely_artifact':False}}}
    monkeypatch.setattr(reader,'_review_annotations',lambda *a:(annotation,{}))
    result=reader.data(run_id,category='uncertain',detail=False)
    assert result['ranking']=='joint_evidence_v1'
    assert [r['id'] for r in result['rows']]==[thin,pending,bulk]
    assert result['filtered_count']==3
    assert [r['optical_rank'] for r in result['rows']]==[25.,40.,0.]
    assert [r['boundary_quality_rank'] for r in result['rows']]==[1.324,None,3.023]
    assert result['rows'][1]['review_score_components']['boundary_quality_factor']==.5
    assert result['rows'][0]['optical_evidence']['threshold_provenance']=='saved_field_classifier.statistics'
    assert result['rows']==reader.data(run_id,category='uncertain',detail=False)['rows']
    annotation['regions'][bulk]['likely_artifact']=True;annotation['_version']='joint-ranking-hide'
    assert reader.data(run_id,category='uncertain',detail=False)['filtered_count']==2
    assert reader.data(run_id,category='uncertain',detail=False,hide_artifacts=False)['filtered_count']==3
    assert reader._index[next(iter(reader._index))][0][0]['rank']==0.


def test_pending_geometry_preserves_area_utility_and_page_factors_equal_details(saved,monkeypatch):
    reader,run_id,run,output,ledger=saved
    evidence={'reference_distance':4.,'match_radius_squared':14.,'support_fraction':0.,
        'log_likelihood_ratio':8.,'minimum_log_likelihood_ratio':3.}
    small=GrapheneReview._joint_score({'area_um2':25.,'optical_evidence':evidence},{})
    large=GrapheneReview._joint_score({'area_um2':1000.,'optical_evidence':evidence},{})
    assert 0<small['combined_score']<large['combined_score']
    assert large['boundary_quality_factor']==small['boundary_quality_factor']==.5
    summary=reader.data(run_id,category='uncertain',detail=False)['rows'][0]
    detail=reader.data(run_id,category='all',observation=summary['id'],detail=True)['rows'][0]
    assert detail['optical_evidence']==summary['optical_evidence']
    assert detail['review_score_components']==summary['review_score_components']
    scores=dict(reader._scores)
    reader.data(run_id,category='all',detail=False)
    assert all(reader._scores[k]==v for k,v in scores.items())


def test_unresolved_boundary_is_missing_evidence_not_measured_zero():
    row={'area_um2':100.,'optical_evidence':{'reference_distance':4.,'match_radius_squared':14.,
        'support_fraction':0.,'log_likelihood_ratio':8.,'minimum_log_likelihood_ratio':3.}}
    unresolved=GrapheneReview._joint_score(row,{'rank':0.,'refinement_status':'unresolved',
        'artifact_flags':['insufficient_local_background_or_foreground']})
    assert unresolved['combined_score']>0 and unresolved['boundary_quality_factor']==.5
    assert 'boundary_geometry' in unresolved['evidence_missing']
    assert unresolved['boundary_evidence_flags']==['insufficient_local_background_or_foreground']
    measured=GrapheneReview._joint_score(row,{'rank':.001,'refinement_status':'review_required',
        'artifact_flags':['diffuse_or_weak_boundary']})
    assert 'boundary_geometry' not in measured['evidence_missing']
    assert 0<measured['combined_score']<unresolved['combined_score']


def test_empirical_support_tiers_keep_bulk_unknowns_and_mixed_duplicate_groups(saved,monkeypatch):
    import copy
    reader,run_id,run,output,ledger=saved
    field='chip001/tile_000001';rel=ledger[field]['field_json']
    payload=json.loads((output/rel).read_text());template=payload['rows'][0]
    # All uncertain deliberately: supported small candidates must not lose to
    # giant on-ray extrapolations or entirely unclassified dark observations.
    specs=[('unknown',10000.,0.,'unknown',0.),
           ('extrapolated_rgb_trajectory_unconfirmed',10000.,0.,'unknown',0.),
           ('operator_supported_rgb_trajectory',25.,1.,'compatible',1.),
           ('operator_negative_achromatic_population',10000.,0.,'unknown',0.),
           ('unknown',100.,0.,'unknown',3.)]
    payload['rows']=[]
    for i,(name,area,support,status,distance) in enumerate(specs,1):
        row=copy.deepcopy(template)
        row.update(id=field+'/'+str(i),area_um2=area,rank=area,disposition='uncertain',class_name=name,
            filter_reasons=['color_unclassified'],sieve_results=[{'name':'material_color_population',
            'status':status,'reference_distance':distance,'support_fraction':support,
            'log_likelihood_ratio_vs_substrate':10000.}])
        payload['rows'].append(row)
    payload['classifier']={'statistics':{'match_quantile':.9973,'minimum_log_likelihood_ratio':3.},
        'empirical_trajectory':{'supported_red_interval':[.10,.37],'proposal_red_interval':[0.,.51]}}
    ledger[field]['hashes'][rel]=save(output/rel,payload);save(output/'ledger.json',ledger)
    ids=[r['id'] for r in payload['rows']]
    result=reader.data(run_id,category='uncertain',detail=False,minimum_area=0)
    assert result['ranking']=='empirical_support_then_joint_v1'
    assert [r['id'] for r in result['rows']]==[ids[2],ids[1],ids[0],ids[4],ids[3]]
    assert result['source_region_count']==5 and result['filtered_count']==5
    # Raw numeric joint scores remain diagnostic; the explicit tier is primary.
    assert result['rows'][0]['review_rank']<result['rows'][1]['review_rank']
    assert result['rows'][0]['review_score_components']['empirical_support_scope']=='observed_band_supported'
    assert [r['id'] for r in reader.data(run_id,category='all',detail=False,minimum_area=0)['rows']]==[r['id'] for r in result['rows']]
    # Cold disposable-cache reads retain support provenance in compact rows.
    fresh=GrapheneReview(reader.root)
    assert [r['id'] for r in fresh.data(run_id,category='all',detail=False,minimum_area=0)['rows']]==[r['id'] for r in result['rows']]
    groups={'_version':'mixed-tiers','groups':[{'id':'same-flake','representative_id':ids[0],
        'observation_ids':ids[:3]}]}
    monkeypatch.setattr(reader,'_review_annotations',lambda *a:({},groups))
    grouped=reader.data(run_id,category='all',detail=False,minimum_area=0)
    assert [r['id'] for r in grouped['rows']]==[ids[2],ids[4],ids[3]]
    assert reader.data(run_id,category='all',observation=ids[0],detail=True)['rows'][0]['id']==ids[0]
    assert len(reader.data(run_id,category='all',group_duplicates=False,minimum_area=0)['rows'])==5


def test_empirical_support_requires_saved_compatibility_not_small_perpendicular_distance(tmp_path):
    reader=GrapheneReview(tmp_path)
    classifier={'empirical_trajectory':{'supported_red_interval':[.1,.37]}}
    row={'class_name':'unknown','contrast_rgb':[.8,.616,.384],
        'sieve_results':[{'name':'material_color_population','status':'unknown','reference_distance':0.}]}
    evidence=reader._optical_evidence(row,classifier)
    assert evidence['empirical_support_tier']==2
    assert evidence['empirical_support_scope']=='unclassified_optical_feature'
    assert 'empirical_support_tier' not in reader._optical_evidence(row,{})


def test_analysis_memory_lru_selector_transitions_and_exact_revisit(offline,monkeypatch):
    import copy
    import shutil
    f=offline;reader=f['reader'];first=f['output']
    outputs=[first,first.parent/'second-analysis',first.parent/'third-analysis']
    for output in outputs[1:]:shutil.copytree(first,output)
    def select(output):
        selector=copy.deepcopy(f['selector'])
        selector['analysis_relative_path']=output.relative_to(reader.root).as_posix()
        save(f['run']/'graphene-analysis-selection.json',selector)
        return reader.data(f['run_id'],category='all',detail=True,minimum_area=0)
    original=select(first)
    reader.data(f['run_id'],category='all',detail=False,minimum_area=0)
    original_color=reader.color_data(f['run_id'])
    first_keys=set(reader._active_index[str(first)])
    select(outputs[1])
    assert list(reader._analysis_lru)==list(map(str,outputs[:2]))
    assert first_keys<=set(reader._index)
    select(outputs[2])
    assert list(reader._analysis_lru)==list(map(str,outputs[1:]))
    assert not first_keys.intersection(reader._index)
    assert str(first) not in reader._active_index and str(first) not in reader._manifest_fields
    assert all(k[0]!=str(first) for k in reader._field_index_keys)
    assert all(k[0]!=str(first) for k in reader._paths)
    assert all(k[0][0]!=str(first) for cache in (reader._full_rows,reader._display_rows) for k in cache)
    assert ('pinned',str(first/'plan.json')) not in reader._snapshots
    assert str(first/'ledger.json') not in reader._snapshots
    assert reader._disk_cache().execute('SELECT COUNT(*) FROM fields WHERE namespace LIKE ?',
        (str(first)+'/%',)).fetchone()[0]>0
    # Rehydrate the evicted namespace from validated, unchanged scientific files
    # and the disposable disk cache; exact observations/order are recovered.
    revisited=select(first)
    assert revisited['rows']==original['rows']
    assert revisited['counts']==original['counts']
    assert reader.color_data(f['run_id'])==original_color
    assert list(reader._analysis_lru)==[str(outputs[2]),str(first)]
    # Touching a retained output refreshes recency; it does not duplicate it.
    select(outputs[2]);select(outputs[1])
    assert list(reader._analysis_lru)==[str(outputs[2]),str(outputs[1])]
    real_data=reader.data
    def check_locked(*args,**kwargs):
        assert reader._lock._is_owned()
        return real_data(*args,**kwargs)
    monkeypatch.setattr(reader,'data',check_locked)
    reader.color_data(f['run_id'])


def test_explicit_chip_selection_isolated_from_default_and_other_derivatives(offline):
    import copy
    f=offline;reader=f['reader'];root=reader.root;second=f['output'].parent.parent/'chip002'/'second-version'
    save(f['run']/'chip002/manifest.json',f['manifest'])
    plan=copy.deepcopy(f['plan']);plan['pin']['chip']='chip002';sha=save(second/'plan.json',plan)
    save(second/'model.json',json.loads((f['output']/'model.json').read_text()))
    ledger=copy.deepcopy(f['ledger'])
    for tile,commit in ledger.items():
        payload=json.loads((f['output']/commit['field_json']).read_text());payload['field']=payload['field'].replace('chip001','chip002')
        commit['hashes'][commit['field_json']]=save(second/commit['field_json'],payload)
    save(second/'ledger.json',ledger);save(second/'status.json',{'state':'complete','finished_fields':2})
    save(f['run']/'graphene-analysis-selection-chip002.json',{'schema':1,'run_id':f['run_id'],
        'analysis_relative_path':second.relative_to(root).as_posix(),'plan_sha256':sha})
    first=reader.data(f['run_id'],category='all',detail=False)
    selected=reader.data(f['run_id'],chip='chip002',category='all',detail=False)
    assert first['analysis_source']['chip']=='chip001' and selected['analysis_source']['chip']=='chip002'
    assert all(row['id'].startswith('chip002/') for row in selected['rows'])
    assert all('/chip002/' in row['preview_url'] for row in selected['rows'])
    exact=reader.data(f['run_id'],field='chip002/tile_000001',category='all',detail=False)
    assert exact['rows']==selected['rows']
    assert reader.color_data(f['run_id'],chip='chip002')['run_id']==f['run_id']
    with pytest.raises(ValueError,match='requested chip'):reader.data(f['run_id'],chip='chip003')


def test_gallery_memory_index_tracks_area_filter_without_losing_small_observations(saved):
    reader,run_id,run,output,ledger=saved
    result=reader.data(run_id,category='all',minimum_area=25,detail=False)
    assert result['source_region_count']==3 and result['filtered_count']==2
    assert sum(len(rows) for rows,summary in reader._index.values())==2
    lower=reader.data(run_id,category='all',minimum_area=0,detail=False)
    assert lower['filtered_count']==3
    higher=reader.data(run_id,category='all',minimum_area=75,detail=False)
    assert higher['filtered_count']==1 and higher['source_region_count']==3
    assert sum(len(rows) for rows,summary in reader._index.values())==1
    tiny=reader.data(run_id,category='all',minimum_area=75,observation='chip001/tile_000001/3',detail=False)
    assert tiny['rows'][0]['area_um2']==8


def test_area_index_reuses_legacy_disk_projection_without_reading_source(saved,monkeypatch):
    reader,run_id,run,output,ledger=saved
    reader.data(run_id,category='all',minimum_area=0,detail=False)
    db=reader._disk_cache()
    for index_key in reader._active_index[str(output)]:
        current=reader._canonical(index_key);legacy=reader._canonical(index_key[:-1])
        with db:
            db.execute('UPDATE fields SET key=? WHERE key=?',(legacy,current))
            db.execute('UPDATE displays SET key=? WHERE key=?',(legacy,current))
    reader._index.clear();reader._shards.clear();reader._display_rows.clear()
    def unavailable(*args,**kwargs):raise AssertionError('Unnecessary source reread')
    monkeypatch.setattr(reader,'_shard',unavailable)
    result=reader.data(run_id,category='all',minimum_area=25,detail=False)
    assert result['source_region_count']==3 and result['filtered_count']==2
    assert len(result['rows'])==2


def test_dense_field_over_32mb_is_visible_with_bounded_shard_retention(saved):
    reader,run_id,run,output,ledger=saved
    reader.data(run_id,category='all')
    relative=ledger['chip001/tile_000001']['field_json']
    payload=json.loads((output/relative).read_text())
    payload['diagnostic_padding']='x'*(33*1024**2)
    ledger['chip001/tile_000001']['hashes'][relative]=save(output/relative,payload)
    save(output/'ledger.json',ledger)
    result=reader.data(run_id,category='all',detail=False)
    assert result['filtered_count']==2 and not result['warnings']
    assert len(reader._shards)==1


def test_progressive_gallery_returns_partial_then_complete_and_prioritizes_selected_field(offline,monkeypatch):
    f=offline;reader=f['reader']
    monkeypatch.setattr(reader,'INDEX_SLICE_SECONDS',0.)
    first=reader.data(f['run_id'],category='all',detail=False,progressive=True)
    assert first['rows']
    assert first['review_index']=={'state':'loading','fields_indexed':1,'fields_waiting':1,'progressive':True}
    assert first['status']['processed_fields']==2
    assert any(x['state']=='index_pending' for x in first['fields'])
    second=reader.data(f['run_id'],category='all',detail=False,progressive=True)
    assert second['review_index']['state']=='ready'
    complete=reader.data(f['run_id'],category='all',detail=False)
    assert second['rows']==complete['rows'] and second['counts']==complete['counts']
    assert not second['warnings']
    # A direct field must not wait behind unrelated cold fields.
    reader._index.clear()
    target='chip001/tile_000002'
    selected=reader.data(f['run_id'],category='all',field=target,detail=False,progressive=True)
    assert next(x for x in selected['fields'] if x['field']==target)['state']!='index_pending'
    assert selected['review_index']['fields_waiting']==1
