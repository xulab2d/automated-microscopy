"""Suggest grid verification probes from separated substrate observations.

All cells remain hypotheses until their centres are observed. The inference
assumes an approximately stage-axis-aligned regular array; it does not create
chip polygons, fill gaps, claim a chip count, or operate the microscope.
"""
from __future__ import annotations
import math
import numpy as np


def _normalise(probes):
    result = []
    for index, p in enumerate(probes):
        position = p.get('position_um', p.get('position'))
        xy = [position['x'], position['y']] if isinstance(position, dict) else list(position[:2])
        if not np.isfinite(xy).all():
            raise ValueError('Probe coordinates must be finite.')
        label = p.get('centre_classification', p.get('classification', 'unknown'))
        if label not in ('substrate', 'non_substrate', 'unknown', 'uncertain', 'mixed'):
            raise ValueError('Supply classified probes, not raw colour thresholds.')
        result.append({'name': p.get('name', str(index)), 'xy': np.asarray(xy, float), 'label': label,
                       'source': p.get('source'), 'classification_provenance': p.get('classification_provenance', 'raw-reference colour classification')})
    return result


def _transects(probes, axis, tolerance):
    groups = []
    for p in sorted(probes, key=lambda p: p['xy'][1-axis]):
        group = next((g for g in groups if abs(np.median([q['xy'][1-axis] for q in g])-p['xy'][1-axis]) <= tolerance), None)
        if group is None:
            groups.append([p])
        else:
            group.append(p)
    return [sorted(g, key=lambda p: p['xy'][axis]) for g in groups]


def _bands(transect, axis):
    bands, run = [], []
    for p in transect:
        if p['label'] == 'substrate':
            run.append(p)
        elif run:
            bands.append(run)
            run = []
    if run:
        bands.append(run)
    result = []
    for run in bands:
        low, high = run[0]['xy'][axis], run[-1]['xy'][axis]
        before = [p for p in transect if p['xy'][axis] < low and p['label'] == 'non_substrate']
        after = [p for p in transect if p['xy'][axis] > high and p['label'] == 'non_substrate']
        result.append({'positive_span_um': [float(low), float(high)],
                       'centre_estimate_um': float((low+high)/2),
                       'probe_names': [p['name'] for p in run],
                       'negative_before_um': float(before[-1]['xy'][axis]) if before else None,
                       'negative_after_um': float(after[0]['xy'][axis]) if after else None,
                       'continuity': 'unverified between positive observations'})
    return result


def _axis_model(probes, axis, tolerance, pitch, pitch_bounds, residual_fraction):
    choices = []
    for line in _transects(probes, axis, tolerance):
        bands = _bands(line, axis)
        # Unknown-only separation cannot establish distinct chip bands.
        valid = [b for b in bands if len(b['probe_names']) >= 1]
        centres = np.array([b['centre_estimate_um'] for b in valid])
        separated = len(valid) >= 2 and all(any(p['label'] == 'non_substrate' and a < p['xy'][axis] < b
                                               for p in line) for a, b in zip(centres, centres[1:]))
        if separated:
            choices.append((len(valid), len(line), np.ptp(centres), line, valid, centres))
    if not choices:
        return {'status': 'insufficient_evidence', 'reason': 'Need substrate bands separated by observed non-substrate on one transect.'}
    _, _, _, line, bands, centres = max(choices, key=lambda c: c[:3])
    differences = np.diff(centres)
    supplied = pitch is not None
    if supplied:
        candidates = [float(pitch)]
    else:
        # Largest compatible fundamental avoids gratuitous half-pitch solutions.
        candidates = sorted({float(d/k) for d in differences for k in range(1, 5)}, reverse=True)
    fitted = None
    for candidate in candidates:
        if not math.isfinite(candidate) or candidate <= 0:
            continue
        if pitch_bounds is not None and not pitch_bounds[0] <= candidate <= pitch_bounds[1]:
            continue
        steps = np.maximum(1, np.rint(differences/candidate)).astype(int)
        value = candidate if supplied else float(np.sum(differences*steps)/np.sum(steps**2))
        indices = np.r_[0, np.cumsum(steps)]
        origin = float(np.mean(centres-indices*value))
        residuals = centres-(origin+indices*value)
        if np.max(abs(residuals)) <= residual_fraction*value:
            fitted = value, indices, origin, residuals
            break
    if fitted is None:
        return {'status': 'inconsistent', 'bands': bands, 'reason': 'Band positions do not support the requested regular pitch.'}
    value, indices, origin, residuals = fitted
    if any(b['positive_span_um'][1]-b['positive_span_um'][0] > .9*value for b in bands):
        return {'status': 'ambiguous', 'bands': bands,
                'reason': 'An uninterrupted sparse positive run spans nearly an entire fitted pitch; it may hide multiple chips. Use the earlier grid hypothesis or acquire separating gap probes.'}
    return {'status': 'hypothesis', 'axis': 'xy'[axis], 'transect_um': float(np.median([p['xy'][1-axis] for p in line])),
            'pitch_um': value, 'pitch_source': 'provided' if supplied else 'inferred_from_separated_positive_bands',
            'origin_um': origin, 'band_indices': indices.tolist(), 'bands': bands,
            'band_centre_residuals_um': residuals.tolist(), 'max_abs_residual_um': float(max(abs(residuals))),
            'evidence_intervals': len(differences),
            'limitations': 'Band midpoints estimate centres; positive gaps are unmeasured. A larger apparent period may hide missing intervening chips.'}


def infer_grid_hypotheses(probes, *, pitch_um=None, pitch_bounds_um=None,
                          search_bounds_um=None, expected_shape=None,
                          alignment_tolerance_um=2., centre_verification_radius_um=150.,
                          extend_cells=1, residual_fraction=.15, prior_grid=None):
    """Return axis/grid hypotheses and targeted raw-image verification requests.

    ``probes`` accepts layout.py report observations (centre classifications).
    Optional pitch is a scalar or (x,y); bounds are (min,max) or two such pairs.
    ``expected_shape`` is (rows,columns), a prior used only to prioritize extent
    probes. Search bounds are [xmin,xmax,ymin,ymax]; out-of-bounds requests are
    marked, never silently treated as observed negatives.
    """
    if extend_cells < 0 or int(extend_cells) != extend_cells:
        raise ValueError('extend_cells must be a nonnegative integer.')
    if alignment_tolerance_um < 0 or centre_verification_radius_um < 0 or not 0 < residual_fraction < .5:
        raise ValueError('Invalid inference tolerances.')
    if expected_shape is not None and (len(expected_shape) != 2 or any(int(v) != v or v < 1 for v in expected_shape)):
        raise ValueError('expected_shape must be positive integer rows and columns.')
    if search_bounds_um is not None:
        if len(search_bounds_um) != 4 or not np.isfinite(search_bounds_um).all() or not (search_bounds_um[0] < search_bounds_um[1] and search_bounds_um[2] < search_bounds_um[3]):
            raise ValueError('Invalid search bounds.')
    normal = _normalise(probes)
    pitches = [pitch_um, pitch_um] if pitch_um is None or np.isscalar(pitch_um) else list(pitch_um)
    bounds = [pitch_bounds_um, pitch_bounds_um] if pitch_bounds_um is None or np.asarray(pitch_bounds_um).ndim == 1 else list(pitch_bounds_um)
    if len(pitches) != 2 or len(bounds) != 2:
        raise ValueError('Pitch options require x and y values.')
    if prior_grid is None:
        axes = [_axis_model(normal, i, alignment_tolerance_um, pitches[i], bounds[i], residual_fraction) for i in (0, 1)]
    else:
        import copy
        axes = [copy.deepcopy(prior_grid['axes'][axis]) for axis in 'xy']
        if any(a.get('status') != 'hypothesis' for a in axes):
            raise ValueError('The prior must contain two supported axis hypotheses.')
        for i, a in enumerate(axes):
            indices = set(a['band_indices'])
            extensions = []
            for p in normal:
                if p['label'] != 'substrate' or abs(p['xy'][1-i]-a['transect_um']) > alignment_tolerance_um:
                    continue
                index = int(round((p['xy'][i]-a['origin_um'])/a['pitch_um']))
                if abs(p['xy'][i]-(a['origin_um']+index*a['pitch_um'])) <= centre_verification_radius_um:
                    if index not in indices:
                        extensions.append({'index': index, 'probe': p['name']})
                    indices.add(index)
            a['band_indices'] = sorted(indices)
            a['prior_extension_evidence'] = extensions
            a['pitch_source'] = 'retained_prior; newly observed centres extend lattice without merging unsampled positive gaps'
    result = {'schema': 1, 'kind': 'unverified_grid_hypotheses', 'axes': dict(zip('xy', axes)),
              'expected_shape_prior': expected_shape, 'cells': [], 'recommendations': [],
              'chip_count': None, 'status': 'insufficient_evidence',
              'limitations': 'Predictions are verification locations, not chip polygons or proof of occupied/missing grid slots.'}
    result['source_observations'] = [{'name': p['name'], 'xy_um': p['xy'].tolist(), 'classification': p['label'],
                                     'source': p['source'], 'classification_provenance': p['classification_provenance']} for p in normal]
    result['geometry_assumption'] = 'Approximately axis-aligned array of chip centres; individual chips may be rotated and their boundaries are not inferred here.'
    if any(a['status'] != 'hypothesis' for a in axes):
        return result
    result['status'] = 'hypothesis'
    ranges = [range(min(a['band_indices'])-extend_cells, max(a['band_indices'])+extend_cells+1) for a in axes]
    for iy in ranges[1]:
        for ix in ranges[0]:
            indices = [ix, iy]
            xy = [a['origin_um']+index*a['pitch_um'] for a, index in zip(axes, indices)]
            nearby = [p for p in normal if np.linalg.norm(p['xy']-xy) <= centre_verification_radius_um]
            labels = {p['label'] for p in nearby if p['label'] in ('substrate', 'non_substrate')}
            status = 'substrate_observed_near_predicted_centre' if labels == {'substrate'} else 'non_substrate_observed_near_predicted_centre' if labels == {'non_substrate'} else 'conflicting_observations' if len(labels) > 1 else 'unverified'
            extension = [index < min(a['band_indices']) or index > max(a['band_indices']) for a, index in zip(axes, indices)]
            allowed = search_bounds_um is None or (search_bounds_um[0] <= xy[0] <= search_bounds_um[1] and search_bounds_um[2] <= xy[1] <= search_bounds_um[3])
            cell = {'index': indices, 'xy_um': xy, 'status': status, 'evidence_probes': [p['name'] for p in nearby],
                    'extends_observed_bands': extension, 'within_search_bounds': allowed}
            result['cells'].append(cell)
            if status != 'unverified':
                continue
            on_x_line = abs(xy[1]-axes[0]['transect_um']) <= centre_verification_radius_um
            on_y_line = abs(xy[0]-axes[1]['transect_um']) <= centre_verification_radius_um
            if (extension[0] and not extension[1] and on_x_line) or (extension[1] and not extension[0] and on_y_line):
                priority, reason = 1, 'verify_array_extent_on_established_transect'
            elif not any(extension):
                priority, reason = 2, 'verify_interior_predicted_chip_centre'
            else:
                priority, reason = 3, 'verify_extended_grid_cell_after_extent_is_known'
            result['recommendations'].append({**cell, 'priority': priority, 'reason': reason})
    result['recommendations'].sort(key=lambda r: (r['priority'], not r['within_search_bounds'], r['xy_um']))
    return result


def centre_verification_plan(report, patch_size_um=1500.):
    """Generate centre-only probe requests; patch size is a proposal, not clearance.

    Only a verified centre is suitable to start a bounded interior survey, after
    local focus is established. The footprint still needs substrate verification.
    """
    if not math.isfinite(patch_size_um) or patch_size_um <= 0:
        raise ValueError('Patch size must be positive.')
    pending = [c for c in report.get('cells', []) if c['status'] in ('unverified', 'conflicting_observations')]
    pending.sort(key=lambda c: (c['index'][1], c['index'][0] if c['index'][1]%2==0 else -c['index'][0]))
    return {'schema': 1, 'kind': 'grid_centre_verification_plan', 'status': 'proposed',
            'requested_points_um': [c['xy_um'] for c in pending if c['within_search_bounds']],
            'pending_centres': pending, 'already_observed_centres': [c for c in report.get('cells', []) if c['status']=='substrate_observed_near_predicted_centre'],
            'known_non_substrate_centres': [c for c in report.get('cells', []) if c['status']=='non_substrate_observed_near_predicted_centre'],
            'interior_patch_proposal_um': [patch_size_um, patch_size_um],
            'chip_boundary_status': 'Unmeasured; array centre model does not supply chip rectangle orientation or clearance.',
            'source_observations': report.get('source_observations', [])}


def build_verified_layout(grid_report, observations, centre_tolerance_um=150., max_shift_um=3000., full_field_fraction=.85):
    """Select observed substrate locations; shifted locations need full-field support.

    A negative predicted centre leaves the slot unresolved. Nearby observations
    can supply a safer displaced survey centre, but no missing-chip declaration
    follows from a finite offset search.
    """
    cells = grid_report.get('cells', [])
    if not cells:
        raise ValueError('A supported grid hypothesis is required.')
    low = [min(c['index'][axis] for c in cells) for axis in (0,1)]
    chips, unresolved = [], []
    for cell in cells:
        nominal = np.asarray(cell['xy_um'])
        chip_id = f"R{cell['index'][1]-low[1]+1}C{cell['index'][0]-low[0]+1}"
        nearby = []
        for o in observations:
            xy = np.asarray(o['position_um'][:2])
            distance = float(np.linalg.norm(xy-nominal))
            if distance <= max_shift_um:
                nearby.append((distance, o))
        accepted = [(d,o) for d,o in nearby if o.get('centre_classification')=='substrate'
                    and o.get('fractions',{}).get('substrate',0) >= full_field_fraction]
        if accepted:
            distance, chosen = min(accepted, key=lambda p:(p[0] > centre_tolerance_um, p[0], -p[1]['fractions']['substrate']))
            xy = list(chosen['position_um'][:2])
            chips.append({'id': chip_id, 'centre_um': xy, 'nominal_centre_um': nominal.tolist(),
                          'status': 'substrate_verified', 'source_image': chosen['source'], 'source_probe': chosen['name'],
                          'centre_shift_um': (np.asarray(xy)-nominal).tolist(),
                          'centre_kind': 'shifted_observed_interior' if distance>centre_tolerance_um else 'predicted_centre_verified',
                          'substrate_fraction': chosen['fractions']['substrate'],
                          'boundary_status': 'unmeasured; verify survey footprint and local focus before expanding'})
        else:
            central = [o for d,o in nearby if d<=centre_tolerance_um]
            unresolved.append({'id':chip_id,'nominal_centre_um':nominal.tolist(),'status':'unresolved',
                               'centre_observations':[{'name':o['name'],'classification':o.get('centre_classification'),'source_image':o.get('source')} for o in central],
                               'neighborhood_observations':[{'name':o['name'],'position_um':o['position_um'],'classification':o.get('centre_classification'),'substrate_fraction':o.get('fractions',{}).get('substrate'),'source_image':o.get('source')} for _,o in nearby],
                               'reason':'No full-field substrate observation yet; a holder-centred or incompletely searched slot is not a confirmed missing chip.'})
    return {'schema':1,'chips':chips,'missing_slots':unresolved,'confirmed_missing_slots':[],
            'method':{'kind':'raw-reference colour verification at predicted or nearby observed centres',
                      'full_field_substrate_fraction_min':full_field_fraction,'maximum_shift_um':max_shift_um,
                      'unresolved_policy':'No forced chip count; missing_slots are unresolved locations, not confirmed absences.',
                      'array_vs_chip_rotation':'The lattice describes centre positions. Individual chip rotation and boundaries remain unmeasured.'},
            'grid':{'axes':grid_report['axes'],'cells':cells},
            'source_observations':observations}


def render_verified_layout(layout, path):
    """Compact map for acquisition; technical provenance stays in the JSON."""
    from PIL import Image, ImageDraw, ImageFont
    from pathlib import Path
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    image = Image.new('RGB', (1040, 830), '#101b27')
    draw = ImageDraw.Draw(image)
    def font(size):
        try: return ImageFont.truetype('C:/Windows/Fonts/segoeui.ttf', size)
        except OSError: return ImageFont.load_default(size=size)
    large, normal, small = font(28), font(18), font(15)
    chips, unresolved = layout['chips'], layout['missing_slots']
    points = [c['xy_um'] for c in layout['grid']['cells']]
    pitches = [layout['grid']['axes'][axis]['pitch_um']/1000 for axis in 'xy']
    draw.text((32, 20), f'Chip map — {len(chips)} located', fill='#edf5fc', font=large)
    pitch_text = f'{pitches[0]:g} mm spacing' if pitches[0] == pitches[1] else f'{pitches[0]:g} × {pitches[1]:g} mm spacing'
    shifted = [c for c in chips if c['centre_kind'].startswith('shifted')]
    detail = pitch_text
    if shifted:
        c = shifted[0]
        detail += f" · {c['id']} located at X {c['centre_um'][0]/1000:g}, Y {c['centre_um'][1]/1000:g} mm"
    draw.text((32, 64), detail, fill='#b5cadb', font=normal)
    draw.ellipse((780, 29, 794, 43), fill='#75e7b3')
    draw.text((804, 22), 'Located chip', fill='#d8ecdf', font=small)
    draw.line((783, 64, 793, 74), fill='#e9ba75', width=2)
    draw.line((793, 64, 783, 74), fill='#e9ba75', width=2)
    draw.text((804, 56), 'No chip seen', fill='#ead1ac', font=small)
    x0, x1 = min(p[0] for p in points)-4000, max(p[0] for p in points)+6000
    y0, y1 = min(p[1] for p in points)-4000, max(p[1] for p in points)+4000
    def xy(p): return (95+(p[0]-x0)/(x1-x0)*865, 125+(p[1]-y0)/(y1-y0)*610)
    for x in sorted({p[0] for p in points}):
        a, b = xy([x,y0]), xy([x,y1]);draw.line([a,b], fill='#294052')
        draw.text((a[0]-17, 746), f'{x/1000:g}', font=normal, fill='#b5cadb')
    for y in sorted({p[1] for p in points}):
        a, b = xy([x0,y]), xy([x1,y]);draw.line([a,b], fill='#294052')
        draw.text((40,a[1]-12), f'{y/1000:g}', font=normal, fill='#b5cadb')
    draw.text((450, 790), 'Stage X (mm)', font=normal, fill='#b5cadb')
    draw.text((18, 101), 'Y (mm)', font=normal, fill='#b5cadb')
    for c in chips:
        cx,cy = xy(c['centre_um'])
        draw.ellipse((cx-10,cy-10,cx+10,cy+10), fill='#75e7b3', outline='#d4ffea', width=2)
        draw.text((cx+16,cy-14), c['id'], font=normal, fill='#dcf9ed')
        if c['centre_kind'].startswith('shifted'):
            ox,oy = xy(c['nominal_centre_um'])
            draw.ellipse((ox-5,oy-5,ox+5,oy+5), outline='#658f7d', width=1)
            draw.line([(ox+5,oy),(cx-10,cy)], fill='#75e7b3', width=2)
    for c in unresolved:
        cx,cy = xy(c['nominal_centre_um'])
        draw.ellipse((cx-11,cy-11,cx+11,cy+11), outline='#e9ba75', width=2)
        draw.line((cx-7,cy-7,cx+7,cy+7), fill='#e9ba75', width=2)
        draw.line((cx+7,cy-7,cx-7,cy+7), fill='#e9ba75', width=2)
        observations = c.get('neighborhood_observations', [])
        absent = len(observations) >= 9 and all(o.get('classification') == 'non_substrate' for o in observations)
        draw.text((cx+16,cy-23), c['id'], font=normal, fill='#f2d4a7')
        draw.text((cx+16,cy+3), 'No chip seen' if absent else 'Not verified', font=small, fill='#d0ae7f')
    image.save(path)
    return path
