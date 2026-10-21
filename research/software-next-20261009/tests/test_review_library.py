import hashlib
import json
from pathlib import Path

from flakefinder.review_library import ReviewLibrary


RUN='scan-20261013-221727-131d9ced'


def write(path,value):
    path.parent.mkdir(parents=True,exist_ok=True)
    path.write_text(json.dumps(value),encoding='utf-8')


def selected(root):
    run=root/'data'/RUN
    write(run/'scan-request.json',{'id':RUN,'settings':{'material':'graphene','oxide_nm':90,'sample_id':'Grid'}})
    output=root/'analysis/graphene'/RUN/'chip001/full-chip'
    write(output/'plan.json',{'pin':{'identity':{'run_id':RUN}}})
    selector={'analysis_relative_path':output.relative_to(root).as_posix(),
              'plan_sha256':hashlib.sha256((output/'plan.json').read_bytes()).hexdigest()}
    write(run/'graphene-analysis-selection.json',selector)
    write(output/'status.json',{'state':'complete','finished_fields':999,'counts':{'assessed':942}})
    return run,selector


def test_curated_graphene_and_historical_collection(tmp_path):
    selected(tmp_path)
    write(tmp_path/'data/whole-chip-standardized-01/analysis-residue-sieves-campaign-01/status.json',
          {'state':'complete_with_exclusions','processed_fields':1266,'successful_fields':1221})
    result=ReviewLibrary(tmp_path).data()
    assert not result['warnings']
    assert len(result['runs'])==2
    graphene,legacy=result['runs']
    assert graphene['curated'] and graphene['fields']==999 and graphene['assessed_fields']==942
    assert graphene['href']=='/graphene?run='+RUN
    assert legacy['href']=='/library' and legacy['fields']==1266


def test_empty_restart_excluded_and_incremental_optional(tmp_path):
    run=tmp_path/'data'/RUN
    write(run/'scan-request.json',{'id':RUN,'settings':{'material':'graphene'}})
    assert ReviewLibrary(tmp_path).data()['runs']==[]
    write(run/'analysis-graphene-incremental/status.json',{'processed_fields':3,'state':'paused'})
    result=ReviewLibrary(tmp_path).data()['runs']
    assert len(result)==1 and not result[0]['curated']


def test_invalid_selection_cannot_escape_or_impersonate(tmp_path):
    run,selector=selected(tmp_path)
    selector['analysis_relative_path']='../other'
    write(run/'graphene-analysis-selection.json',selector)
    result=ReviewLibrary(tmp_path).data()
    assert not result['runs'] and result['warnings']
    run,selector=selected(tmp_path)
    selector['plan_sha256']='0'*64
    write(run/'graphene-analysis-selection.json',selector)
    result=ReviewLibrary(tmp_path).data()
    assert not result['runs'] and result['warnings']


def test_library_lists_two_chips_and_pending_workflow(tmp_path):
    run,selector=selected(tmp_path)
    second=tmp_path/'analysis/graphene'/RUN/'chip002/second'
    write(second/'plan.json',{'pin':{'identity':{'run_id':RUN},'chip':'chip002'}})
    write(second/'status.json',{'state':'complete','finished_fields':5,'counts':{'assessed':4}})
    write(run/'graphene-analysis-selection-chip001.json',selector)
    write(run/'graphene-analysis-selection-chip002.json',{'analysis_relative_path':second.relative_to(tmp_path).as_posix(),
        'plan_sha256':hashlib.sha256((second/'plan.json').read_bytes()).hexdigest()})
    write(run/'offline-workflow/status.json',{'state':'running','phase':'refinement'})
    result=ReviewLibrary(tmp_path).data()
    assert len(result['runs'])==3 and not result['warnings']
    pending,*chips=result['runs']
    assert pending['kind']=='workflow' and pending['href']=='/processing?run='+RUN
    assert {row['chip_id'] for row in chips}=={'chip001','chip002'}
    assert chips[1]['href'].endswith('&chip=chip002')


def test_running_hbn_opens_all_preliminary_regions_completed_keeps_defaults(tmp_path):
    run=tmp_path/'data'/RUN
    write(run/'scan-request.json',{'id':RUN,'settings':{'material':'hBN','oxide_nm':90}})
    output=tmp_path/'analysis/hbn'/RUN/'chip001/full-chip'
    write(output/'plan.json',{'pin':{'identity':{'run_id':RUN},'chip':'chip001'}})
    write(run/'hbn-analysis-selection-chip001.json',{
        'analysis_relative_path':output.relative_to(tmp_path).as_posix(),
        'plan_sha256':hashlib.sha256((output/'plan.json').read_bytes()).hexdigest()})
    for state in ('running','waiting_for_ram','paused_for_quiet_window'):
        write(output/'status.json',{'state':state,'finished_fields':230,'counts':{'assessed':230}})
        row=ReviewLibrary(tmp_path).data()['runs'][0]
        assert row['href']=='/graphene?run='+RUN+'&chip=chip001&filter=all&target_band=all'
        assert row['fields']==230 and row['state']==state
    write(output/'status.json',{'state':'complete','finished_fields':1780})
    assert ReviewLibrary(tmp_path).data()['runs'][0]['href']=='/graphene?run='+RUN+'&chip=chip001'


def test_running_graphene_preserves_default_candidate_link(tmp_path):
    selected(tmp_path)
    write(tmp_path/'analysis/graphene'/RUN/'chip001/full-chip/status.json',{'state':'running','finished_fields':23})
    assert ReviewLibrary(tmp_path).data()['runs'][0]['href']=='/graphene?run='+RUN
