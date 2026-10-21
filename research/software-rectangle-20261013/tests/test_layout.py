import numpy as np
import pytest
from flakefinder.model import CameraTransform, Frame, Position
from flakefinder.layout import (SubstrateReference, Observation, SparseOccupancy,
                               boundary_segments, SUBSTRATE, NON_SUBSTRATE)


def frame(rgb=None, x=0., y=0.):
    if rgb is None:
        rng = np.random.default_rng(73)
        rgb = np.rint(np.clip(np.array([175., 200., 154.])+rng.normal(0, 2, (200, 300, 3)), 0, 255)).astype('uint8')
    return Frame(rgb, Position(x, y, 0.), (1., 1.), 0, metadata={'white_level': 255})


def observation(x, y, label, name=None, transform=None):
    f = frame(x=x, y=y)
    transform = transform or CameraTransform([[1., 0.], [0., 1.]], (1., 1.), True)
    return Observation(name or f'{x}_{y}', f, transform, np.full((200, 300), label, np.uint8))


def test_raw_reference_separates_holder_and_tolerates_shading():
    reference = SubstrateReference.from_frame(frame())
    a = frame().rgb.copy()
    # Multiplicative illumination variation retains chromaticity.
    a[:, :150] = np.rint(a[:, :150].astype(float)*np.linspace(.5, 1., 150)[None, :, None]).astype('uint8')
    a[:, 150:] = [161, 200, 81]
    labels = reference.classify(frame(a))
    assert np.mean(labels[:, :150] == SUBSTRATE) > .98
    assert np.mean(labels[:, 150:] == NON_SUBSTRATE) > .99
    assert np.mean(reference.classify(frame()) == SUBSTRATE) > .98


def test_dark_clipped_and_changed_context_are_not_substrate():
    reference = SubstrateReference.from_frame(frame())
    a = frame().rgb.copy()
    a[:30] = 0
    a[30:60] = 255
    assert not reference.classify(frame(a))[:60].any()
    changed = frame()
    changed.metadata['objective_position'] = 3
    with pytest.raises(ValueError, match='context'):
        reference.classify(changed)


def test_sparse_positives_remain_disconnected_across_unobserved_gap():
    layout = SparseOccupancy([observation(0, 0, SUBSTRATE), observation(2000, 0, SUBSTRATE)])
    assert layout.query([1000, 0])['status'] == 'unobserved'
    assert layout.query([0, 0])['status'] == 'substrate'
    report = layout.report()
    assert report['grid']['chip_count'] is None
    assert not report['edge_brackets']
    assert len(report['unknown_gaps']) == 1
    assert report['recommendations'][0]['reason'] == 'test_continuity_between_substrate_observations'
    assert report['recommendations'][0]['next_xy_um'] == pytest.approx([1000, 0], abs=.5)


def test_opposite_observations_bracket_gap_without_claiming_single_edge():
    layout = SparseOccupancy([observation(0, 0, NON_SUBSTRATE), observation(2000, 0, SUBSTRATE)])
    bracket = layout.report()['edge_brackets'][0]
    assert bracket['transition_count'] == 'one or more'
    assert 1699 <= bracket['width_um'] <= 1702
    assert bracket['endpoints_um'][0][0] == pytest.approx(150., abs=1.)
    assert bracket['endpoints_um'][1][0] == pytest.approx(1850., abs=1.)
    # The new midpoint replaces the old bracket, making the next operation sequential.
    layout.observations.append(observation(1000, 0, SUBSTRATE))
    bracket = layout.report()['edge_brackets'][0]
    assert bracket['next_xy_um'] == pytest.approx([500., 0.], abs=1.)


def test_overlapping_conflicting_observations_are_unknown():
    layout = SparseOccupancy([observation(0, 0, NON_SUBSTRATE), observation(0, 0, SUBSTRATE)])
    assert layout.query([0, 0]) == {'status': 'unknown', 'observations': 2, 'conflict': True}


def test_observed_boundary_maps_full_source_pixels_through_rotated_transform():
    transform = CameraTransform([[0., -1.], [1., 0.]], (1., 1.), True)
    obs = observation(1000, 2000, NON_SUBSTRATE, transform=transform)
    # Simulate the lower-resolution classification used on larger raw frames.
    obs.labels = np.full((100, 150), NON_SUBSTRATE, np.uint8)
    obs.labels[:, :75] = SUBSTRATE
    boundaries = boundary_segments(obs)
    assert len(boundaries) == 1
    boundary = boundaries[0]
    assert np.linalg.norm(np.diff(boundary['endpoints_um'], axis=0)) > 180
    assert np.array(boundary['endpoints_um'])[:, 1] == pytest.approx([1999., 1999.])
    for pixel, xy in zip(boundary['endpoints_px'], boundary['endpoints_um']):
        assert transform.stage_xy(pixel, obs.frame) == pytest.approx(xy)


def test_fov_rim_and_small_flake_holes_are_not_chip_boundaries():
    obs = observation(0, 0, SUBSTRATE)
    assert not boundary_segments(obs)
    obs.labels[50:80, 50:80] = NON_SUBSTRATE
    assert not boundary_segments(obs)


def test_pooled_verified_references_retain_legitimate_red_channel_variation():
    refs=[]
    for red in (165,175,195):
        a=frame().rgb.copy();a[:,:,0]=np.clip(a[:,:,0].astype(int)+red-175,0,255).astype('uint8');refs.append(frame(a))
    reference=SubstrateReference.from_frames(refs)
    for f in refs:
        assert np.mean(reference.classify(f)==SUBSTRATE)>.98
    holder=np.empty_like(refs[0].rgb);holder[:]=[161,200,81]
    assert np.mean(reference.classify(frame(holder))==NON_SUBSTRATE)>.99
