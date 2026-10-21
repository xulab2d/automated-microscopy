"""Reference bootstrap simulated motion/photometry only; no hardware clients."""
from dataclasses import asdict
import json

import numpy as np
import pytest

import flakefinder.autonomous_reference as bootstrap
from flakefinder.autofocus import FocusResult
from flakefinder.imaging import save_frame
from flakefinder.model import AcquisitionError, CameraTransform, Cancelled, Frame, Limits, Position, SafetyError


class Stage:
    def __init__(self, x=0., uniform=False):
        self.p = Position(x, 0., 100.)
        self.limits = Limits(-6000, 6000, -6000, 6000, 0, 200)
        self.armed = True
        self.uniform = uniform
        self.sequence = 0
        self.moves = []
        self.exposure = 10.

    def position(self):
        return self.p

    def check_cancel(self):
        pass

    def travel(self, x, y):
        self.p = Position(x, y, self.p.z)
        self.limits.check(self.p)
        self.moves.append(self.p)

    def move_z(self, z):
        self.p = Position(self.p.x, self.p.y, z)

    def capture(self):
        self.sequence += 1
        xx = np.arange(256)-127.5+self.p.x
        bright = (xx >= 0) & (xx < 5000) & (not self.uniform)
        rgb = np.empty((256, 256, 3), dtype=np.uint8)
        rgb[:] = np.where(bright[None, :, None], [145, 105, 155], [18, 18, 18])
        return Frame(rgb, self.p, (1., 1.), self.sequence,
                     metadata={'white_level': 255, 'camera_resources': {'ff_exposure_ms': self.exposure}})


def setup(monkeypatch, tmp_path, *, x=0., uniform=False, focus_success=True):
    scope = Stage(x, uniform)
    transform = CameraTransform([[1., 0.], [0., 1.]], (1., 1.))
    config = {'camera_transform': asdict(transform), 'context': {'exposure_ms': 10.}}
    calls = []

    def focus(adapter, low, high, output, **kwargs):
        assert (low, high) == (0, 200)
        assert adapter.box is not None and adapter.reference_frame is not None
        crop = adapter.capture()
        assert crop.metadata['focus_mask_policy'] == 'include_chip_edge'
        scope.move_z(105)
        result = FocusResult(focus_success, 105., 1., 1., 'simulated independent confirmation', [],
                             outcome='resolved_peak' if focus_success else 'non_peak')
        bootstrap.atomic_json(output/'focus-result.json', asdict(result))
        calls.append('focus')
        return result

    def capture(scope, config, settings, run, position, check, update):
        check()
        frame = scope.capture()
        assert frame.position == position
        save_frame(run/'calibration', 'reference-attempt-01', frame)
        return frame, {'attempts': [], 'chosen_exposure_ms': 10.}, None

    monkeypatch.setattr(bootstrap, 'recover_focus', focus)
    arguments = dict(scope=scope, config=config, settings={'exposure_ms': 10.}, run=tmp_path,
                     position=scope.position(), bounds=[-5500, 5500, -5500, 5500],
                     check=lambda: None, update=lambda *a, **kw: None,
                     capture_reference=capture, validate_final=lambda frame: calls.append('validate'))
    return scope, arguments, calls


def test_edge_focus_and_independent_bright_reference(monkeypatch, tmp_path):
    scope, arguments, calls = setup(monkeypatch, tmp_path)
    frame, audit, receipt, proof = bootstrap.acquire_reference(**arguments)
    assert calls == ['focus', 'validate']
    assert frame.position.x > 128 and frame.position.z == 105
    assert proof['state'] == 'accepted'
    assert proof['reference_controls']['accepted']
    assert proof['edge_evidence']['raw_validation']['step_polarity'] == 'outside_darker'
    assert proof['budget']['captures_started'] == scope.sequence
    assert audit['bootstrap_proof_sha256']
    assert all(row['raw_sha256'] and row['metadata_sha256'] for row in proof['observations'])
    assert scope.position() == frame.position


def test_homogeneous_start_searches_and_brackets_chip(monkeypatch, tmp_path):
    scope, arguments, calls = setup(monkeypatch, tmp_path, x=-1500.)
    _, _, _, proof = bootstrap.acquire_reference(**arguments)
    assert len(proof['observations']) > 2
    assert any('bracket-' in row['raw_path'] for row in proof['observations'])
    assert calls == ['focus', 'validate']
    assert all(-5500 <= p.x <= 5500 and -5500 <= p.y <= 5500 for p in scope.moves)


def test_uniform_holder_cannot_self_certify_as_reference(monkeypatch, tmp_path):
    scope, arguments, calls = setup(monkeypatch, tmp_path, uniform=True)
    with pytest.raises(AcquisitionError, match='no focusable'):
        bootstrap.acquire_reference(**arguments, options=bootstrap.BootstrapOptions(max_search_fields=4))
    assert not calls
    assert scope.sequence == 4
    proof = json.loads((tmp_path/'autonomous-reference/proof.json').read_text())
    assert proof['state'] == 'failed'


def test_unresolved_focus_never_proceeds_to_reference(monkeypatch, tmp_path):
    _, arguments, calls = setup(monkeypatch, tmp_path, focus_success=False)
    with pytest.raises(AcquisitionError, match='focus unresolved'):
        bootstrap.acquire_reference(**arguments)
    assert calls == ['focus']


def test_cancel_before_first_capture_or_move(monkeypatch, tmp_path):
    scope, arguments, calls = setup(monkeypatch, tmp_path)
    def stop():
        raise Cancelled('operator STOP')
    arguments['check'] = stop
    with pytest.raises(Cancelled):
        bootstrap.acquire_reference(**arguments)
    assert scope.sequence == 0 and not scope.moves and not calls


def test_shared_capture_budget_includes_search_exposure_and_focus(monkeypatch, tmp_path):
    scope, arguments, calls = setup(monkeypatch, tmp_path)
    with pytest.raises(AcquisitionError, match='capture budget'):
        bootstrap.acquire_reference(**arguments, options=bootstrap.BootstrapOptions(max_captures=2))
    assert scope.sequence == 2
    assert not calls


def test_interior_target_must_remain_inside_current_bounds(monkeypatch, tmp_path):
    scope, arguments, calls = setup(monkeypatch, tmp_path)
    arguments['bounds'] = [-100, 100, -100, 100]
    with pytest.raises(SafetyError, match='outside the new placement'):
        bootstrap.acquire_reference(**arguments)
    assert not scope.moves


def test_dark_reference_rejected_by_independent_control(monkeypatch, tmp_path):
    scope, arguments, _ = setup(monkeypatch, tmp_path)
    frame = scope.capture()
    transform = CameraTransform(**arguments['config']['camera_transform'])
    evidence = bootstrap.edge_evidence(frame, transform)
    scope.uniform = True
    with pytest.raises(AcquisitionError, match='independently measured bright'):
        bootstrap.verify_reference(scope.capture(), frame, evidence, transform)


def test_travel_budget_stops_before_long_search_move(monkeypatch, tmp_path):
    scope, arguments, _ = setup(monkeypatch, tmp_path, uniform=True)
    with pytest.raises(AcquisitionError, match='XY travel budget'):
        bootstrap.acquire_reference(**arguments, options=bootstrap.BootstrapOptions(max_xy_travel_um=100))
    assert scope.sequence == 1 and not scope.moves


def test_invalid_bounds_do_not_touch_hardware(monkeypatch, tmp_path):
    scope, arguments, _ = setup(monkeypatch, tmp_path)
    arguments['bounds'] = [-7000, 5500, -5500, 5500]
    with pytest.raises(SafetyError, match='exceeds armed'):
        bootstrap.acquire_reference(**arguments)
    assert scope.sequence == 0 and not scope.moves


def test_changed_reference_exposure_recaptures_and_hashes_independent_edge(monkeypatch, tmp_path):
    scope, arguments, _ = setup(monkeypatch, tmp_path)
    original = arguments['capture_reference']
    def capture(*args):
        if args[3] == tmp_path:
            scope.exposure = 12.
            args[1]['context']['exposure_ms'] = 12.
        return original(*args)
    arguments['capture_reference'] = capture
    frame, _, _, proof = bootstrap.acquire_reference(**arguments)
    assert proof['focused_edge_path'].endswith('final-exposure-edge.tiff')
    assert proof['focused_edge_sha256'] == bootstrap.file_hash(proof['focused_edge_path'])
    assert scope.position() == frame.position
    assert proof['budget']['captures_started'] == scope.sequence


def test_same_field_proposal_cannot_certify_uniform_or_wrong_field(monkeypatch, tmp_path):
    scope, arguments, _ = setup(monkeypatch, tmp_path)
    transform = CameraTransform(**arguments['config']['camera_transform'])
    selected = bootstrap.select_edge_roi(scope.capture(), 64., transform)
    scope.uniform = True
    with pytest.raises(AcquisitionError, match='independently supported'):
        bootstrap.edge_evidence(scope.capture(), transform, selection_hint=selected)
    scope.travel(100, 0)
    with pytest.raises(SafetyError, match='same current XY'):
        bootstrap.edge_evidence(scope.capture(), transform, selection_hint=selected)


def test_saved_busy_focus_field_replay():
    """Opt-in immutable real-image replay; reads files only, never hardware.

    Set FLAKEFINDER_BOOTSTRAP_REPLAY to a saved failed run containing its camera
    profile and autonomous-reference focused-edge raw/proof. No replay position
    is exported as a live movement target or used by production acquisition.
    """
    import os
    from pathlib import Path
    from flakefinder.imaging import load_frame
    location = os.environ.get('FLAKEFINDER_BOOTSTRAP_REPLAY')
    if not location:
        pytest.skip('Saved-image replay requires explicit fixture run path.')
    run = Path(location)
    frame = load_frame(run/'autonomous-reference/focused-edge.tiff')
    transform = CameraTransform(**json.loads((run/'camera-profile-used.json').read_text())['camera_transform'])
    selected = json.loads((run/'autonomous-reference/proof.json').read_text())['edge_selection']
    with pytest.raises(AcquisitionError, match='coherent gradient or long separating'):
        bootstrap.edge_focus_roi(frame, 64., transform)
    for hint in (selected, None):
        evidence = bootstrap.edge_evidence(frame, transform, selection_hint=hint)
        raw = evidence['raw_validation']
        assert raw['accepted'] and raw['supported_profiles'] == 60
        assert raw['step_polarity'] == 'outside_darker'
        assert raw['raw_span_px'] >= .45*min(raw['raw_working_shape'])
        assert raw['orientation_drift_px'] <= raw['allowed_orientation_drift_px']
        assert raw['raw_scatter_px'] <= raw['raw_transition_width_px_p90']
        if hint is None:
            assert evidence['selection']['method'] == 'current_frame_lowpass_separator_proposal'
        # The identical proposal must still reject loss of the raw transition.
        uniform = Frame(np.full_like(frame.rgb, 500), frame.position, frame.pixel_size_um,
                        frame.sequence, frame.acquired_at, frame.metadata)
        with pytest.raises(AcquisitionError, match='independently supported'):
            bootstrap.edge_evidence(uniform, transform, selection_hint=evidence['selection'])


def test_clean_patch_retry_preserves_first_raw_and_revalidates(monkeypatch, tmp_path):
    scope, arguments, _ = setup(monkeypatch, tmp_path)
    seen=[]
    def validate(frame):
        seen.append(frame.position)
        if len(seen)==1:raise AcquisitionError('central patch contains a flake')
    arguments['validate_final']=validate
    frame, _, _, proof=bootstrap.acquire_reference(**arguments)
    assert len(seen)==2 and seen[0]!=seen[1] and frame.position==seen[1]
    assert (tmp_path/'calibration/reference-attempt-01.tiff').is_file()
    assert (tmp_path/'autonomous-reference/reference-candidate-02/calibration/reference-attempt-01.tiff').is_file()
    assert [r['accepted'] for r in proof['reference_candidates']]==[False,True]
    assert all(r['reference_controls']['accepted'] for r in proof['reference_candidates'])
    assert scope.sequence==proof['budget']['captures_started']


def test_clean_patch_retries_stop_after_three_candidates(monkeypatch, tmp_path):
    _, arguments, _ = setup(monkeypatch, tmp_path)
    def reject(frame):raise AcquisitionError('central patch still rejected')
    arguments['validate_final']=reject
    with pytest.raises(AcquisitionError,match='still rejected'):
        bootstrap.acquire_reference(**arguments)
    proof=json.loads((tmp_path/'autonomous-reference/proof.json').read_text())
    assert len(proof['reference_candidates'])==3
    assert all(not r['accepted'] for r in proof['reference_candidates'])


def test_reference_safety_failure_does_not_retry(monkeypatch, tmp_path):
    _, arguments, _ = setup(monkeypatch, tmp_path)
    def reject(frame):raise SafetyError('objective changed')
    arguments['validate_final']=reject
    with pytest.raises(SafetyError,match='objective changed'):
        bootstrap.acquire_reference(**arguments)
    proof=json.loads((tmp_path/'autonomous-reference/proof.json').read_text())
    assert len(proof['reference_candidates'])==1


def test_saved_reference_clean_patch_replay():
    import os
    from pathlib import Path
    from flakefinder.imaging import load_frame
    location=os.environ.get('FLAKEFINDER_REFERENCE_PATCH_REPLAY')
    if not location:pytest.skip('Clean-patch replay requires explicit immutable run fixture.')
    run=Path(location);frame=load_frame(run/'calibration/reference-attempt-01.tiff')
    transform=CameraTransform(**json.loads((run/'camera-profile-used.json').read_text())['camera_transform'])
    proof=json.loads((run/'autonomous-reference/proof.json').read_text())
    proposal=bootstrap.clean_reference_target(frame,transform,proof['edge_evidence'],proof['bounds_um'],
        attempted=[[frame.position.x,frame.position.y]])
    assert proposal['previous_central_patch']['patch_substrate_fraction']<.90
    assert proposal['field_substrate_fraction']>.55 and proposal['fully_supported_radius_um']>20.
    assert np.allclose(transform.stage_xy(proposal['source_pixel'],frame),proposal['point_xy_um'])
    normal=np.asarray(proof['edge_evidence']['edge']['normal']);h,w=frame.rgb.shape[:2]
    half_extent=np.abs(np.asarray(transform.matrix).T@normal)@np.array([w/2,h/2])
    assert np.asarray(proposal['point_xy_um'])@normal+half_extent+60<=proof['edge_evidence']['edge']['offset_um']
