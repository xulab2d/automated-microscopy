import pytest
from flakefinder.grid_layout import infer_grid_hypotheses, centre_verification_plan


def probe(x, y, substrate=True):
    return {'name': f'{x}-{y}', 'position_um': [x, y, 0], 'centre_classification': 'substrate' if substrate else 'non_substrate'}


def cross():
    p = [probe(x, 30000) for x in (36000,40000,48000,50000,52000,60000,64000)]
    p += [probe(x,30000,False) for x in (44000,46000,54000,56000)]
    p += [probe(50000,y) for y in (16000,20000,28000,32000,40000,44000)]
    p += [probe(50000,y,False) for y in (24000,26000,34000,36000)]
    return p


def test_learn_pitch_and_recommend_extent_without_inventing_chip_count():
    r = infer_grid_hypotheses(cross(), expected_shape=(4,4))
    assert r['axes']['x']['pitch_um'] == pytest.approx(12000)
    assert r['axes']['y']['pitch_um'] == pytest.approx(12000)
    assert r['chip_count'] is None
    assert {tuple(p['xy_um']) for p in r['recommendations'] if p['priority']==1} == {(26000,30000),(74000,30000),(50000,6000),(50000,54000)}
    assert sum(p['status']=='substrate_observed_near_predicted_centre' for p in r['cells']) == 1
    assert all(p['status']=='unverified' for p in r['recommendations'])


def test_unseparated_positive_gaps_cannot_establish_pitch():
    p = [probe(x, 0) for x in (0,10000,20000)] + [probe(0,y) for y in (10000,20000)]
    r = infer_grid_hypotheses(p)
    assert r['status'] == 'insufficient_evidence'
    assert not r['cells']


def test_observed_centre_negative_does_not_become_missing_chip_claim():
    r = infer_grid_hypotheses(cross()+[probe(38000,18000,False)])
    cell = next(p for p in r['cells'] if p['xy_um']==[38000,18000])
    assert cell['status']=='non_substrate_observed_near_predicted_centre'
    assert r['chip_count'] is None
    assert not any(p['xy_um']==[38000,18000] for p in r['recommendations'])


def test_travel_bounds_mark_predictions_without_hiding_unmeasured_extent():
    r = infer_grid_hypotheses(cross(), search_bounds_um=[34000,66000,14000,46000])
    extent = [p for p in r['recommendations'] if p['priority']==1]
    assert len(extent)==4
    assert all(not p['within_search_bounds'] for p in extent)
    assert all(p['status']=='unverified' for p in extent)


def test_inconsistent_supplied_pitch_is_rejected_as_evidence():
    r = infer_grid_hypotheses(cross(), pitch_um=19000, residual_fraction=.05)
    assert r['status']=='insufficient_evidence'
    assert r['axes']['x']['status']=='inconsistent'


def test_prior_lattice_extends_from_outer_positives_without_merging_across_gap():
    prior = infer_grid_hypotheses(cross())
    p = cross()+[probe(26000,30000),probe(50000,6000)]
    ambiguous = infer_grid_hypotheses(p)
    assert ambiguous['status']=='insufficient_evidence'
    r = infer_grid_hypotheses(p, prior_grid=prior, extend_cells=0, expected_shape=(4,4))
    assert r['axes']['x']['pitch_um']==pytest.approx(12000)
    assert len(r['cells'])==16
    plan = centre_verification_plan(r)
    assert len(plan['requested_points_um'])==13
    assert len(plan['already_observed_centres'])==3
    assert {tuple(c['xy_um']) for c in plan['already_observed_centres']}=={(26000,30000),(50000,30000),(50000,6000)}
