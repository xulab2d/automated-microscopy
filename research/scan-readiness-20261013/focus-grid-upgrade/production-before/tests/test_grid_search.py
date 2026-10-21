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


def test_estimates_and_arbitrary_seed_cannot_anchor_grid():
    result = fit_grid_prior([observation('one', (25000, 40000)),
                             observation('two', (35000, 40000), 'estimated')])
    assert result['status'] == 'insufficient_evidence'
    assert result['anchor_centres_um'] == [[25000., 40000.]]
    assert not result['candidate_centres_um']


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
    assert grid_probe_order(plan, {0,1,2}, [0,0], {'candidate_centres_um': [[10000,0]]}) == [2,1,0]
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
