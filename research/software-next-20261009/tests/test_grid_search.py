import math

import numpy as np
import pytest

from flakefinder.grid_search import GridPriorOptions, fit_grid_prior, grid_probe_order


def observation(identifier, point, status='measured_enclosure'):
    x, y = point
    return {'id': identifier, 'boundary_status': status, 'centre_um': [999999, 999999],
            'stage_polygon_um': [[x-2500,y-2500],[x+2500,y-2500],[x+2500,y+2500],[x-2500,y+2500]]}


def rotated_grid(angle=31., translation=(17000., -28000.)):
    theta = math.radians(angle)
    matrix = np.array([[math.cos(theta), -math.sin(theta)], [math.sin(theta), math.cos(theta)]])
    return np.array([[x*10000., y*10000.] for y in range(4) for x in range(4)]) @ matrix.T + translation


def test_unknown_translation_rotation_recovers_measured_grid():
    grid = rotated_grid()
    result = fit_grid_prior([observation(str(i), grid[i]) for i in (0, 3, 12, 15)])
    assert result['status'] == 'supported'
    assert result['anchor_count'] == 4
    assert len(result['candidate_centres_um']) == 12
    assert result['all_chips_found'] is False
    fitted = np.asarray(result['hypotheses'][0]['site_centres_um'])
    assert np.max(np.min(np.linalg.norm(grid[:, None, :] - fitted[None, :, :], axis=2), axis=1)) < .001


def test_two_adjacent_anchors_retain_translation_ambiguity():
    grid = rotated_grid()
    result = fit_grid_prior([observation(str(i), grid[i]) for i in (5, 6)])
    assert result['status'] == 'ambiguous'
    assert len(result['hypotheses']) > 1
    assert any(np.max(np.min(np.linalg.norm(grid[:, None, :] - np.array(h['site_centres_um'])[None, :, :], axis=2), axis=1)) < .001
               for h in result['hypotheses'])


def test_unqualified_estimates_and_arbitrary_seed_cannot_anchor_grid():
    result = fit_grid_prior([observation('one', (25000, 40000)),
                             observation('two', (35000, 40000), 'estimated')])
    assert result['status'] == 'one_anchor_proposals'
    assert result['anchor_centres_um'] == [[25000., 40000.]]
    assert result['measured_anchor_count'] == result['search_anchor_count'] == 1
    assert len(result['hypotheses']) == 16


def test_inconsistent_measurements_do_not_invent_grid():
    result = fit_grid_prior([observation('a', (0, 0)), observation('b', (100000, 0))])
    assert result['status'] == 'inconsistent'
    assert result['hypotheses'] == []


def test_rough_spacing_and_measurement_jitter_supported():
    grid = rotated_grid() * 1.08
    grid[15] += [250, -200]
    result = fit_grid_prior([observation(str(i), grid[i]) for i in (0, 3, 12, 15)])
    assert result['hypotheses']
    assert result['hypotheses'][0]['max_residual_um'] < 1000


def test_order_is_only_priority_and_preserves_explicit_envelope():
    plan = {'probe_centres_um': [[0.,0.], [5000.,0.], [10000.,0.]],
            'bounds_um': [0.,10000.,-100.,100.]}
    assert grid_probe_order(plan, {0,1,2}, [0,0], {'candidate_centres_um': [[10000,0]]}) == [2,0,1]
    assert grid_probe_order(plan, {0,1,2}, [0,0], {'candidate_centres_um': [[20000,0]]}) == [0,1,2]
    assert grid_probe_order(plan, {0,2}, [0,0], {'candidate_centres_um': [[10000,0]]}) == [2,0]


def test_hypothesis_limit_discloses_discarded_ambiguity():
    result = fit_grid_prior([observation('a',(0,0)), observation('b',(10000,0))], {'max_hypotheses': 1})
    assert result['status'] == 'ambiguous'
    assert result['hypotheses_truncated']
    assert len(result['hypotheses']) == 1


def test_invalid_or_duplicate_measured_evidence_rejected():
    with pytest.raises(ValueError, match='Duplicate'):
        fit_grid_prior([observation('a',(0,0)), observation('a',(10000,0))])
    with pytest.raises(ValueError):
        GridPriorOptions(spacing_um=float('nan'))


def estimated(identifier, point, orientation=0., uncertainty=250.):
    measured=observation(identifier,point)
    return {'id':identifier,'boundary_status':'estimated',
        'estimated_outline':{'status':'estimated','method':'measured_sides_shared_rectangle_orientation',
            'polygon_um':measured['stage_polygon_um'],'uncertainty_um':uncertainty,
            'orientation':{'orientation_deg':orientation},
            'lines':[{'edge_point_count':20,'source_images':['side%d.tiff'%i]} for i in range(4)]}}


@pytest.mark.parametrize('slot',[0,5,15])
def test_first_rectangle_unknown_corner_or_interior_index_keeps_all_placements(slot):
    grid=rotated_grid(angle=23.)
    result=fit_grid_prior([estimated('first',grid[slot],orientation=23.)])
    assert result['status']=='one_anchor_proposals'
    assert result['measured_anchor_count']==0 and result['search_anchor_count']==1
    assert len(result['hypotheses'])==16
    assert {tuple(h['first_chip_slot']) for h in result['hypotheses']}=={(y,x) for y in range(4) for x in range(4)}
    expected=result['hypotheses'][slot]
    assert np.allclose(expected['site_centres_um'],grid)
    assert expected['grid_rotation_measured'] is False
    assert result['grid_orientation_status']=='unmeasured'
    assert result['orientation_uncertainty_deg']==90.
    assert result['all_chips_found'] is False
    assert any(c['kind']=='unverified_orientation_neighbor_probe' for c in result['candidate_sites'])


def test_one_anchor_does_not_truncate_unknown_slot_to_a_corner():
    result=fit_grid_prior([estimated('first',[0.,0.])],{'max_hypotheses':1})
    assert len(result['hypotheses'])==16
    assert result['hypotheses_truncated'] is False


def test_nearby_neighbors_precede_far_union_extremes_and_travel_breaks_ties():
    fit=fit_grid_prior([estimated('first',[0.,0.])])
    plan={'probe_centres_um':[[10100.,100.],[30000.,0.],[-10000.,0.],[0.,10000.],[0.,-10000.]],
          'bounds_um':[-40000.,40000.,-40000.,40000.]}
    order=grid_probe_order(plan,set(range(5)),[5000.,0.],fit)
    assert order[0]==0 # A slightly imperfect nearby target beats exact far site.
    assert order.index(1)>order.index(2)
    assert set(order)==set(range(5))


def test_mild_grid_rotation_and_pitch_error_remain_near_first_neighbor_proposals():
    fit=fit_grid_prior([estimated('first',[0.,0.],orientation=0.)])
    angle=math.radians(7.)
    adjacent=[10800*math.cos(angle),10800*math.sin(angle)]
    plan={'probe_centres_um':[[100.,100.],adjacent,[30000.,0.]],
          'bounds_um':[-40000.,40000.,-40000.,40000.]}
    assert grid_probe_order(plan,{0,1,2},[0.,0.],fit)[0]==1
    refined=fit_grid_prior([estimated('first',[0.,0.],orientation=0.),estimated('second',adjacent,orientation=0.)])
    assert refined['status']=='ambiguous'
    assert refined['measured_anchor_count']==0 and refined['search_anchor_count']==2
    assert any(abs(h['spacing_um']-10800)<.001 and
               abs(((h['rotation_deg']-7+45)%90)-45)<.001 for h in refined['hypotheses'])


def test_isolated_off_grid_and_repeated_same_point_misses_do_not_exclude_site():
    observations=[estimated('first',[0.,0.])]
    original=fit_grid_prior(observations)
    miss={'requested_xy_um':[10000.,0.],'classification':'non_substrate_sample'}
    single=fit_grid_prior(observations,misses=[miss,miss,
        {'requested_xy_um':[45000.,45000.],'classification':'non_substrate_sample'},
        {'requested_xy_um':[10500.,0.],'classification':'uncertain'}])
    assert single['candidate_centres_um']==original['candidate_centres_um']
    site=next(c for c in single['candidate_sites'] if np.allclose(c['xy_um'],[10000,0]))
    assert site['corroborating_local_misses']==1 and site['miss_penalty']==0
    confirmed=fit_grid_prior(observations,misses=[miss,
        {'position_um':{'x':11000.,'y':0.,'z':100.},'classification':'non_substrate_sample'}])
    site=next(c for c in confirmed['candidate_sites'] if np.allclose(c['xy_um'],[10000,0]))
    assert site['corroborating_local_misses']==2 and site['miss_penalty']>0
    assert confirmed['candidate_centres_um']==original['candidate_centres_um']
    assert len(confirmed['hypotheses'])==16


def test_bounds_and_periodic_baseline_preserve_complete_fallback():
    fit=fit_grid_prior([estimated('first',[0.,0.])])
    plan={'probe_centres_um':[[0.,0.],[10000.,0.],[20000.,0.],[30000.,0.]],
          'bounds_um':[-100.,31000.,-100.,100.]}
    assert grid_probe_order(plan,{0,1,2,3},[0.,0.],fit)[0]==1
    assert grid_probe_order(plan,{0,1,2,3},[0.,0.],fit,priority_streak=8)==[0,1,2,3]
    assert grid_probe_order(plan,{2,3},[0.,0.],fit)==[2,3]
    narrow={'probe_centres_um':[[0.,0.],[500.,0.]],'bounds_um':[-100.,600.,-100.,100.]}
    assert grid_probe_order(narrow,{0,1},[0.,0.],fit)==[0,1]


def test_rectangle_without_four_measured_sides_is_not_a_search_anchor():
    item=estimated('first',[0.,0.]);item['estimated_outline']['lines'].pop()
    result=fit_grid_prior([item])
    assert result['search_anchor_count']==0 and not result['candidate_centres_um']
