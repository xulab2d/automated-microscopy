"""hBN uses its own pinned model/selection, never historical graphene data."""
import json
import pytest
from test_graphene_review import offline,save
from flakefinder.graphene_review import GrapheneReview
from flakefinder.review_library import ReviewLibrary


@pytest.fixture
def hbn(offline):
    f=offline;root=f['run'].parent.parent
    new=root/'analysis/hbn'/f['run_id']/'chip001/conditional-v1'
    new.parent.mkdir(parents=True);f['output'].rename(new);f['output']=new
    request={'id':f['run_id'],'settings':{'material':'hBN','oxide_nm':90,'sample_id':'Loose hBN'}}
    f['plan']['pin']['identity']['scan_request_sha256']=save(f['run']/'scan-request.json',request)
    model={'material':'hBN','oxide_nm':90};save(new/'model.json',model)
    f['plan']['model_sha256']=GrapheneReview._canonical(model)
    f['selector'].update(analysis_relative_path=new.relative_to(root).as_posix(),plan_sha256=save(new/'plan.json',f['plan']))
    save(f['run']/'hbn-analysis-selection.json',f['selector'])
    f['payload']['classifier']['model_sha256']=f['plan']['model_sha256']
    for row in f['payload']['regions']:row['contrast_rgb']=[.03,.04,.02]
    commit=f['ledger']['tile_000001'];commit['hashes'][commit['field_json']]=save(new/commit['field_json'],f['payload'])
    save(new/'ledger.json',f['ledger'])
    return f


def test_hbn_candidates_uncertain_colors_library_and_derivative_pins(hbn):
    f=hbn;reader=f['reader'];value=reader.data(f['run_id'],category='all',minimum_area=0,target_band='other')
    assert value['material']==value['material_label']=='hBN'
    assert value['counts']['shortlist']==0 and value['counts']['uncertain']==3
    assert any(r['original_disposition']=='shortlist' for r in value['rows'])
    assert all('thin_film_evidence_pending' in r['display_filter_reasons'] for r in value['rows'])
    assert value['analysis_source']['namespace']=='hbn'
    assert all(r['layer_number'] is None and not r['chemical_identity_confirmed'] for r in value['rows'])
    colors=reader.color_data(f['run_id'])
    assert colors['count']==3 and colors['material']=='hBN' and not colors['empirical_trajectory'] and colors['proposals']==[]
    assert 'graphene' not in colors['qualification'].lower()
    field='chip001/tile_000001';digest=f['ledger']['tile_000001']['hashes']['fields/one/field.json']
    derivative={'schema':1,'run_id':f['run_id'],'plan_sha256':f['selector']['plan_sha256'],
        'source_fields':{field:digest},'minimum_area_um2':25,
        'groups':[{'id':'pair','representative_id':field+'/1','observation_ids':[field+'/1',field+'/3']}]}
    path=f['output'].parent/'review-v1/dedup.json';sha=save(path,derivative)
    save(f['run']/'hbn-review-selection.json',{'schema':1,'run_id':f['run_id'],
        'plan_sha256':f['selector']['plan_sha256'],'dedup':{'path':path.relative_to(reader.root).as_posix(),'sha256':sha}})
    grouped=reader.data(f['run_id'],category='all',minimum_area=0,target_band='other')
    assert grouped['review_processing']['dedup_available'] and grouped['filtered_count']==2
    library=ReviewLibrary(reader.root).data()
    assert not library['warnings'] and len(library['runs'])==1
    row=library['runs'][0]
    assert row['material']=='hBN' and row['kind']=='hbn' and row['refinement_selected']
    assert row['href']=='/graphene?run='+f['run_id']


@pytest.mark.parametrize('change',['material','oxide','empirical'])
def test_hbn_rejects_incompatible_even_rehashed_model(hbn,change):
    f=hbn;model={'material':'hBN','oxide_nm':90}
    if change=='material':model['material']='graphene'
    if change=='oxide':model['oxide_nm']=285
    if change=='empirical':model['empirical_trajectory']={'direction_rgb':[1,.77,.48]}
    save(f['output']/'model.json',model);f['plan']['model_sha256']=GrapheneReview._canonical(model)
    f['selector']['plan_sha256']=save(f['output']/'plan.json',f['plan'])
    save(f['run']/'hbn-analysis-selection.json',f['selector'])
    with pytest.raises(ValueError,match='context|trajectory'):f['reader'].data(f['run_id'])


def test_hbn_never_falls_back_to_graphene_selector(hbn):
    f=hbn;(f['run']/'hbn-analysis-selection.json').unlink()
    value=f['reader'].data(f['run_id'])
    assert not value['rows'] and value['status']['state']=='not_started'
    assert value['analysis_source']['namespace']=='hbn'


def test_hbn_screen_updates_gallery_plot_and_cached_views_without_source_changes(hbn,monkeypatch):
    f=hbn;reader=f['reader'];field='chip001/tile_000001';identifier=field+'/1'
    initial=reader.data(f['run_id'],category='all',detail=False,target_band='other')
    assert initial['counts']['shortlist']==0 and initial['counts']['uncertain']==3
    assert reader.color_data(f['run_id'])['proposals']==[]
    original=(f['output']/'fields/one/field.json').read_bytes()
    derivative=f['output'].parent/'screen-v2/index.json'
    def entry(band,ambiguous=False,evidence=1):
        return {'rank':1.,'likely_artifact':True,'hbn_screen':{
            'policy_version':'hbn-thin-film-evidence-v2','target_band':band,'thin_supported':True,
            'contamination_ambiguous':ambiguous,'operator_supported':False,'thickness_interval_nm':[2,12],
            'reasons':[],'metrics':{'target_log_evidence':evidence},'qualification':'Conditional optical evidence.'}}
    value={'schema':2,'run_id':f['run_id'],'plan_sha256':f['selector']['plan_sha256'],
        'source_fields':{field:f['ledger']['tile_000001']['hashes']['fields/one/field.json']},
        'regions':{identifier:entry('below_5_nm',evidence=3),field+'/2':entry('spans_target_bands',True),
                   field+'/3':entry('5_to_30_nm',evidence=5)}}
    def select():
        sha=save(derivative,value)
        save(f['run']/'hbn-review-selection.json',{'schema':1,'run_id':f['run_id'],
            'plan_sha256':f['selector']['plan_sha256'],
            'refinement':{'path':derivative.relative_to(reader.root).as_posix(),'sha256':sha}})
    select()
    monkeypatch.setattr(reader,'_shard',lambda *a,**k:pytest.fail('Screen update reread full field geometry'))
    passed=reader.data(f['run_id'],detail=False)
    assert passed['counts']['shortlist']==2 and passed['counts']['uncertain']==1
    assert passed['rows'][0]['id']==field+'/3' # Higher saved optical evidence precedes geometry.
    assert passed['rows'][0]['original_disposition']=='uncertain' # No legacy optical veto.
    assert passed['review_processing']['hidden_artifacts']==0 # Old morphology flags are diagnostic.
    assert {r['id'] for r in reader.color_data(f['run_id'])['proposals']}=={identifier,field+'/3'}
    for band,expected in [('below_5_nm',identifier),('5_to_30_nm',field+'/3')]:
        result=reader.data(f['run_id'],detail=False,target_band=band)
        assert result['counts']['shortlist']==1 and result['counts']['uncertain']==1
        assert result['rows'][0]['id']==expected
        assert [r['id'] for r in reader.color_data(f['run_id'],target_band=band)['proposals']]==[expected]
    value['regions'][identifier]['hbn_screen'].update(thin_supported=False,target_band='thick_or_bulk',reasons=['thicker_film_evidence'])
    value['regions'][field+'/2']['hbn_screen']['operator_supported']=True
    select()
    result=reader.data(f['run_id'],detail=False)
    assert result['counts']['shortlist']==2 and result['counts']['uncertain']==0
    positive=next(r for r in result['rows'] if r['id']==field+'/2')
    assert 'operator_visual_example_not_measured_thickness' in positive['display_filter_reasons']
    bulk=reader.data(f['run_id'],category='uncertain',detail=False,target_band='other')
    assert bulk['filtered_count']==1 and bulk['rows'][0]['id']==identifier
    assert bulk['rows'][0]['original_disposition']=='shortlist'
    assert reader.color_data(f['run_id'],target_band='other')['proposals']==[]
    # Direct observation remains accessible outside the chosen default band.
    assert reader.data(f['run_id'],category='all',observation=identifier,detail=False)['rows'][0]['id']==identifier
    value['regions'][field+'/3']={'hbn_screen':{'policy_version':'hbn-observed-boundary-interior-v1','passes':True}}
    select()
    unknown=reader.data(f['run_id'],category='uncertain',detail=False,target_band='other')
    assert unknown['filtered_count']==2
    # Operator retrieval does not bend a mismatched optical thickness interval.
    value['regions'][field+'/3']={'hbn_screen':{'policy_version':'hbn-thin-film-evidence-v2',
        'target_band':'unknown','thin_supported':False,'contamination_ambiguous':True,'operator_supported':True,
        'thickness_interval_nm':None,'metrics':{},'reasons':['optical_model_mismatch']}}
    select()
    assert field+'/3' in [r['id'] for r in reader.data(f['run_id'],detail=False)['rows']]
    other=reader.data(f['run_id'],detail=False,target_band='other')
    assert [r['id'] for r in other['rows']]==[field+'/3']
    for band in ('below_5_nm','5_to_30_nm'):
        assert field+'/3' not in [r['id'] for r in reader.data(f['run_id'],detail=False,target_band=band)['rows']]
    assert field+'/3' in [r['id'] for r in reader.color_data(f['run_id'])['proposals']]
    assert value['regions'][field+'/3']['hbn_screen']['target_band']=='unknown'
    assert (f['output']/'fields/one/field.json').read_bytes()==original
    with pytest.raises(ValueError,match='thickness'):reader.data(f['run_id'],target_band='made_up')


def test_hbn_halo_filter_is_reversible_and_preserves_operator_examples(hbn):
    f=hbn;reader=f['reader'];field='chip001/tile_000001'
    def screen(operator=False,halo=True):
        return {'hbn_screen':{'policy_version':'hbn-thin-film-evidence-v2',
            'target_band':'below_5_nm','thin_supported':True,'contamination_ambiguous':False,
            'operator_supported':operator,'halo_screen':{'likely_halo':halo},'reasons':[], 'metrics':{}},
            'likely_artifact':True}
    value={'schema':2,'run_id':f['run_id'],'plan_sha256':f['selector']['plan_sha256'],
        'source_fields':{field:f['ledger']['tile_000001']['hashes']['fields/one/field.json']},
        'regions':{field+'/1':screen(),field+'/2':screen(operator=True),field+'/3':screen(halo=False)}}
    path=f['output'].parent/'halo-review/index.json';sha=save(path,value)
    save(f['run']/'hbn-review-selection.json',{'schema':1,'run_id':f['run_id'],
        'plan_sha256':f['selector']['plan_sha256'],
        'refinement':{'path':path.relative_to(reader.root).as_posix(),'sha256':sha}})
    default=reader.data(f['run_id'],category='all',detail=False,minimum_area=0)
    assert {r['id'] for r in default['rows']}=={field+'/2',field+'/3'}
    assert default['review_processing']['hidden_artifacts']==1
    shown=reader.data(f['run_id'],category='all',detail=False,minimum_area=0,hide_artifacts=False)
    assert len(shown['rows'])==3
    assert next(r for r in shown['rows'] if r['id']==field+'/1')['disposition']=='uncertain'
    assert shown['counts']['shortlist']==2
    assert reader.data(f['run_id'],category='all',detail=False,minimum_area=0,observation=field+'/1')['rows'][0]['id']==field+'/1'
    colors=reader.color_data(f['run_id'])
    assert colors['count']==3
    assert {r['id'] for r in colors['proposals']}=={field+'/2',field+'/3'}


def test_live_hbn_commits_are_visible_before_review_and_review_replaces_pending(hbn):
    f=hbn;reader=f['reader'];output=f['output'];field='chip001/tile_000001'
    original=(output/'fields/one/field.json').read_bytes()
    save(output/'status.json',{'state':'running','finished_fields':0,'selected_fields':2})
    save(output/'ledger.json',{})
    initial=reader.data(f['run_id'],category='all',target_band='all',minimum_area=0,detail=False)
    assert initial['rows']==[] and initial['status']['processed_fields']==0
    save(output/'ledger.json',{'tile_000001':f['ledger']['tile_000001']})
    save(output/'status.json',{'state':'running','finished_fields':1,'selected_fields':2})
    live=reader.data(f['run_id'],category='all',target_band='all',minimum_area=0,detail=False)
    assert live['status']['processed_fields']==1 and live['status']['total_fields']==2
    assert len(live['rows'])==3 and all(r['review_pending'] for r in live['rows'])
    assert live['counts']['shortlist']==0 and live['counts']['uncertain']==3
    assert live['review_processing']['pending_observations']==3
    assert live['fields_version']!=initial['fields_version']
    assert reader.color_data(f['run_id'],target_band='all')['count']==3
    path=output.parent/'live-reviewed/index.json'
    evidence={'schema':2,'run_id':f['run_id'],'plan_sha256':f['selector']['plan_sha256'],
        'source_fields':{field:f['ledger']['tile_000001']['hashes']['fields/one/field.json']},
        'regions':{field+'/1':{'hbn_screen':{'policy_version':'hbn-thin-film-evidence-v2',
            'target_band':'below_5_nm','thin_supported':True,'contamination_ambiguous':False,'metrics':{},'reasons':[]}}}}
    sha=save(path,evidence)
    save(f['run']/'hbn-review-selection.json',{'schema':1,'run_id':f['run_id'],
        'plan_sha256':f['selector']['plan_sha256'],
        'refinement':{'path':path.relative_to(reader.root).as_posix(),'sha256':sha}})
    updated=reader.data(f['run_id'],category='all',target_band='all',minimum_area=0,detail=False)
    assert updated['review_processing']['pending_observations']==2
    assert next(r for r in updated['rows'] if r['id']==field+'/1')['review_pending'] is False
    assert updated['counts']['shortlist']==1 and updated['fields_version']!=live['fields_version']
    assert (output/'fields/one/field.json').read_bytes()==original


def test_human_feedback_promotes_excludes_undoes_and_is_bound_to_source(hbn):
    from flakefinder.review_feedback import FeedbackConflict
    f=hbn;reader=f['reader'];run=f['run_id']
    original=(f['output']/'fields/one/field.json').read_bytes()
    row=reader.data(run,category='all',target_band='all',detail=False)['rows'][0]
    args=dict(actor='Operator test',expected_revision=0,client_event_id='click-1')
    saved=reader.save_feedback(run,row['id'],row['feedback_identity'],label='thin_hbn',**args)
    assert saved['revision']==1
    candidate=reader.data(run,category='shortlist',target_band='both',detail=False)
    assert candidate['counts']['shortlist']==1
    assert candidate['rows'][0]['human_feedback']['label']=='thin_hbn'
    assert candidate['rows'][0]['feedback_revision']==1
    # A visual thin label does not fabricate a calibrated sub-5nm assignment.
    assert not reader.data(run,category='shortlist',target_band='below_5_nm',detail=False)['rows']
    reloaded=type(reader)(reader.root).data(run,category='shortlist',target_band='both',detail=False)
    assert reloaded['rows'][0]['human_feedback']['label']=='thin_hbn'
    with pytest.raises(FeedbackConflict):
        reader.save_feedback(run,row['id'],row['feedback_identity'],label='bulk',actor='Operator test',expected_revision=0,client_event_id='stale-click')
    reader.save_feedback(run,row['id'],row['feedback_identity'],label='bulk',actor='Operator test',expected_revision=1,client_event_id='click-2')
    assert reader.data(run,category='shortlist',target_band='all',detail=False)['counts']['shortlist']==0
    excluded=reader.data(run,category='rejected',target_band='all',detail=False)
    assert [r['id'] for r in excluded['rows']]==[row['id']]
    reader.save_feedback(run,row['id'],row['feedback_identity'],label=None,actor='Operator test',expected_revision=2,client_event_id='undo-1')
    cleared=reader.data(run,category='all',target_band='all',observation=row['id'],detail=False)['rows'][0]
    assert cleared['human_feedback'] is None and cleared['feedback_revision']==3
    assert (f['output']/'fields/one/field.json').read_bytes()==original
    reader.save_feedback(run,row['id'],row['feedback_identity'],label='encapsulation_hbn',actor='Operator test',expected_revision=3,client_event_id='click-3')
    payload=json.loads(original);payload['review_test_changed_source']=True
    field_path='fields/one/field.json'
    f['ledger']['tile_000001']['hashes'][field_path]=save(f['output']/field_path,payload)
    save(f['output']/'ledger.json',f['ledger'])
    new_source=reader.data(run,category='all',target_band='all',observation=row['id'],detail=False)['rows'][0]
    assert new_source['human_feedback'] is None and new_source['feedback_revision']==0
    assert new_source['feedback_identity']!=row['feedback_identity']
    changed={**row['feedback_identity'],'field_sha256':'e'*64}
    with pytest.raises(FeedbackConflict):
        reader.save_feedback(run,row['id'],changed,label='thin_hbn',actor='Operator test',expected_revision=3,client_event_id='bad-source')


def test_human_feedback_http_roundtrip_and_token_guard(hbn):
    from flakefinder.suite_server import serve_suite
    from urllib.request import Request,urlopen
    from urllib.error import HTTPError
    f=hbn
    save(f['reader'].root/'.flakefinder-project.json',{'project_id':'test-human-review'})
    server,url=serve_suite(f['reader'].root,0)
    try:
        endpoint=url+'api/graphene?run='+f['run_id']+'&filter=all&target_band=all&detail=0'
        row=json.load(urlopen(endpoint))['rows'][0]
        body={'run_id':f['run_id'],'observation':row['id'],'identity':row['feedback_identity'],
            'label':'encapsulation_hbn','actor':'Test operator','expected_revision':0,'client_event_id':'http-1'}
        def submit(token,origin=True):
            headers={'Content-Type':'application/json','X-Review-Token':token}
            if origin:headers['Origin']=url.rstrip('/')
            return json.load(urlopen(Request(url+'api/graphene/feedback',data=json.dumps(body).encode(),headers=headers)))
        with pytest.raises(HTTPError) as error:submit('wrong')
        assert error.value.code==403
        token=json.load(urlopen(url+'api/session'))['token']
        with pytest.raises(HTTPError):submit(token,False)
        result=submit(token)
        assert result['annotation']['label']=='encapsulation_hbn'
        assert submit(token)['idempotent'] is True
        candidates=json.load(urlopen(url+'api/graphene?run='+f['run_id']+'&filter=shortlist&target_band=both&detail=0'))
        assert candidates['rows'][0]['human_feedback']['label']=='encapsulation_hbn'
        body['client_event_id']='http-stale'
        with pytest.raises(HTTPError) as error:submit(token)
        assert error.value.code==409
    finally:server.shutdown();server.server_close()
