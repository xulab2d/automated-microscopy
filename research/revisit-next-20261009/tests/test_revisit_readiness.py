from dataclasses import asdict
from pathlib import Path
from types import SimpleNamespace
import copy
import json

import cv2
import numpy as np
import pytest

from flakefinder.calibration import calibrate_camera
from flakefinder.imaging import load_frame, save_frame
from flakefinder.model import Frame, Position, CameraTransform, SafetyError, atomic_json
from flakefinder.optics import optimize_exposure, save_objective_profile
from flakefinder.revisit_queue import export_graphene_revisit_queue, _canonical, _digest


class RetiringCamera:
    """Exercise the real raw persistence functions without a vendor client."""
    armed = True
    keep_vendor_results = False
    job_template = 'fake-camera'
    expected_scale = (.1, .1)

    def __init__(self):
        self.here = Position(0, 0, 100)
        self.sequence = 0
        self.pending = {}
        self.maximum_pending = 0
        self.stopped = False
        self.camera_resources = {'ff_exposure_ms': 100., 'ff_white_balance': '1,1,1'}
        self.base = np.random.default_rng(12).integers(800, 2500, (256, 256, 3), dtype=np.uint16)

    def check_cancel(self):
        pass

    def position(self):
        return self.here

    def travel(self, x, y):
        self.here = Position(x, y, self.here.z)

    def stop(self):
        self.stopped = True
        return []

    def set_exposure_ms(self, value):
        self.camera_resources['ff_exposure_ms'] = float(value)

    def capture(self):
        if len(self.pending) >= 4:
            raise RuntimeError('Unacknowledged fake vendor output limit')
        shifted = cv2.warpAffine(self.base, np.float32([[1, 0, -10*self.here.x], [0, 1, -10*self.here.y]]),
                                (256, 256), borderMode=cv2.BORDER_REFLECT)
        rgb = np.uint16(np.clip(shifted.astype(float)*self.camera_resources['ff_exposure_ms']/100, 0, 4095))
        frame = Frame(rgb, self.here, (.1, .1), self.sequence, metadata={
            'source': 'FAKE', 'camera_resources': dict(self.camera_resources), 'white_level': 4095})
        self.pending[self.sequence] = frame
        self.sequence += 1
        self.maximum_pending = max(self.maximum_pending, len(self.pending))
        return frame

    def vendor_frame_pending(self, frame):
        return frame.sequence in self.pending

    def ack_persisted_frame(self, frame, path):
        saved = load_frame(path)
        assert np.array_equal(saved.rgb, frame.rgb)
        assert saved.position == frame.position and saved.metadata == frame.metadata
        del self.pending[frame.sequence]
        return {'status': 'removed', 'fake': True}

    def hardware_controls(self):
        return SimpleNamespace(objectives=lambda: {
            'position': 4, 'motorization': 'Coded', 'objectives': [
                {'position': 4, 'name': 'FAKE100x', 'magnification': 100.}]}, lamp_percent=lambda: 75.)


def test_calibration_releases_six_saved_frames_without_pending_accumulation(tmp_path):
    scope = RetiringCamera()
    transform, quality = calibrate_camera(scope, step_um=1, output=tmp_path/'calibration')
    assert transform.calibrated and quality['return_error_um'] < .01
    assert scope.sequence == 6 and scope.maximum_pending == 1 and not scope.pending
    assert len(list((tmp_path/'calibration').glob('*.tiff'))) == 6
    assert np.allclose(transform.matrix, [[.1, 0], [0, .1]], atol=.003)


def test_vendor_retiring_calibration_requires_output_before_capture_or_travel():
    scope = RetiringCamera()
    with pytest.raises(SafetyError, match='durable output'):
        calibrate_camera(scope, output=None)
    assert scope.sequence == 0 and scope.position() == Position(0, 0, 100)


def test_exposure_and_profile_preserve_raw_before_vendor_release(tmp_path):
    scope = RetiringCamera()
    result = optimize_exposure(scope, tmp_path/'exposure', initial_ms=30.)
    assert len(result['steps']) >= 2 and not scope.pending and scope.maximum_pending == 1
    transform = CameraTransform([[.1, 0], [0, .1]], (.1, .1), True)
    profile = save_objective_profile(scope, {'context': {'sample_id': 'g90'},
                                    'focus': {'half_range_um': 1, 'coarse_step_um': .25, 'fine_step_um': .125}},
                                     transform, tmp_path/'100x.json')
    assert profile['verification_capture']['sha256'] == _digest(Path(profile['verification_capture']['path']))
    assert profile['parcentric_offset'] is None and profile['parcentric_status'] == 'not_measured'
    assert not scope.pending
    before = scope.sequence
    with pytest.raises(SafetyError, match='immutable'):
        save_objective_profile(scope, {'context': {'sample_id': 'g90'}}, transform, tmp_path/'100x.json')
    assert scope.sequence == before


def test_ack_failure_keeps_pending_and_does_not_take_more_frames(tmp_path):
    scope = RetiringCamera()
    def fail(frame, path):
        assert Path(path).is_file() and Path(path).with_suffix('.json').is_file()
        raise SafetyError('receipt verification failed')
    scope.ack_persisted_frame = fail
    with pytest.raises(SafetyError, match='receipt verification failed'):
        optimize_exposure(scope, tmp_path/'exposure', initial_ms=30.)
    assert scope.sequence == 1 and len(scope.pending) == 1


def test_objective_profile_cli_supplies_autofocus_receipt_destination(tmp_path, monkeypatch):
    import flakefinder.__main__ as cli
    import flakefinder.autofocus as af
    scope = RetiringCamera()
    scope.close = lambda: None
    config = {'context': {'sample_id': 'g90'}, 'focus': {'half_range_um': 1}, 'calibration_step_um': 1}
    monkeypatch.setattr(cli, 'load_config', lambda path: config)
    monkeypatch.setattr(cli, 'connect', lambda config: scope)
    monkeypatch.setattr(cli, 'arm', lambda scope, config: None)
    seen = []
    def focus(scope, **options):
        seen.append(options['output'])
        assert options['output'] == tmp_path/'100x'/'focus'
        return SimpleNamespace(success=True)
    monkeypatch.setattr(af, 'autofocus', focus)
    monkeypatch.setattr('sys.argv', ['flakefinder', '--config', 'FAKE', 'objective-profile', '--output', str(tmp_path/'100x.json')])
    cli.main()
    assert seen and not scope.pending and scope.maximum_pending == 1


@pytest.fixture
def dataset(tmp_path):
    project = tmp_path/'project';project.mkdir()
    atomic_json(project/'.flakefinder-project.json', {'project_id': 'test-project'})
    run = project/'data/run-graphene';run.mkdir(parents=True)
    request = {'schema': 1, 'id': run.name, 'settings': {'sample_id': 'graphene-test', 'material': 'graphene', 'oxide_nm': 90}}
    atomic_json(run/'scan-request.json', request)
    atomic_json(run/'batch.json', {'coordinate_frame_id': 'fresh-frame'})
    atomic_json(run/'layout.json', {'coordinate_frame_id': 'fresh-frame'})
    transform = {'matrix': [[.1725, -.007], [.007, .1725]], 'pixel_size_um': [.1725, .1725], 'calibrated': True}
    chip = run/'chip001';tile = 'tile_000001';field = 'chip001/'+tile
    image = Frame(np.full((64, 80, 3), 2000, np.uint16), Position(120, 300, 15000), (.1725, .1725), 1,
                  metadata={'source': 'FAKE', 'white_level': 4095})
    save_frame(chip/'tiles', tile, image)
    (chip/'boundary-labels').mkdir()
    np.savez_compressed(chip/'boundary-labels'/(tile+'.npz'), labels=np.ones((64, 80), np.uint8))
    hashes = {p.relative_to(chip).as_posix(): _digest(p) for p in
              [chip/'tiles'/(tile+'.tiff'), chip/'tiles'/(tile+'.json'), chip/'boundary-labels'/(tile+'.npz')]}
    record = {'position': asdict(image.position), 'hashes': hashes}
    manifest = {'camera_shape': [64, 80, 3], 'transform': transform, 'context': request['settings'],
                'plan': {'chip_id': 'chip001'}, 'completed': {tile: record}}
    atomic_json(chip/'manifest.json', manifest)
    analysis = run/'analysis-graphene-incremental';analysis.mkdir()
    identity = {'project_id': 'test-project', 'run_id': run.name, 'scan_request_sha256': _digest(run/'scan-request.json')}
    atomic_json(analysis/'plan.json', {'identity': identity, 'coordinate_frame_id': 'fresh-frame'})
    attempt = analysis/'fields/chip001__tile_000001-fixed';attempt.mkdir(parents=True)
    labels = np.zeros((64, 80), np.uint32);labels[20:40, 25:50] = 1;labels[3:9, 4:12] = 2
    pixel = [35., 30.]
    xy = np.asarray(transform['matrix']) @ (np.asarray(pixel)-[(80-1)/2, (64-1)/2])+[120, 300]
    row = {'id': field+'/1', 'field': field, 'instance_label': 1, 'source_shape_hw': [64, 80],
           'source_position_um': record['position'], 'source_transform_sha256': _canonical(transform),
           'source_tiff_sha256': hashes['tiles/'+tile+'.tiff'], 'revisit_source_pixel_xy': pixel,
           'stage_xy_um': xy.tolist(), 'area_um2': 28., 'rank': 28., 'disposition': 'shortlist'}
    other = copy.deepcopy(row);other.update(id=field+'/2', instance_label=2, revisit_source_pixel_xy=[6., 6.], disposition='uncertain')
    other['stage_xy_um'] = (np.asarray(transform['matrix'])@(np.asarray([6., 6.])-[(80-1)/2, (64-1)/2])+[120, 300]).tolist()
    np.savez_compressed(attempt/'masks.npz', labels=labels)
    payload = {'field': field, 'rows': [row, other], 'source_record_sha256': _canonical(record),
               'source_hashes': hashes, 'source_transform': transform, 'mask_sha256': _digest(attempt/'masks.npz')}
    atomic_json(attempt/'field.json', payload)
    commit = {'field_json': (attempt/'field.json').relative_to(analysis).as_posix(),
              'source_record_sha256': _canonical(record),
              'geometry_sha256': _canonical({key: manifest[key] for key in ('transform', 'camera_shape', 'context', 'plan')}),
              'hashes': {p.relative_to(analysis).as_posix(): _digest(p) for p in attempt.iterdir()}}
    atomic_json(analysis/'ledger.json', {field: commit})
    atomic_json(analysis/'catalog.json', {**identity, 'coordinate_frame_id': 'fresh-frame', 'rows': [row]})
    return SimpleNamespace(project=project, run=run, chip=chip, tile=tile, field=field, analysis=analysis,
                           attempt=attempt, row=row, other=other, payload=payload, commit=commit, manifest=manifest)


def export(dataset, output, **kwargs):
    return export_graphene_revisit_queue(dataset.project, dataset.run.name, output, **kwargs)


def test_export_proves_source_but_never_invents_100x_target_coordinates(dataset, tmp_path):
    result = export(dataset, tmp_path/'queue')
    assert result['candidate_count'] == 1 and not result['motion_authorized'] and not result['hardware_accessed']
    assert result['readiness']['calibration_status'] == 'missing_measured_100x_profile'
    target = result['candidates'][0]
    assert target['source_stage_xy_um'] == dataset.row['stage_xy_um'] and target['target_stage_xy_um'] is None
    assert (tmp_path/'queue/targets.csv').is_file() and (tmp_path/'queue/READINESS.txt').is_file()
    uncertain = export(dataset, tmp_path/'explicit', candidate_ids=[dataset.other['id']])
    assert uncertain['candidates'][0]['disposition'] == 'uncertain'
    assert uncertain['candidates'][0]['selection'] == 'explicit_review_selection'


@pytest.mark.parametrize('corruption', ['raw', 'coordinate_frame', 'catalog_xy', 'geometry', 'mask', 'request'])
def test_export_rejects_changed_source_identity_or_pixels(dataset, tmp_path, corruption):
    if corruption == 'raw':
        with (dataset.chip/'tiles'/(dataset.tile+'.tiff')).open('ab') as stream: stream.write(b'changed')
    elif corruption == 'coordinate_frame':
        atomic_json(dataset.run/'batch.json', {'coordinate_frame_id': 're-zeroed'})
    elif corruption == 'catalog_xy':
        path = dataset.analysis/'catalog.json';data = json.loads(path.read_text());data['rows'][0]['stage_xy_um'][0] += 1;atomic_json(path, data)
    elif corruption == 'geometry':
        dataset.manifest['transform']['matrix'][0][0] *= 2;atomic_json(dataset.chip/'manifest.json', dataset.manifest)
    elif corruption == 'mask':
        with (dataset.attempt/'masks.npz').open('ab') as stream:stream.write(b'changed')
    else:
        path=dataset.run/'scan-request.json';data=json.loads(path.read_text());data['settings']['sample_id']='other';atomic_json(path,data)
    with pytest.raises(SafetyError): export(dataset, tmp_path/'queue')
    assert not (tmp_path/'queue').exists()


def test_rejects_a_hole_despite_self_consistent_hashes(dataset, tmp_path):
    with np.load(dataset.attempt/'masks.npz') as data: labels = data['labels']
    labels[30, 35] = 0
    np.savez_compressed(dataset.attempt/'masks.npz', labels=labels)
    dataset.payload['mask_sha256'] = _digest(dataset.attempt/'masks.npz')
    atomic_json(dataset.attempt/'field.json', dataset.payload)
    dataset.commit['hashes'] = {p.relative_to(dataset.analysis).as_posix(): _digest(p) for p in dataset.attempt.iterdir()}
    atomic_json(dataset.analysis/'ledger.json', {dataset.field: dataset.commit})
    with pytest.raises(SafetyError, match='inside a hole'):
        export(dataset, tmp_path/'queue')


def test_path_traversal_and_wrong_objective_profile_never_export(dataset, tmp_path):
    with pytest.raises(ValueError):
        export_graphene_revisit_queue(dataset.project, '../escape', tmp_path/'queue')
    profile = {'schema': 1, 'objective': {'magnification': 20}, 'context': {'sample_id': 'graphene-test'}}
    atomic_json(tmp_path/'wrong-profile.json', profile)
    with pytest.raises(SafetyError, match='100'):
        export(dataset, tmp_path/'queue', profile_path=tmp_path/'wrong-profile.json')
    assert not (tmp_path/'queue').exists()
