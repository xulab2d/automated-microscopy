"""Saved-source geometry and operator XY registration; importing never connects hardware."""
from pathlib import Path
import hashlib
import itertools
import json
import math
import re

import numpy as np
from .model import SafetyError


def digest(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def read(path):
    return json.loads(Path(path).read_text(encoding='utf-8-sig'))


def inside(root, relative):
    root = Path(root).resolve()
    path = (root / relative).resolve(strict=True)
    if not path.is_relative_to(root):
        raise SafetyError('Revisit source escapes its managed project.')
    return path


def corners_from_plan(plan):
    """Use physical-line offsets, never the expanded acquisition envelope."""
    boundary = plan.get('boundary_estimate', {})
    lines = boundary.get('lines', [])
    if len(lines) < 3:
        return []
    normals = np.asarray([line['normal'] for line in lines], float)
    offsets = np.asarray([line['offset_um'] for line in lines], float)
    if not np.isfinite(normals).all() or not np.isfinite(offsets).all():
        raise SafetyError('Invalid saved chip boundary.')
    vertices = []
    for i, j in itertools.combinations(range(len(lines)), 2):
        matrix = normals[[i, j]]
        if abs(np.linalg.det(matrix)) < 1e-6:
            continue
        point = np.linalg.solve(matrix, offsets[[i, j]])
        if np.all(normals @ point <= offsets + 1e-5):
            if not any(np.linalg.norm(point - old) < .01 for old in vertices):
                vertices.append(point)
    if len(vertices) < 3:
        raise SafetyError('Saved sides do not bound a chip polygon.')
    centre = np.mean(vertices, axis=0)
    vertices.sort(key=lambda p: math.atan2(*(p-centre)[::-1]))
    uncertainty = float(boundary.get('uncertainty_um', 0))
    if not math.isfinite(uncertainty) or uncertainty <= 0:
        raise SafetyError('Inferred corners lack a spatial uncertainty allowance.')
    return [dict(id=f'corner_{i}', xy_um=p.tolist(), kind='inferred_line_intersection',
                 uncertainty_um=uncertainty, qualification='Coarse aid; corner not directly imaged. This allowance is not a confidence bound.')
            for i, p in enumerate(vertices)]


def source_metadata(project, run_id, observation):
    """Resolve one committed region without loading all review rows or raw TIFFs."""
    from .graphene_review import GrapheneReview
    if not re.fullmatch(r'chip\d+/tile_\d+/[1-9]\d*', str(observation)):
        raise ValueError('Choose one saved observation, not a duplicate group.')
    project = Path(project).resolve()
    chip, tile, _ = observation.split('/')
    viewer = GrapheneReview(project)
    run, request, request_hash = viewer._run(run_id)
    offline = viewer._offline(run, run_id, request_hash, chip)
    if offline is None:
        raise SafetyError('Select a pinned offline graphene analysis before revisit.')
    output, _, ledger, stale, analysis = offline
    if stale:
        raise SafetyError('Selected analysis is stale or temporarily unavailable.')
    field = chip + '/' + tile
    commit = ledger[field]
    payload = viewer._shard(output, field, commit)
    row = next((r for r in payload['rows'] if r['id'] == observation), None)
    if row is None or row.get('revisit_point_inside_observed_reduced_mask') is not True:
        raise SafetyError('Observation lacks a source-pixel revisit point.')
    plan_path = run/chip/'coverage-plan.json'
    plan = read(plan_path)
    sidecar_path = run/chip/'tiles'/(tile+'.json')
    expected = commit['source_hashes'].get('tiles/'+tile+'.json')
    if not expected or digest(sidecar_path) != expected:
        raise SafetyError('Source position metadata differs from committed acquisition.')
    sidecar = read(sidecar_path)
    point = pixel_to_stage(row['revisit_source_pixel_xy'], row['source_shape_hw'],
                           sidecar['position'], payload['source_transform'])
    if not np.allclose(point, row['stage_xy_um'], rtol=0, atol=1e-6):
        raise SafetyError('Saved region coordinates disagree with source pixel transform.')
    if payload['source_transform'] != plan['transform']:
        raise SafetyError('Source camera transform differs from chip plan.')
    manifest_path = run/chip/'manifest.json'
    manifest = read(manifest_path)
    preview_path = run/chip/'tiles'/(tile+'.jpg')
    paths = [run/'scan-request.json', manifest_path, plan_path, sidecar_path, preview_path,
             run/analysis['selection_file'], output/'plan.json', output/'model.json', output/commit['field_json']]
    pins = {p.relative_to(project).as_posix(): digest(p) for p in paths}
    from urllib.parse import quote
    fields = []
    shape = list(plan['camera_shape'][:2])
    for name, record in manifest['completed'].items():
        if not re.fullmatch(r'tile_\d+', name):
            continue
        position = record.get('position', {})
        fields.append(dict(field=chip+'/'+name, shape_hw=shape,
            stage_xy_um=[position.get('x'), position.get('y')],
            preview_url='/files/'+quote((run/chip/'tiles'/(name+'.jpg')).relative_to(project).as_posix(), safe='/')))
    return dict(schema=1, run_id=run_id, chip_id=chip, observation=observation,
        target=dict(stage_xy_um=point, revisit_source_pixel_xy=row['revisit_source_pixel_xy'],
                    preview_url='/files/'+quote((run/chip/'tiles'/(tile+'.jpg')).relative_to(project).as_posix(), safe='/')),
        corners=corners_from_plan(plan), fields=fields, source_pins=pins,
        transform=plan['transform'], source_context=request['settings'],
        required_objective=sidecar['metadata']['objective'],
        default_limits=None, limits_policy='fresh_controller_ranges',
        qualification='Operator registration predicts XY only. Verify the flake visually; inferred corners are coarse. No historical focus height is used.')


def verify_source(project, metadata):
    for relative, expected in metadata['source_pins'].items():
        if digest(inside(project, relative)) != expected:
            raise SafetyError('Pinned revisit source changed: '+relative)


def pixel_to_stage(pixel, shape, position, transform):
    point = np.asarray(pixel, float)
    h, w = shape
    if point.shape != (2,) or not np.isfinite(point).all() or not (0 <= point[0] < w and 0 <= point[1] < h):
        raise SafetyError('Source point must lie inside the saved native image.')
    if transform.get('calibrated') is not True:
        raise SafetyError('Source transform is not calibrated.')
    matrix = np.asarray(transform['matrix'], float)
    if matrix.shape != (2, 2) or not np.isfinite(matrix).all() or abs(np.linalg.det(matrix)) < 1e-12:
        raise SafetyError('Invalid camera transform.')
    result = matrix @ (point-[(w-1)/2, (h-1)/2]) + [position['x'], position['y']]
    if not np.isfinite(result).all():
        raise SafetyError('Saved source position is not finite.')
    return result.tolist()


def source_anchor(project, metadata, *, corner_id=None, source_observation=None,
                  source_field=None, source_pixel_xy=None):
    verify_source(project, metadata)
    if sum(v is not None for v in (corner_id, source_observation, source_field)) != 1:
        raise ValueError('Choose exactly one inferred corner, observation, or source image point.')
    if corner_id is not None:
        corner = next((c for c in metadata['corners'] if c['id'] == corner_id), None)
        if corner is None:
            raise ValueError('Unknown source corner.')
        return {**corner, 'source_xy_um': corner['xy_um'], 'source_pins': metadata['source_pins']}
    if source_observation is not None:
        other = source_metadata(project, metadata['run_id'], source_observation)
        if other['chip_id'] != metadata['chip_id']:
            raise SafetyError('Registration anchors must belong to the selected chip.')
        return dict(id=source_observation, source_xy_um=other['target']['stage_xy_um'],
                    source_pins=other['source_pins'], uncertainty_um=5., kind='operator_matched_observed_feature')
    if not re.fullmatch(re.escape(metadata['chip_id'])+r'/tile_\d+', str(source_field)):
        raise SafetyError('Source image must belong to the selected chip.')
    run = inside(project, 'data/'+metadata['run_id'])
    chip, tile = source_field.split('/')
    manifest = read(run/chip/'manifest.json')
    record = manifest['completed'][tile]
    path = run/chip/'tiles'/(tile+'.json')
    expected = record['hashes'].get('tiles/'+tile+'.json')
    if not expected or digest(path) != expected:
        raise SafetyError('Selected source image position metadata is unavailable or changed.')
    saved = read(path)
    preview = run/chip/'tiles'/(tile+'.jpg')
    if not preview.is_file():
        raise SafetyError('Selected source image preview is unavailable.')
    plan = read(run/chip/'coverage-plan.json')
    point = pixel_to_stage(source_pixel_xy, plan['camera_shape'][:2], saved['position'], plan['transform'])
    return dict(id=source_field+':'+json.dumps(source_pixel_xy), kind='operator_matched_image_point',
        source_xy_um=point, source_pixel_xy=source_pixel_xy, uncertainty_um=5.,
        preview_provenance='Display derivative pinned at registration, not a scientific raw image checksum.',
        source_pins={**metadata['source_pins'], path.relative_to(Path(project).resolve()).as_posix(): expected,
                     preview.relative_to(Path(project).resolve()).as_posix():digest(preview)})


def fit_registration(anchors, *, orientation_unchanged=False):
    if type(orientation_unchanged) is not bool:
        raise ValueError('Orientation confirmation must be boolean.')
    if not 1 <= len(anchors) <= 2:
        raise SafetyError('Record one or two distinct source anchors.')
    source = np.asarray([a['source_xy_um'] for a in anchors], float)
    current = np.asarray([[a['position_um']['x'], a['position_um']['y']] for a in anchors], float)
    if not np.isfinite(source).all() or not np.isfinite(current).all():
        raise SafetyError('Anchor coordinates must be finite.')
    uncertainty = max(float(a['uncertainty_um']) for a in anchors)
    rotation = np.eye(2)
    mismatch = 0.; angular = 0.
    if len(anchors) == 1:
        if orientation_unchanged is not True:
            raise SafetyError('A single anchor requires explicit unchanged orientation confirmation.')
    else:
        a, b = source[1]-source[0], current[1]-current[0]
        lengths = [np.linalg.norm(a), np.linalg.norm(b)]
        tolerance = 2*sum(float(v['uncertainty_um']) for v in anchors)
        if min(lengths) <= 4*tolerance:
            raise SafetyError('Anchor separation is too small relative to localization uncertainty.')
        mismatch = abs(lengths[1]-lengths[0])
        if mismatch > tolerance:
            raise SafetyError('Anchor distances do not support a rigid same-chip registration; check landmark correspondence.')
        angle = math.atan2(b[1], b[0])-math.atan2(a[1], a[0])
        rotation = np.array([[math.cos(angle), -math.sin(angle)], [math.sin(angle), math.cos(angle)]])
        angular = math.asin(min(1., tolerance/min(lengths)))
    translation = current.mean(axis=0)-rotation@source.mean(axis=0)
    return dict(rotation=rotation.tolist(), translation_um=translation.tolist(),
        mode='two_point_rigid' if len(anchors)==2 else 'translation_orientation_asserted',
        orientation_unchanged=orientation_unchanged, scale_fitted=False,
        anchor_distance_mismatch_um=float(mismatch), source_origin_um=source.mean(axis=0).tolist(),
        localization_allowance_um=uncertainty+mismatch/2, angular_allowance_rad=angular,
        qualification='Operator correspondence; no image-based identity verification or new focus plane.')


def transform_target(registration, source_xy):
    point = np.asarray(source_xy, float)
    target = np.asarray(registration['rotation'])@point+registration['translation_um']
    uncertainty = registration.get('localization_allowance_um', 0.) + np.linalg.norm(point-registration.get('source_origin_um', point))*registration.get('angular_allowance_rad', 0.)
    return target.tolist(), float(uncertainty)
