"""Small read-only dashboard projection of acquisition-owned saved evidence.

No microscope imports, image decoding, raw arrays, or writes. File snapshots can
disagree briefly during acquisition; missing/partial files are reported, never
interpreted as successful acquisition. Positions describe saved captures only.
"""
from datetime import datetime
import json
import math
from pathlib import Path
import time


def _number(value):
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def _xy(value):
    if isinstance(value, dict):
        value = [value.get('x'), value.get('y')]
    return list(value[:2]) if isinstance(value, (list, tuple)) and len(value) >= 2 and all(_number(v) for v in value[:2]) else None


def _sample(values, limit):
    if len(values) <= limit:
        return values
    return [values[round(i * (len(values) - 1) / (limit - 1))] for i in range(limit)]


def build_live_progress(folder):
    """Return JSON-compatible progress (schema 1), normally below 300 kB.

    All paths exposed to the viewer are confined, root-relative paths. ``at``
    on events carries ``time_kind``: capture time and status-file save time are
    intentionally distinct. Collections expose counts before display sampling.
    """
    root = Path(folder).resolve()
    warnings, events, images = [], [], []

    def inside(path, base=root):
        try:
            path = Path(path)
            path = (path if path.is_absolute() else base / path).resolve()
            return path if path.is_relative_to(root) else None
        except (OSError, ValueError, TypeError):
            return None

    def saved_at(path):
        try:
            return path.stat().st_mtime if path else None
        except OSError:
            return None

    def read(path):
        path = inside(path)
        if path is None:
            warnings.append('Ignored a path outside the campaign folder.')
            return {}
        try:
            if not path.exists():
                return {}
            if path.stat().st_size > 32 * 1024 * 1024:
                warnings.append('Oversized status file omitted: ' + path.name)
                return {}
            value = json.loads(path.read_text(encoding='utf-8'))
            if not isinstance(value, dict):
                raise ValueError('Expected a status object')
            return value
        except (OSError, ValueError, UnicodeError):
            warnings.append('Snapshot temporarily unreadable: ' + path.relative_to(root).as_posix())
            return {}

    def event(at, kind, label, time_kind='recorded_event'):
        if _number(at):
            events.append(dict(at=at, kind=kind, label=label, time_kind=time_kind))

    def preview(path, label):
        path = inside(path)
        if path is None:
            return
        jpg = path.with_suffix('.jpg')
        if not jpg.is_file():
            return
        sidecar = read(path.with_suffix('.json'))
        metadata = sidecar.get('metadata', {})
        # Focus ROI sidecars describe an effective specimen point; retain the
        # physical stage readback if it is explicitly present.
        position = metadata.get('physical_stage_position') or metadata.get('physical_position_um') or metadata.get('stage_position_um') or sidecar.get('position')
        item = dict(relative_path=jpg.relative_to(root).as_posix(), label=label,
                    captured_at=sidecar.get('acquired_at'), saved_at=saved_at(jpg),
                    position_um=position, position_kind='saved_capture',
                    storage_guard=metadata.get('capture_storage_guard'))
        images.append(item)

    batch = read(root / 'batch.json')
    now = time.time()
    phase = str(batch.get('phase') or batch.get('state') or 'waiting')
    current = batch.get('current_chip')
    chips = batch.get('chip_states', {})
    if not isinstance(chips, dict):
        chips = {}
    chip_id = current if current in chips else next(reversed(chips), None)
    chip_root = inside(chip_id) if chip_id else None
    chip_info = chips.get(chip_id, {})
    manifest = read(chip_root / 'manifest.json') if chip_root else {}
    plan = read(chip_root / 'coverage-plan.json') if chip_root else {}
    completed = manifest.get('completed', {})
    if not isinstance(completed, dict):
        completed = {}

    attempts = []
    discovery_folder = None
    for item in batch.get('boundary_attempts', [])[-128:]:
        if not isinstance(item, dict):
            continue
        attempts.append({k: item.get(k) for k in ('id', 'state', 'seed_xy_um', 'chip_id', 'reason', 'error') if k in item})
        if item.get('folder'):
            candidate = inside(item['folder'])
            if candidate and (candidate / 'discovery.json').is_file():
                discovery_folder = candidate
    if current and chip_info.get('discovery_folder'):
        discovery_folder = inside(chip_info['discovery_folder']) or discovery_folder
    if discovery_folder is None:
        paths = list((root / 'discovery').glob('*/discovery.json'))
        if paths:
            discovery_folder = max(paths, key=lambda p: saved_at(p) or 0).parent
    discovery = read(discovery_folder / 'discovery.json') if discovery_folder else {}
    records = discovery.get('records', [])
    outline = discovery.get('estimated_outline', {})

    seeds = []
    for item in batch.get('seed_records', []):
        xy = _xy(item.get('position_um') or item.get('requested_xy_um'))
        if xy:
            seeds.append(dict(x=xy[0], y=xy[1], status=item.get('classification'),
                              attempt_id=item.get('attempt_id'), index=item.get('index')))
    if batch.get('seed_records'):
        last = batch['seed_records'][-1]
        if last.get('image'):
            preview(root / last['image'], 'Latest saved search field')
    edges = []
    for item in records[-80:]:
        xy = _xy(item.get('position_um') or item.get('requested_xy_um'))
        if not xy:
            continue
        edge = item.get('edge') or {}
        points = [_xy(p) for p in edge.get('points_um', [])]
        edges.append(dict(x=xy[0], y=xy[1], status=item.get('classification'), phase=item.get('phase'),
                          name=item.get('name'), fractions=item.get('fractions'),
                          normal=edge.get('normal'), offset_um=edge.get('offset_um'),
                          line=dict(normal=edge.get('normal'), offset_um=edge.get('offset_um')) if edge else None,
                          points_um=_sample([p for p in points if p], 24)))
    for item in records[-8:]:
        if discovery_folder and item.get('image'):
            path = inside(item['image'], discovery_folder)
            if path:
                preview(path, 'Boundary probe: ' + str(item.get('phase', 'saved')))
                side = images[-1] if images and images[-1]['relative_path'] == path.with_suffix('.jpg').relative_to(root).as_posix() else {}
                event(side.get('captured_at'), 'boundary', str(item.get('name')) + ' · ' + str(item.get('classification')), 'capture')

    shape = plan.get('camera_shape') or manifest.get('camera_shape') or []
    matrix = (plan.get('transform') or manifest.get('transform') or {}).get('matrix')
    width = height = None
    footprint = None
    if len(shape) >= 2 and matrix and len(matrix) == 2:
        h, w = shape[:2]
        footprint = [[matrix[0][0]*x + matrix[0][1]*y, matrix[1][0]*x + matrix[1][1]*y]
                     for x, y in [(-w/2, -h/2), (w/2, -h/2), (w/2, h/2), (-w/2, h/2)]]
        width = abs(matrix[0][0])*w + abs(matrix[0][1])*h
        height = abs(matrix[1][0])*w + abs(matrix[1][1])*h
    planned = [dict(x=p[0], y=p[1], width_um=width, height_um=height) for p in plan.get('tile_centres_um', []) if _xy(p)]
    acquired = []
    for name, item in completed.items():
        xy = _xy(item.get('position'))
        if xy:
            acquired.append(dict(x=xy[0], y=xy[1], width_um=width, height_um=height,
                                 tile_id=name, focus_limited=item.get('focus_limited')))
    if chip_root and completed:
        # Camera sequence counters may reset for every vendor job. The saved
        # preview timestamp identifies the latest committed field across jobs.
        last_tile = max(completed, key=lambda k: saved_at(chip_root / 'tiles' / (k + '.jpg')) or 0)
        preview(chip_root / 'tiles' / (last_tile + '.tiff'), 'Latest saved raster field · ' + chip_id)

    focus = dict(status='unavailable', samples=[], best_z_um=None)
    surface = read(chip_root / 'focus-surface' / 'focus-surface.json') if chip_root else {}
    anchor = surface.get('anchors', [])[-1] if surface.get('anchors') else {}
    focus_folder = None
    focus_data = {}
    if current and surface:
        focus.update(status=anchor.get('status', surface.get('state')), source='chip_focus_surface',
                     anchor_index=anchor.get('index'), verified_points_um=surface.get('verified_points_um', []),
                     planned_anchors_um=surface.get('anchor_centres_um', []), chip_id=current)
        if surface.get('attempt_folder') and anchor:
            focus_folder = inside(Path(surface['attempt_folder']) / ('anchor_%03d' % anchor.get('index', 0)), chip_root / 'focus-surface')
        if focus_folder:
            candidates = [focus_folder / 'local' / 'focus-curve.json', focus_folder / 'local' / 'quick-focus.json',
                          focus_folder / 'local' / 'fallback' / 'focus-curve.json', focus_folder / 'recovery' / 'recovery.json',
                          focus_folder / 'recovery' / 'verification' / 'focus-curve.json']
            candidates = [p for p in candidates if p.is_file()]
            if candidates:
                focus_path = max(candidates, key=lambda p: saved_at(p) or 0)
                focus_data = read(focus_path)
                focus['saved_at'] = saved_at(focus_path)
            if not focus_data:
                focus_data = anchor.get('local_result') or anchor.get('recovery_result') or {}
            for relative in ('selection/full-field.tiff', 'anchor.tiff', 'recovery/best_observed.tiff', 'recovery/verified_focus.tiff'):
                preview(focus_folder / relative, 'Saved focus evidence · ' + str(anchor.get('index', '')))
        focus['latest_sample'] = chip_info.get('focus_progress')
    elif discovery.get('edge_focus_attempts'):
        attempt = discovery['edge_focus_attempts'][-1]
        focus.update(status=attempt.get('state'), source='boundary_edge_focus')
        if attempt.get('folder'):
            focus_folder = inside(attempt['folder'], discovery_folder)
        if focus_folder:
            focus_data = read(focus_folder / 'recovery.json') or attempt.get('result') or {}
            focus['saved_at'] = saved_at(focus_folder / 'recovery.json')
            for relative in ('selection/full-field.tiff', 'best_observed.tiff', 'verified_focus.tiff'):
                preview(focus_folder / relative, 'Saved boundary autofocus evidence')
        event(attempt.get('started_at'), 'focus', 'Boundary autofocus started')
        event(attempt.get('finished_at'), 'focus', 'Boundary autofocus ' + str(attempt.get('state')))
    samples = [{k: s[k] for k in ('z_um', 'score', 'phase', 'focus_metric') if k in s}
               for s in (focus_data.get('samples') or (focus_data.get('result') or {}).get('samples') or focus_data.get('quick_samples') or []) if _number(s.get('z_um')) and _number(s.get('score'))]
    focus['sample_count'] = len(samples)
    focus['samples'] = _sample(samples, 400)
    focus['samples_sampled'] = len(samples) > 400
    if samples:
        focus['best_z_um'] = max(samples, key=lambda s: s['score'])['z_um']
        focus['best_is_verified'] = focus_data.get('success') is True or focus_data.get('state') == 'focused'
    focus['qualification'] = 'Raw saved focus scores; scores across different image content are not comparable.'

    images.sort(key=lambda v: v.get('captured_at') if _number(v.get('captured_at')) else v.get('saved_at') or 0)
    latest = images[-1] if images else None
    position = latest.get('position_um') if latest else None
    if _xy(position) and not isinstance(position, dict):
        position = dict(zip(('x', 'y', 'z'), position))
    storage = dict(source='unavailable', free_bytes=None, reserve_bytes=batch.get('storage_options', {}).get('reserve_bytes'))
    for image in reversed(images):
        guard = image.get('storage_guard')
        if guard:
            storage.update(free_bytes=guard.get('free_bytes_before_capture'), reserve_bytes=guard.get('reserve_bytes'),
                           observed_at=image.get('captured_at'), source='saved_pre_capture_storage_guard')
            break
    storage['qualification'] = 'Last saved storage check, not a current disk reading.'
    if latest:
        latest = {k: v for k, v in latest.items() if k not in ('storage_guard', 'position_um')}

    guard = read(root / 'deadline-guard.json')
    deadline_at = None
    try:
        deadline_at = datetime.fromisoformat(guard['deadline_utc']).timestamp()
    except (KeyError, ValueError, TypeError):
        pass
    for key in ('resume_attempts', 'continuation_attempts', 'budget_transitions', 'algorithm_transitions'):
        for item in batch.get(key, [])[-5:]:
            event(item.get('at') or item.get('started_at') or item.get('updated_at'), key, key.replace('_', ' '))
    event(batch.get('started_at'), 'campaign', 'Campaign started')
    event(discovery.get('started_at'), 'boundary', 'Boundary discovery started')
    event(manifest.get('updated_at'), 'raster', 'Raster checkpoint · ' + str(len(completed)) + ' fields saved', 'status_saved')
    event(guard.get('started_at'), 'deadline', 'Deadline guard started')
    events.sort(key=lambda e: e['at'])
    step = 'focus' if 'focus' in phase else 'raster' if phase == 'tile' or any(s in phase for s in ('raster', 'scan_chip', 'acquiring')) else 'boundary' if any(s in phase for s in ('edge', 'boundary', 'discover')) else 'analysis' if 'analy' in phase else 'search'
    output = dict(schema=1, available=bool(batch), source='saved_files_only', phase=phase, current_step=step,
        state=batch.get('state'), updated_at=batch.get('updated_at'), viewer_read_at=now,
        coordinate_frame_id=batch.get('coordinate_frame_id'), current_chip=current, current_image=latest,
        stop_requested=(root / 'STOP').exists(), attempts=attempts,
        stage_map=dict(bounds_um=batch.get('search_bounds_um'), seeds=_sample(seeds, 1000), edges=edges,
            outline_um=outline.get('polygon_um', []), outline_status=outline.get('status'),
            outline_uncertainty_um=outline.get('uncertainty_um'), outline_qualification=outline.get('assumption'),
            planned_tiles=_sample(planned, 1000), acquired_tiles=_sample(acquired, 1000),
            tile_footprint_offsets_um=footprint, coverage_chip_id=chip_id, current_position_um=position,
            position_source='latest saved image; not a live stage readback', position_captured_at=latest.get('captured_at') if latest else None,
            current_position_at=latest.get('captured_at') if latest else None,
            current_position_saved_at=latest.get('saved_at') if latest else None,
            focus_anchors_um=surface.get('verified_points_um', []), rays=discovery.get('rays', {})),
        counts=dict(seeds=len(seeds), edge_probes=len(records), planned_tiles=len(planned), acquired_tiles=len(acquired),
            seeds_sampled=len(seeds)>1000, edge_probes_sampled=len(records)>80,
            planned_tiles_sampled=len(planned)>1000, acquired_tiles_sampled=len(acquired)>1000),
        focus=focus, events=events[-40:], timing=dict(started_at=batch.get('started_at'),
            deadline_at=deadline_at, deadline_utc=guard.get('deadline_utc'), deadline_guard_state=guard.get('state'),
            elapsed_s=max(0, now-batch['started_at']) if _number(batch.get('started_at')) else None,
            active_elapsed_s=batch.get('elapsed_seconds'), active_budget_s=batch.get('options', {}).get('walltime_budget_s')),
        storage=storage, warnings=list(dict.fromkeys(warnings))[:12],
        qualification='Saved acquisition evidence only. Estimated outlines do not establish discovery of every chip.')
    # A very large campaign remains a bounded dashboard response; the recorded
    # counts above always refer to the complete saved collections.
    if len(json.dumps(output)) > 300000:
        for key in ('seeds', 'planned_tiles', 'acquired_tiles'):
            output['stage_map'][key] = _sample(output['stage_map'][key], 300)
            output['counts'][key + '_sampled'] = output['counts'][key] > 300
        output['warnings'].append('Large campaign geometry sampled for display; original evidence is unchanged.')
    return output
