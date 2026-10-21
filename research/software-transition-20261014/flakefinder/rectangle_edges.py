"""Offline rectangle-prior geometry with locally supported raw RGB sides.

The user-supplied rectangular-chip prior shares orientation between four sides,
but every side location still requires measured image points. A search bracket
can constrain a raw fit; its midpoint is never substituted for a measured edge.
No imports or functions connect hardware or write acquisition evidence.
"""
import math

import cv2
import numpy as np

from .layout import resize_rgb_float32


def rectangle_orientation(edges):
    """Robust common orientation modulo 90 degrees, with no location inference."""
    angles = []
    for edge in edges:
        n = np.asarray(edge.get('normal', []), float)
        if n.shape == (2,) and np.isfinite(n).all() and np.linalg.norm(n) > 0:
            angles.append(math.atan2(n[1], n[0]))
    if not angles:
        return {'status': 'unresolved', 'reason': 'No measured side orientation.', 'normals': []}
    angles = np.asarray(angles)
    # Choose the observed orientation with smallest trimmed angular deviation.
    def residual(theta):
        return np.angle(np.exp(4j * (angles - theta))) / 4
    centre = min(angles, key=lambda theta: float(np.median(abs(residual(theta)))))
    keep = abs(residual(centre)) <= math.radians(12)
    theta = float(np.angle(np.mean(np.exp(4j * angles[keep]))) / 4)
    normals = [[math.cos(theta + i*math.pi/2), math.sin(theta + i*math.pi/2)] for i in range(4)]
    return {'status': 'estimated', 'orientation_deg': math.degrees(theta), 'normals': normals,
            'measured_orientation_count': int(keep.sum()),
            'angular_scatter_deg': math.degrees(float(np.max(abs(residual(theta)[keep])))),
            'qualification': 'Shared rectangular orientation only; no missing side location inferred.'}


def measure_rectangle_side(frame, matrix, normal, *, offset_bracket_um=None):
    """Measure a dark-outside RGB step using an approximate outward normal.

    Local median plateau profiles tolerate debris and channel outliers. A
    bounded slope/offset consensus keeps at most one vote per tangent profile;
    distributed local support replaces whole-field purity/span requirements.
    ``offset_bracket_um`` bounds normal.dot(stage_position), in stage units.
    It does not establish an edge without RGB transitions.
    """
    report = {'accepted': False, 'method': 'rectangle_prior_trimmed_RGB_profiles',
              'step_polarity': 'outside_darker', 'location_source': 'measured_raw_RGB_points',
              'focus_needed': False,
              'focus_qualification': 'Geometry rejection alone is not evidence that autofocus is needed.'}
    matrix = np.asarray(matrix, float)
    normal = np.asarray(normal, float)
    if matrix.shape != (2, 2) or not np.isfinite(matrix).all() or abs(np.linalg.det(matrix)) < 1e-12:
        raise ValueError('A finite nonsingular camera transform is required.')
    if normal.shape != (2,) or not np.isfinite(normal).all() or np.linalg.norm(normal) == 0:
        raise ValueError('A finite outward side normal is required.')
    normal = normal / np.linalg.norm(normal)
    bracket = None
    if offset_bracket_um is not None:
        bracket = np.asarray(offset_bracket_um, float)
        if bracket.shape != (2,) or not np.isfinite(bracket).all() or bracket[0] >= bracket[1]:
            raise ValueError('Offset bracket must contain two increasing finite positions.')
        report['offset_bracket_um'] = bracket.tolist()
    source = np.asarray(frame.rgb)
    if source.ndim != 3 or source.shape[2] != 3 or not np.isfinite(source).all():
        raise ValueError('Finite raw RGB image required.')
    sh, sw = source.shape[:2]
    scale = min(1., 512 / max(sh, sw))
    w, h = round(sw*scale), round(sh*scale)
    if min(w, h) < 32:
        return None, dict(report, reason='Image too small for independent plateau profiles.')
    rgb = resize_rgb_float32(source, (w, h))
    rgb = cv2.GaussianBlur(rgb, (0, 0), .7)
    linear = matrix @ np.diag([sw/w, sh/h])
    origin = np.array([frame.position.x, frame.position.y]) + matrix @ [(sw/w-sw)/2, (sh/h-sh)/2]
    image_normal = linear.T @ normal
    factor = float(np.linalg.norm(image_normal))
    image_normal /= factor
    tangent = np.array([-image_normal[1], image_normal[0]])
    corners = np.array([[0,0], [w-1,0], [w-1,h-1], [0,h-1]])
    t0, t1 = float(np.min(corners@tangent)), float(np.max(corners@tangent))
    q0, q1 = float(np.min(corners@image_normal)), float(np.max(corners@image_normal))
    candidates = []
    for profile_id, t in enumerate(np.linspace(t0, t1, 66)[1:-1]):
        q = np.arange(math.floor(q0), math.ceil(q1)+1, dtype=float)
        points = t*tangent + q[:, None]*image_normal
        valid = (points[:,0] >= 1) & (points[:,0] < w-2) & (points[:,1] >= 1) & (points[:,1] < h-2)
        points, q = points[valid], q[valid]
        if len(q) < 28:
            continue
        values = cv2.remap(rgb, points[:,0].astype(np.float32)[None,:], points[:,1].astype(np.float32)[None,:], cv2.INTER_LINEAR)[0]
        choices = []
        for k in range(12, len(q)-12):
            offset = float(origin@normal + q[k]*factor)
            if bracket is not None and not bracket[0] <= offset <= bracket[1]:
                continue
            left = np.median(values[k-12:k-4], axis=0)
            right = np.median(values[k+4:k+12], axis=0)
            delta = left-right
            contrast = delta / np.maximum(np.maximum(abs(left), abs(right)), 1.)
            # Two channels must agree strongly, and the third may be noisy
            # but cannot show a substantial opposite transition.
            if np.count_nonzero(contrast >= .08) < 2 or float(np.min(contrast)) < -.03:
                continue
            noise = np.median(abs(np.diff(values[k-12:k-4], axis=0)), axis=0) + np.median(abs(np.diff(values[k+4:k+12], axis=0)), axis=0)
            if np.count_nonzero(delta > 3*np.maximum(noise, .25)) < 2:
                continue
            choices.append((float(np.median(contrast)), k))
        selected = []
        for strength, k in sorted(choices, reverse=True):
            if any(abs(k-old) < 12 for old in selected):
                continue
            selected.append(k)
            candidates.append((t, q[k], profile_id, strength))
            if len(selected) == 3:
                break
    report.update(tested_profiles=64, candidate_points=len(candidates), raw_working_shape=[h,w])
    if len(candidates) < 8:
        return None, dict(report, reason='Insufficient local RGB step evidence.')
    data = np.asarray(candidates)
    # A bounded Hough-style consensus near the shared orientation is robust to
    # large isolated flakes and adhesive. It requires no global colour purity.
    best = None
    for slope in np.linspace(-math.tan(math.radians(7)), math.tan(math.radians(7)), 29):
        offsets = data[:,1] - slope*data[:,0]
        for intercept in np.unique(np.round(offsets/3)*3):
            selected = []
            for profile_id in np.unique(data[:,2]):
                indices = np.flatnonzero((data[:,2] == profile_id) & (abs(offsets-intercept) <= 3.))
                if len(indices):
                    selected.append(int(min(indices, key=lambda j: (abs(offsets[j]-intercept), -data[j,3]))))
            if len(selected) < 8:
                continue
            selected = np.asarray(selected)
            span = float(np.ptp(data[selected,0]))
            score = (len(selected), span, float(np.sum(data[selected,3])))
            if best is None or score > best[0]:
                best = (score, selected)
    if best is None:
        return None, dict(report, reason='RGB transitions lack a coherent side consensus.')
    chosen = data[best[1]]
    tq = chosen[:,:2].astype(np.float32)
    vx, vy, cx, cy = cv2.fitLine(tq, cv2.DIST_HUBER, 0, .01, .001).ravel()
    slope = float(vy/vx) if abs(vx) > 1e-9 else float('inf')
    if abs(slope) > math.tan(math.radians(8)):
        return None, dict(report, reason='Measured transition disagrees with rectangle orientation.')
    intercept = float(np.median(chosen[:,1] - slope*chosen[:,0]))
    scatter = max(.5, 1.4826*float(np.median(abs(chosen[:,1]-slope*chosen[:,0]-intercept))))
    chosen = chosen[abs(chosen[:,1]-slope*chosen[:,0]-intercept) <= max(2., 3*scatter)]
    pixels = chosen[:,0,None]*tangent + chosen[:,1,None]*image_normal
    stage = pixels@linear.T + origin
    stage_tangent = linear @ (tangent+slope*image_normal)
    measured_normal = np.array([stage_tangent[1], -stage_tangent[0]])
    measured_normal /= np.linalg.norm(measured_normal)
    if measured_normal@normal < 0:
        measured_normal *= -1
    offset = float(np.median(stage@measured_normal))
    raw_span = float(np.ptp(stage @ np.array([-measured_normal[1], measured_normal[0]])))
    field_span = float((t1-t0)*np.linalg.norm(linear@tangent))
    required_span = min(60., .2*field_span)
    bins = np.floor(3*(chosen[:,0]-chosen[:,0].min())/max(float(np.ptp(chosen[:,0])), 1e-6)).clip(0,2)
    if len(chosen) < 8 or raw_span < required_span or len(set(bins)) < 3:
        return None, dict(report, reason='Local transition support is not spatially distributed.', supported_profiles=len(chosen), raw_span_um=raw_span)
    uncertainty = max(2*factor, 3*scatter*factor)
    edge = {'normal': measured_normal.tolist(), 'offset_um': offset, 'points_um': stage.tolist(),
            'uncertainty_um': uncertainty, 'proposal_method': report['method'],
            'rectangle_orientation_prior': normal.tolist(), 'raw_support_profiles': len(chosen),
            'raw_support_span_um': raw_span, 'local_residual_px_p95': float(np.quantile(abs(chosen[:,1]-slope*chosen[:,0]-intercept), .95))}
    report.update(accepted=True, reason='Distributed local RGB transitions support a measured rectangle side.',
                  supported_profiles=len(chosen), raw_span_um=raw_span, uncertainty_um=uncertainty,
                  measured_orientation_difference_deg=math.degrees(math.acos(float(np.clip(normal@measured_normal,-1,1)))),
                  measured_raw_points_image=pixels.tolist())
    return edge, report


def fit_rectangle_outline(edges, seed, bounds, margin_um=75.):
    """Estimate four shared-orientation sides from measured points, never width priors."""
    orientation = rectangle_orientation(edges)
    result = {'status': 'unresolved', 'method': 'measured_sides_shared_rectangle_orientation',
              'polygon_um': [], 'lines': [], 'orientation': orientation, 'all_chips_found': False,
              'assumption': 'Approximately rectangular chip; unobserved corners inferred from four measured sides.'}
    if orientation['status'] != 'estimated':
        return dict(result, reason=orientation['reason'])
    seed = np.asarray(seed, float)
    bounds = np.asarray(bounds, float)
    if seed.shape != (2,) or bounds.shape != (4,) or not np.isfinite(np.r_[seed,bounds,margin_um]).all() or margin_um <= 0:
        raise ValueError('Finite seed, search bounds and positive margin required.')
    normals = np.asarray(orientation['normals'])
    groups = [[] for _ in range(4)]
    for edge in edges:
        points = np.asarray(edge.get('points_um', []), float)
        if points.ndim != 2 or points.shape[1] != 2 or len(points) < 3 or not np.isfinite(points).all():
            continue
        n = np.asarray(edge['normal'], float); n /= np.linalg.norm(n)
        index = int(np.argmax(normals@n))
        if normals[index]@n < math.cos(math.radians(12)):
            continue
        if float(np.median(points@normals[index])) <= float(seed@normals[index]):
            continue
        groups[index].append(edge)
    missing = [i for i,g in enumerate(groups) if not g]
    if missing:
        return dict(result, reason='Missing measured rectangle side location.', missing_side_indices=missing)
    offsets = []
    for normal, group in zip(normals, groups):
        points = np.concatenate([edge['points_um'] for edge in group])
        offset = float(np.median(points@normal)); offsets.append(offset)
        scatter = float(np.quantile(abs(points@normal-offset), .95))
        result['lines'].append({'normal':normal.tolist(), 'offset_um':offset,
            'source_images':[e['image'] for e in group if e.get('image')], 'edge_point_count':len(points),
            'measured_points_um':points.tolist(), 'residual_um_p95':scatter,
            'local_uncertainty_um':max([float(e.get('uncertainty_um',0)) for e in group]+[scatter])})
    width, height = offsets[0]+offsets[2], offsets[1]+offsets[3]
    if min(width,height) <= 0:
        return dict(result, reason='Measured opposite sides have inconsistent locations.')
    # Slight nonorthogonality creates growing uncertainty toward unobserved
    # corners. Include it in the planning margin instead of asserting exactness.
    angular_allowance = math.tan(math.radians(orientation['angular_scatter_deg']))*max(width,height)/2
    for line in result['lines']:
        line['margin_um'] = max(float(margin_um), line['local_uncertainty_um']+angular_allowance)
        line['expanded_offset_um'] = line['offset_um']+line['margin_um']
    polygon = []
    for i in range(4):
        j = (i+1)%4
        polygon.append(np.linalg.solve(normals[[i,j]], [result['lines'][i]['expanded_offset_um'],result['lines'][j]['expanded_offset_um']]).tolist())
    p = np.asarray(polygon); x0,x1,y0,y1 = bounds
    if np.any(p[:,0] <= x0) or np.any(p[:,0] >= x1) or np.any(p[:,1] <= y0) or np.any(p[:,1] >= y1):
        return dict(result, reason='Estimated rectangle reaches search bounds.')
    result.update(status='estimated', polygon_um=polygon, measured_dimensions_um=[width,height],
                  uncertainty_um=max(line['margin_um'] for line in result['lines']),
                  uncertainty_kind='Measured local scatter plus shared-orientation corner allowance; not a confidence bound.')
    return result
