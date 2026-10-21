"""Offline, source-verified export of prospective high-magnification targets.

This module imports no microscope adapter and cannot perform motion. Source XY
coordinates are evidence from the survey, not calibrated target-objective XY.
"""
from __future__ import annotations

import csv
import hashlib
import json
import math
import re
import time
import zipfile
from pathlib import Path

import numpy as np

from .model import CameraTransform, SafetyError, atomic_json


def _read(path, maximum_bytes=64 * 1024**2):
    path = Path(path)
    if not path.is_file() or path.stat().st_size > maximum_bytes:
        raise SafetyError('Missing or oversized provenance JSON: ' + str(path))
    return json.loads(path.read_text(encoding='utf-8-sig'))


def _canonical(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, allow_nan=False).encode()).hexdigest()


def _digest(path):
    digest = hashlib.sha256()
    with Path(path).open('rb') as stream:
        for block in iter(lambda: stream.read(1024**2), b''):
            digest.update(block)
    return digest.hexdigest()


def _child(root, relative):
    relative = Path(relative)
    if relative.is_absolute() or '..' in relative.parts:
        raise SafetyError('Source path must be relative and confined: ' + str(relative))
    path = (root / relative).resolve(strict=True)
    if not path.is_relative_to(root.resolve()):
        raise SafetyError('Source redirects outside the pinned dataset.')
    return path


def _verify(path, expected):
    if not isinstance(expected, str) or not re.fullmatch('[0-9a-f]{64}', expected):
        raise SafetyError('Missing recorded SHA256 for ' + str(path))
    if _digest(path) != expected:
        raise SafetyError('Source checksum differs: ' + str(path))
    return expected


def _geometry(manifest):
    return _canonical({key: manifest.get(key) for key in ('transform', 'camera_shape', 'context', 'plan')})


def _labels(path, shape):
    # Decompress only the exact instance labels, not every analysis array.
    with zipfile.ZipFile(path) as archive:
        entry = archive.getinfo('labels.npy')
        if entry.file_size > 64 * 1024**2:
            raise SafetyError('Instance label array exceeds the bounded source-frame budget.')
    with np.load(path, allow_pickle=False) as arrays:
        labels = arrays['labels']
    if labels.shape != shape or labels.dtype.kind not in 'ui':
        raise SafetyError('Instance labels do not match the source image grid.')
    return labels


def assess_100x_readiness(sample_id, profile_path=None):
    """Report missing evidence; an offline export never authorizes travel."""
    blockers = ['current_coordinate_frame_not_verified', 'current_100x_focus_and_travel_limits_not_verified',
                'run_scoped_acquisition_authorization_required']
    result = {'motion_ready': False, 'target_magnification': 100,
              'target_stage_offset_um': None, 'blockers': blockers}
    if profile_path is None:
        result.update(calibration_status='missing_measured_100x_profile', profile=None,
                      parcentric_status='not_measured')
        blockers.extend(['missing_measured_100x_profile', 'missing_measured_objective_offset'])
        return result
    path = Path(profile_path).resolve(strict=True)
    profile = _read(path)
    if (profile.get('schema') != 1 or profile.get('objective', {}).get('magnification') != 100
            or profile.get('context', {}).get('sample_id') != sample_id):
        raise SafetyError('Profile must describe measured100× optics for this exact sample.')
    transform = CameraTransform(**profile['camera_transform'])
    if not transform.calibrated:
        raise SafetyError('100× profile has no measured camera transform.')
    result.update(calibration_status='profile_present_requires_live_validation',
                  profile={'path': str(path), 'sha256': _digest(path), 'objective': profile['objective']},
                  parcentric_status='unverified')
    # No current persisted offset-evidence contract exists. Do not interpret a
    # guessed zero, historical profile or a free-form offset as calibration.
    blockers.append('missing_verified_objective_offset_contract')
    if not profile.get('verification_capture'):
        blockers.append('profile_capture_evidence_missing')
    return result


def export_graphene_revisit_queue(project, run_id, output, *, candidate_ids=None,
                                 max_candidates=25, profile_path=None):
    """Export selected committed observations without acquiring or changing them.

    Default selection is the ranked catalog snapshot. Explicit IDs may also
    select uncertain observations retained in a committed field shard. Every
    candidate is checked against its exact saved instance mask and source
    geometry; uncommitted work never becomes a target.
    """
    if not isinstance(run_id, str) or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_-]{0,95}', run_id):
        raise ValueError('A confined simple run ID is required.')
    if type(max_candidates) is not int or not 1 <= max_candidates <= 100:
        raise ValueError('Export1..100 targets per bounded offline queue.')
    project = Path(project).resolve(strict=True)
    run = _child(project, 'data/' + run_id)
    request_path = run / 'scan-request.json'
    request = _read(request_path)
    settings = request.get('settings', {})
    sample_id = settings.get('sample_id')
    if (request.get('schema') != 1 or request.get('id') != run_id or not sample_id
            or settings.get('material') != 'graphene' or settings.get('oxide_nm') != 90):
        raise SafetyError('Queue export requires an identified graphene90nm scan request.')
    request_sha = _digest(request_path)
    marker = _read(project / '.flakefinder-project.json')
    project_id = marker.get('project_id', marker.get('id'))
    if not project_id:
        raise SafetyError('Managed project identity is missing.')
    batch = _read(run / 'batch.json')
    layout = _read(run / 'layout.json')
    coordinate = batch.get('coordinate_frame_id')
    if not coordinate or layout.get('coordinate_frame_id') != coordinate:
        raise SafetyError('Campaign coordinate-frame evidence disagrees or is missing.')
    analysis = _child(run, 'analysis-graphene-incremental')
    plan = _read(analysis / 'plan.json')
    identity = {'project_id': project_id, 'run_id': run_id, 'scan_request_sha256': request_sha}
    if plan.get('identity') != identity or plan.get('coordinate_frame_id') != coordinate:
        raise SafetyError('Analysis plan/request/coordinate identity mismatch.')
    ledger = _read(analysis / 'ledger.json')
    catalog_path = analysis / 'catalog.json'
    catalog = _read(catalog_path)
    if (catalog.get('coordinate_frame_id') != coordinate
            or any(catalog.get(key) != value for key, value in identity.items())):
        raise SafetyError('Catalog belongs to another run or coordinate frame.')
    catalog_rows = {row['id']: row for row in catalog.get('rows', [])}
    if len(catalog_rows) != len(catalog.get('rows', [])):
        raise SafetyError('Duplicate catalog target identities.')
    explicit = candidate_ids is not None
    ids = list(candidate_ids) if explicit else list(catalog_rows)[:max_candidates]
    if not ids or len(ids) != len(set(ids)) or len(ids) > max_candidates:
        raise ValueError('Select a nonempty unique bounded list of candidate IDs.')
    fields = {}
    rows = []
    label_field = None
    active_labels = None
    for candidate_id in ids:
        if not isinstance(candidate_id, str) or not re.fullmatch(r'chip[0-9]+/tile_[0-9]+/[1-9][0-9]*', candidate_id):
            raise SafetyError('Unexpected candidate identity: ' + str(candidate_id))
        chip_id, tile_id, label_text = candidate_id.split('/')
        field = chip_id + '/' + tile_id
        if field not in fields:
            commit = ledger.get(field)
            if not isinstance(commit, dict):
                raise SafetyError('Candidate field is not committed in the analysis ledger.')
            field_path = _child(analysis, commit['field_json'])
            field_rel = field_path.relative_to(analysis).as_posix()
            _verify(field_path, commit.get('hashes', {}).get(field_rel))
            payload = _read(field_path)
            if payload.get('field') != field:
                raise SafetyError('Committed analysis field identity mismatch.')
            chip = _child(run, chip_id)
            manifest = _read(chip / 'manifest.json')
            record = manifest.get('completed', {}).get(tile_id)
            if not isinstance(record, dict) or _canonical(record) != commit.get('source_record_sha256'):
                raise SafetyError('Source acquisition record differs from the committed analysis.')
            if _geometry(manifest) != commit.get('geometry_sha256'):
                raise SafetyError('Source coordinate geometry/context changed.')
            if (manifest.get('context', {}).get('sample_id') != sample_id
                    or manifest['context'].get('material') != 'graphene' or manifest['context'].get('oxide_nm') != 90):
                raise SafetyError('Source field belongs to another specimen or optical substrate context.')
            if payload.get('source_record_sha256') != _canonical(record) or payload.get('source_transform') != manifest['transform']:
                raise SafetyError('Analysis source geometry is inconsistent.')
            required = ['tiles/' + tile_id + '.tiff', 'tiles/' + tile_id + '.json',
                        'boundary-labels/' + tile_id + '.npz']
            hashes = {relative: record.get('hashes', {}).get(relative) for relative in required}
            if payload.get('source_hashes') != hashes:
                raise SafetyError('Analysis source hashes disagree with the acquisition commit.')
            for relative, expected in hashes.items():
                _verify(_child(chip, relative), expected)
            sidecar = _read(chip / required[1])
            transform = CameraTransform(**manifest['transform'])
            if not transform.calibrated or sidecar['position'] != record['position']:
                raise SafetyError('Source position or measured survey transform is invalid.')
            if not np.allclose(sidecar['pixel_size_um'], transform.pixel_size_um, rtol=0, atol=1e-9):
                raise SafetyError('Source pixel scale differs from the measured transform.')
            shape = tuple(manifest['camera_shape'][:2])
            if len(shape) != 2 or any(type(n) is not int or n <= 0 for n in shape) or math.prod(shape) > 6_000_000:
                raise SafetyError('Unexpected source camera dimensions.')
            from PIL import Image
            with Image.open(chip / required[0]) as image:
                if (image.format != 'TIFF' or (image.height, image.width) != shape
                        or image.tag_v2.get(277) != 3 or tuple(image.tag_v2.get(258, ())) != (16, 16, 16)):
                    raise SafetyError('Raw TIFF dimensions disagree with source geometry.')
            mask_path = field_path.parent / 'masks.npz'
            mask_hash = _verify(mask_path, commit.get('hashes', {}).get(mask_path.relative_to(analysis).as_posix()))
            if payload.get('mask_sha256') != mask_hash:
                raise SafetyError('Saved mask checksum disagrees with analysis evidence.')
            fields[field] = {'chip': chip, 'transform_dict': manifest['transform'], 'commit': commit,
                             'selected_rows': [row for row in payload.get('rows', []) if row.get('id') in ids],
                             'record': record, 'shape': shape,
                             'field_path': field_path, 'mask_path': mask_path, 'hashes': hashes,
                             'transform': transform, 'field_sha256': _digest(field_path), 'mask_sha256': mask_hash}
        verified = fields[field]
        if label_field != field:
            active_labels = None  # At most one full source label grid is resident.
            active_labels = _labels(verified['mask_path'], verified['shape'])
            label_field = field
        matching = [row for row in verified['selected_rows'] if row.get('id') == candidate_id]
        if len(matching) != 1:
            raise SafetyError('Candidate is missing or duplicated in its committed source shard.')
        row = matching[0]
        source_transform = verified['transform_dict']
        if (row.get('instance_label') != int(label_text) or row.get('field') != field
                or row.get('source_shape_hw') != list(verified['shape'])
                or row.get('source_transform_sha256') != _canonical(source_transform)
                or row.get('source_tiff_sha256') != verified['hashes']['tiles/' + tile_id + '.tiff']
                or row.get('source_position_um') != verified['record']['position']):
            raise SafetyError('Candidate provenance differs from its verified source.')
        if candidate_id in catalog_rows:
            for key in ('stage_xy_um', 'revisit_source_pixel_xy', 'source_position_um',
                        'source_transform_sha256', 'source_tiff_sha256', 'source_shape_hw'):
                if catalog_rows[candidate_id].get(key) != row.get(key):
                    raise SafetyError('Catalog coordinates differ from the committed observation: ' + key)
        pixel = np.asarray(row.get('revisit_source_pixel_xy'), dtype=float)
        h, w = verified['shape']
        if pixel.shape != (2,) or not np.isfinite(pixel).all() or not (0 <= pixel[0] <= w-1 and 0 <= pixel[1] <= h-1):
            raise SafetyError('Candidate source pixel is invalid.')
        for x in {int(math.floor(pixel[0])), int(math.ceil(pixel[0]))}:
            for y in {int(math.floor(pixel[1])), int(math.ceil(pixel[1]))}:
                if active_labels[y, x] != int(label_text):
                    raise SafetyError('Candidate point is outside its exact saved instance mask or inside a hole.')
        position = verified['record']['position']
        recomputed = np.asarray(source_transform['matrix']) @ (pixel - [(w-1)/2, (h-1)/2]) + [position['x'], position['y']]
        saved = np.asarray(row.get('stage_xy_um'), dtype=float)
        if saved.shape != (2,) or not np.isfinite(saved).all() or not np.allclose(saved, recomputed, rtol=0, atol=.001):
            raise SafetyError('Candidate stage coordinates do not follow the measured source transform.')
        rows.append({'id': candidate_id, 'source_field': field, 'coordinate_frame_id': coordinate,
                     'source_image': (verified['chip'] / 'tiles' / (tile_id + '.tiff')).relative_to(project).as_posix(),
                     'source_sha256': verified['hashes']['tiles/' + tile_id + '.tiff'],
                     'source_position_um': position, 'source_pixel_xy': pixel.tolist(),
                     'source_stage_xy_um': recomputed.tolist(), 'target_stage_xy_um': None,
                     'source_transform': source_transform, 'source_transform_sha256': _canonical(source_transform),
                     'source_record_sha256': verified['commit']['source_record_sha256'],
                     'source_geometry_sha256': verified['commit']['geometry_sha256'],
                     'source_mask_sha256': verified['mask_sha256'], 'source_instance_label': int(label_text),
                     'analysis_field_json': verified['field_path'].relative_to(project).as_posix(),
                     'analysis_field_sha256': verified['field_sha256'],
                     'source_row_sha256': _canonical(row), 'area_um2': row.get('area_um2'),
                     'rank': row.get('rank'), 'disposition': row.get('disposition'),
                     'selection': 'explicit_review_selection' if explicit else 'ranked_catalog_snapshot',
                     'focus_evidence': row.get('focus_evidence'), 'material_identity_confirmed': False,
                     'layer_number': None, 'status': 'source_verified_target_calibration_missing'})
    # Ongoing acquisition may append other fields. Pin/recheck only immutable
    # selected commits and coordinate identity, never require the whole run to stop.
    if (_digest(request_path) != request_sha or _read(run / 'batch.json').get('coordinate_frame_id') != coordinate
            or _read(run / 'layout.json').get('coordinate_frame_id') != coordinate):
        raise SafetyError('Request or coordinate identity changed while exporting.')
    current_ledger = _read(analysis / 'ledger.json')
    for field, verified in fields.items():
        manifest = _read(verified['chip'] / 'manifest.json')
        if (current_ledger.get(field) != verified['commit'] or _geometry(manifest) != verified['commit']['geometry_sha256']
                or _canonical(manifest['completed'][field.split('/')[1]]) != verified['commit']['source_record_sha256']):
            raise SafetyError('Selected committed source changed while exporting.')
    readiness = assess_100x_readiness(sample_id, profile_path)
    result = {'schema': 1, 'kind': 'offline_reviewable_revisit_queue', 'created_at': time.time(),
              **identity, 'sample_id': sample_id, 'coordinate_frame_id': coordinate,
              'catalog_snapshot_sha256': _canonical(catalog), 'candidate_count': len(rows),
              'readiness': readiness, 'motion_authorized': False, 'hardware_accessed': False,
              'coordinate_statement': 'Source XY is valid only in the unchanged survey coordinate frame; no objective offset is assumed.',
              'candidates': rows}
    from .storage import resolve_output_path
    output = resolve_output_path(output)
    if output.exists():
        raise SafetyError('Choose a new queue output folder; exports are immutable.')
    output.mkdir(parents=True)
    atomic_json(output / 'revisit-queue.json', result)
    with (output / 'targets.csv').open('x', encoding='utf-8', newline='') as stream:
        writer = csv.writer(stream)
        writer.writerow(['candidate_id', 'area_um2', 'source_x_um', 'source_y_um', 'source_pixel_x', 'source_pixel_y',
                         'disposition', 'target_x_um', 'target_y_um', 'status'])
        for row in rows:
            writer.writerow([row['id'], row['area_um2'], *row['source_stage_xy_um'], *row['source_pixel_xy'],
                             row['disposition'], '', '', row['status']])
    (output / 'READINESS.txt').write_text(
        'OFFLINE REVIEW QUEUE — NOT A MOTION PLAN\n\n'
        + str(len(rows)) + ' source-verified observations. No hardware accessed.\n'
        + 'Coordinate frame: ' + coordinate + '\n'
        + 'Outstanding requirements:\n' + '\n'.join('- ' + item for item in readiness['blockers'])
        + '\n\nSource coordinates cannot establish that the holder has not moved or XY has not been re-zeroed.\n'
        + 'Use current fiducial registration and a measured100x objective handoff before travel.\n'
        + 'No material identity or layer number is confirmed by this export.\n', encoding='utf-8')
    return result
