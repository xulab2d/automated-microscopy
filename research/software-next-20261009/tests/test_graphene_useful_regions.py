import copy
import numpy as np
from flakefinder.graphene_useful_regions import prepare_field,refine_candidate,review_assessment
from test_graphene_refinement import fixture


def test_field_cached_refinement_preserves_measured_hole_and_large_unknown_size():
    _,truth,image,valid,bg=fixture();prepared=prepare_field(image,valid,bg,[.2,.2])
    result=refine_candidate(image,truth,valid,[.2,.2],substrate_mask=bg,prepared=prepared)
    region={'area_um2':150.,'selected_for_review':False,'class_index':-1,'filter_reasons':['below_useful_size','color_unclassified_or_not_coherent_material_population']}
    assessment=review_assessment(region,result,[.2,.2])
    assert assessment['useful_size'] and 'below_useful_size' not in assessment['filter_reasons']
    assert 'below_useful_size' in assessment['original_filter_reasons']
    assert not result['refined_mask'][99,82] and not result['refined_mask'][65,120]
    assert assessment['size_assessment']['material_color_independent']
    assert result['metrics']['interior_pixels']>=16
    assert np.isfinite(result['metrics']['interior_relative_variation'])
    assert result['metrics']['interior_erosion_um']==.6


def test_ranking_is_independent_of_material_class_and_penalizes_huge_diffuse_shadow():
    _,truth,image,valid,bg=fixture();result=refine_candidate(image,truth,valid,[.2,.2],substrate_mask=bg)
    unknown={'area_um2':100.,'selected_for_review':False,'class_index':-1,'filter_reasons':['below_useful_size']}
    classified={**unknown,'selected_for_review':True,'class_index':2}
    good=review_assessment(unknown,result,[.2,.2]);same=review_assessment(classified,result,[.2,.2])
    assert good['rank']==same['rank']
    shadow=copy.deepcopy(result);shadow['status']='review_required';shadow['metrics'].update(
        boundary_support_fraction=.2,median_boundary_transition=.1,nearby_dark_feature_fraction=.1)
    bad=review_assessment({**unknown,'area_um2':5000.},shadow,[.2,.2])
    assert bad['likely_artifact'] and bad['rank']<good['rank']/20
    assert bad['useful_size'] # geometry is retained despite artifact warning


def test_dark_clean_thick_feature_is_not_a_shadow_ranking_penalty():
    _,truth,image,valid,bg=fixture();image[truth]*=.7
    result=refine_candidate(image,truth,valid,[.2,.2],substrate_mask=bg)
    row=review_assessment({'area_um2':150.,'filter_reasons':[]},result,[.2,.2])
    assert not row['likely_artifact'] and row['rank']>1


def test_camera_cut_does_not_veto_visible_measured_boundary_or_count_missing_exterior():
    _,truth,image,valid,bg=fixture()
    # Crop through a true flake; its remaining sharp edges are still observed.
    section=np.s_[70:150,20:160]
    result=refine_candidate(image[section],truth[section],valid[section],[.2,.2],substrate_mask=bg[section])
    assert 'candidate_cut_by_crop_or_camera' in result['flags']
    assert result['status']=='edge_supported_proposal' and result['metrics']['boundary_support_fraction']>.8
    assert result['metrics']['camera_boundary_excluded'] and result['metrics']['extent_incomplete']


def test_missing_source_field_is_recorded_and_later_fields_complete(tmp_path,monkeypatch):
    import importlib.util,json
    from pathlib import Path
    from types import SimpleNamespace
    script=Path(__file__).resolve().parents[3]/'research/graphene-segmentation-20261014/refine_useful_regions.py'
    spec=importlib.util.spec_from_file_location('useful_runner_test',script);runner=importlib.util.module_from_spec(spec);spec.loader.exec_module(runner)
    monkeypatch.setattr(runner.driver,'cpu_policy',lambda:None)
    monkeypatch.setattr(runner.driver.time,'sleep',lambda _:None)
    project=tmp_path/'project';rid='scan-20261013-221727-131d9ced';base=project/'analysis/graphene'/rid/'chip001/base'
    base.mkdir(parents=True);chip=project/'data'/rid/'chip001';chip.mkdir(parents=True)
    (chip/'manifest.json').write_text('{}')
    runner.driver.watcher.save(base/'plan.json',{'pin':{'manifest_sha256':runner.driver.digest(chip/'manifest.json')}})
    runner.driver.watcher.save(base/'later.json',{'regions':[]})
    runner.driver.watcher.save(base/'ledger.json',{
        'tile_000524':{'state':'assessed','field_json':'missing.json','hashes':{'missing.json':'a'*64}},
        'tile_000525':{'state':'assessed','field_json':'later.json','hashes':{'later.json':runner.driver.digest(base/'later.json')}}})
    result=runner.run(SimpleNamespace(analysis=base,version='refinement-test',tiles='all',priority_tiles=''))
    assert result['state']=='complete' and result['fields_complete']==2 and result['unavailable_fields']==1
    out=base.parent/'refinement-test';ledger=json.loads((out/'ledger.json').read_text())
    assert ledger['tile_000524']['state']=='source_unavailable'
    assert ledger['tile_000524']['scientific_absence_not_inferred']
    assert ledger['tile_000525']['state']=='no_regions_above_area_threshold'
    pointer=json.loads((out/'latest.json').read_text())
    assert pointer['index_path'].endswith('-complete.json')
    assert json.loads((out/pointer['index_path']).read_text())['progress']['state']=='complete'


def test_shadow_nearness_is_diagnostic_but_weak_or_unbounded_contour_still_requires_review():
    from flakefinder.graphene_useful_regions import contour_blockers
    diagnostic='diffuse_boundary_near_dark_feature_possible_shadow'
    flags=[diagnostic,'candidate_cut_by_crop_or_camera']
    assert contour_blockers(flags)==[]
    assert flags==[diagnostic,'candidate_cut_by_crop_or_camera']
    assert contour_blockers([diagnostic,'diffuse_or_weak_boundary'])==['diffuse_or_weak_boundary']
    assert contour_blockers([diagnostic,'large_area_change_requires_review'])==['large_area_change_requires_review']
