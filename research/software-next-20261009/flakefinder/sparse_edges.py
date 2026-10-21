"""Fit chip geometry from coherent substrate/holder edges, without hardware.

The result is an estimated convex outline, not a continuously observed collar.
Small dark objects cannot trigger edge following. A line must separate sizeable
substrate and holder regions across the image. Full-raster border observations
must check the estimate; chips with curved/occluded edges can remain unresolved.
"""
from pathlib import Path
import argparse
import hashlib
import json
import time

import cv2
import numpy as np

from .layout import SUBSTRATE, NON_SUBSTRATE
from .model import atomic_json


def _standard_hough_candidate(edges, s, b):
    h, w = edges.shape
    # Hough sorts peaks by votes. Evaluate only the strongest twelve; this is a
    # bounded proposal fallback, not a search over arbitrary weak separators.
    lines = cv2.HoughLines(edges, 1, np.pi/720, threshold=35)
    if lines is None:
        return None
    yy, xx = np.mgrid[:h, :w]
    best = None
    for offset, angle in lines.reshape(-1, 2)[:12]:
        normal = np.array([np.cos(angle), np.sin(angle)], float)
        distance = xx*normal[0]+yy*normal[1]-offset
        if np.median(distance[b]) < np.median(distance[s]):
            normal, distance = -normal, -distance
        inside, outside = distance < -4, distance > 4
        if min(inside.mean(), outside.mean()) < .07:
            continue
        substrate_purity = float(s[inside].mean())
        holder_purity = float(b[outside].mean())
        if substrate_purity < .78 or holder_purity < .78:
            continue
        near = (edges > 0) & (abs(distance) < 3)
        points = np.column_stack([xx[near], yy[near]]).astype(float)
        if len(points) < .4*min(h, w):
            continue
        tangent = np.array([-normal[1], normal[0]])
        measured_span = float(np.ptp(points @ tangent))
        if measured_span < .45*min(h, w):
            continue
        centre = points.mean(axis=0)
        _, _, vt = np.linalg.svd(points-centre, full_matrices=False)
        fitted_normal = vt[-1]
        if fitted_normal @ normal < 0:
            fitted_normal = -fitted_normal
        residual = np.abs((points-centre) @ fitted_normal)
        score = min(substrate_purity, holder_purity)*measured_span
        if best is None or score > best[0]:
            best = (score, points, fitted_normal, substrate_purity, holder_purity, residual)
    return best


def _label_edge(labels, position_um, source_shape, matrix, *, standard_fallback=False):
    """Return an outward stage-space half-plane normal and its edge samples."""
    # Geometry only needs a few hundred pixels, not a full-resolution TIFF.
    h0, w0 = labels.shape
    scale = min(1., 320 / max(h0, w0))
    a = cv2.resize(labels, (round(w0*scale), round(h0*scale)), interpolation=cv2.INTER_NEAREST)
    h, w = a.shape
    s, b = a == SUBSTRATE, a == NON_SUBSTRATE
    if min(s.mean(), b.mean()) < .08:
        return None
    # Remove small interior objects for line proposal only. Final side evidence
    # is always evaluated against the original labels, including contamination.
    clean = cv2.medianBlur(np.uint8(s)*255, 7)
    edges = cv2.Canny(clean, 40, 100)
    edges[:4] = edges[-4:] = 0
    edges[:, :4] = edges[:, -4:] = 0
    lines = cv2.HoughLinesP(edges, 1, np.pi/720, threshold=35,
                            minLineLength=.45*min(h, w), maxLineGap=12)
    yy, xx = np.mgrid[:h, :w]
    best = None
    for x1, y1, x2, y2 in (() if lines is None else lines.reshape(-1, 4)):
        tangent = np.array([x2-x1, y2-y1], float)
        normal = np.array([-tangent[1], tangent[0]]) / np.linalg.norm(tangent)
        offset = normal @ [x1, y1]
        distance = xx*normal[0]+yy*normal[1]-offset
        if np.median(distance[b]) < np.median(distance[s]):
            normal, offset, distance = -normal, -offset, -distance
        inside, outside = distance < -4, distance > 4
        if min(inside.mean(), outside.mean()) < .07:
            continue
        substrate_purity = float(s[inside].mean())
        holder_purity = float(b[outside].mean())
        if substrate_purity < .78 or holder_purity < .78:
            continue
        near = (edges > 0) & (abs(distance) < 3)
        points = np.column_stack([xx[near], yy[near]]).astype(float)
        if len(points) < .4*min(h, w):
            continue
        centre = points.mean(axis=0)
        _, _, vt = np.linalg.svd(points-centre, full_matrices=False)
        fitted_normal = vt[-1]
        if fitted_normal @ normal < 0:
            fitted_normal = -fitted_normal
        residual = np.abs((points-centre) @ fitted_normal)
        score = min(substrate_purity, holder_purity)*np.linalg.norm(tangent)
        if best is None or score > best[0]:
            best = (score, points, fitted_normal, substrate_purity, holder_purity, residual)
    if best is None and standard_fallback:
        best = _standard_hough_candidate(edges, s, b)
    if best is None:
        return None
    _, points, normal, ps, ph, residual = best
    sh, sw = source_shape[:2]
    linear = np.asarray(matrix) @ np.diag([sw/w, sh/h])
    origin = np.asarray(position_um[:2])+np.asarray(matrix) @ [(sw/w-sw)/2, (sh/h-sh)/2]
    stage_points = points @ linear.T+origin
    stage_normal = np.linalg.solve(linear.T, normal)
    stage_normal /= np.linalg.norm(stage_normal)
    return {'normal': stage_normal.tolist(), 'offset_um': float(np.median(stage_points @ stage_normal)),
            'points_um': stage_points[::max(1, len(stage_points)//80)].tolist(),
            'substrate_purity': ps, 'holder_purity': ph,
            'local_residual_px_p95': float(np.quantile(residual, .95))}


def coherent_edge(labels, position_um, source_shape, matrix):
    """Legacy label-only API: its proposal behavior is deliberately unchanged."""
    return _label_edge(labels, position_um, source_shape, matrix)


def validated_coherent_edge(frame, labels, matrix, profile):
    """Propose from labels, then require independent raw photometric support.

    Only when the original HoughP path yields no label candidate are up to 12
    standard Hough peaks considered. Only a new fallback proposal may be refined
    to measured raw coordinates. A previously accepted fast-path candidate is
    either retained at exactly its original coordinates or rejected.
    """
    from .photometric_edges import assess_edge_profile, supported_edge, MEASURED_PROFILE_MODEL
    contract = assess_edge_profile(profile, frame)
    if not contract['supported']:
        return None, {'accepted': False, 'reason': contract['reason'], 'profile': contract}
    position = [frame.position.x, frame.position.y]
    candidate = _label_edge(labels, position, frame.rgb.shape, matrix)
    fallback = candidate is None
    if fallback:
        candidate = _label_edge(labels, position, frame.rgb.shape, matrix, standard_fallback=True)
    if candidate is None:
        return None, {'accepted': False, 'reason': 'No label-side-supported line proposal.', 'profile': contract}
    edge, report = supported_edge(frame, labels, candidate, matrix, allow_raw_refinement=fallback,
        allow_bright=profile.get('holder_model') == MEASURED_PROFILE_MODEL)
    report.update(profile=contract, proposal_path='bounded_standard_hough' if fallback else 'legacy_hough_p',
                  original_label_proposal=candidate, raw_refinement_permitted=fallback)
    return edge, report


def fit_validated_outline(edges, seed, bounds, margin, observations, matrix, source_shape):
    """Reject a fitted polygon contradicting registered substrate observations.

    Rejection retains the measured lines and their diagnostics for further
    probing, but removes the polygon. No line is discarded just to force closure.
    """
    from .photometric_edges import registered_substrate_consistency, ALGORITHM_VERSION
    result = fit_outline(edges, seed, bounds, margin)
    checks = [registered_substrate_consistency(line, observations, matrix, source_shape,
              margin_um=max(float(margin), float(line.get('margin_um', margin))))
              for line in result.get('lines', [])]
    result['algorithm'] = ALGORITHM_VERSION
    result['registered_consistency'] = {'accepted': all(item['accepted'] for item in checks),
        'observation_count': len(observations), 'line_checks': checks,
        'qualification': 'Same separated convex chip hypothesis; not proof of unsampled gaps or complete coverage.'}
    if not result['registered_consistency']['accepted']:
        result.update(status='unresolved', reason='registered_substrate_contradicts_halfplanes', polygon_um=[])
    return result


def _clip(poly, normal, offset):
    result = []
    for a, b in zip(poly, np.roll(poly, -1, axis=0)):
        da, db = np.dot(a, normal)-offset, np.dot(b, normal)-offset
        if da <= 0:
            result.append(a)
        if (da <= 0) != (db <= 0):
            result.append(a+(b-a)*da/(da-db))
    return np.asarray(result, float)


def fit_outline(edges, seed_xy, bounds_um, margin_um=75.):
    """Group consistent outward lines and fit their pooled measured edge points."""
    groups = []
    seed = np.asarray(seed_xy, float)
    for edge in edges:
        normal = np.asarray(edge['normal'])
        if normal @ seed >= edge['offset_um']:
            continue  # An interior occlusion/other chip does not bound this seed.
        for group in groups:
            n = np.asarray(group[0]['normal'])
            if normal @ n > np.cos(np.deg2rad(12)) and abs((edge['offset_um']-normal@seed)-(group[0]['offset_um']-n@seed)) < 450:
                group.append(edge)
                break
        else:
            groups.append([edge])
    lines = []
    for group in groups:
        points = np.concatenate([g['points_um'] for g in group])
        centre = np.median(points, axis=0)
        # Robust orientation from distant edge samples; no rectangle/grid prior.
        vx, vy, cx, cy = cv2.fitLine(points.astype(np.float32), cv2.DIST_HUBER, 0, .01, .001).ravel()
        normal = np.array([-vy, vx], float)
        if normal @ np.asarray(group[0]['normal']) < 0:
            normal = -normal
        offset = float(normal @ [cx, cy])
        residual = abs(points @ normal-offset)
        p95 = float(np.quantile(residual, .95))
        if p95 > 40:
            continue
        # Extra raster margin covers blur/local fit error; this remains a model
        # assumption, not a claimed mathematical bound on unobserved corners.
        allowance = max(float(margin_um), 3*p95)
        lines.append({'normal': normal.tolist(), 'offset_um': offset,
                      'expanded_offset_um': offset+allowance,
                      'residual_um_p95': p95, 'margin_um': allowance,
                      'source_images': [g['image'] for g in group], 'edge_point_count': len(points)})
    if len(lines) < 3:
        return {'status': 'unresolved', 'reason': 'Fewer than three coherent edge directions.', 'lines': lines}
    angles = np.sort(np.mod([np.arctan2(*x['normal'][::-1]) for x in lines], 2*np.pi))
    if np.max(np.diff(np.r_[angles, angles[0]+2*np.pi])) >= np.pi:
        return {'status': 'unresolved', 'reason': 'Measured edge normals do not bound the seed.', 'lines': lines}
    x0, x1, y0, y1 = bounds_um
    poly = np.array([[x0,y0],[x1,y0],[x1,y1],[x0,y1]], float)
    for line in lines:
        poly = _clip(poly, line['normal'], line['expanded_offset_um'])
        if len(poly) < 3:
            return {'status':'unresolved', 'reason':'Inconsistent fitted half-planes.', 'lines':lines}
    touches = np.any(np.isclose(poly[:,0], x0)) or np.any(np.isclose(poly[:,0], x1)) or np.any(np.isclose(poly[:,1], y0)) or np.any(np.isclose(poly[:,1], y1))
    if touches:
        return {'status':'unresolved', 'reason':'Estimated outline reaches search bounds.', 'lines':lines}
    return {'schema':1, 'status':'estimated', 'method':'coherent_halfplane_edges_with_robust_line_fit',
            'polygon_um':poly.tolist(), 'uncertainty_um':max(x['margin_um'] for x in lines),
            'uncertainty_kind':'planning margin, not a statistical confidence bound', 'lines':lines,
            'assumption':'Convex chip with approximately straight edges; unobserved corners are inferred.',
            'coverage_requirement':'Check substrate at raster perimeter and expand/replan if it reaches that perimeter.'}


def estimate_saved_outline(folder, *, cardinal_only=False, margin_um=75.):
    folder = Path(folder)
    source = folder/'discovery.json'
    discovery = json.loads(source.read_text(encoding='utf-8'))
    start = time.perf_counter()
    edges, inspected = [], 0
    for record in discovery['records']:
        if cardinal_only and record['phase']=='boundary_frontier':
            continue
        inspected += 1
        with np.load(folder/record['labels_file']) as data:
            edge = coherent_edge(data['labels'], record['position_um'], discovery['source_shape'], discovery['transform']['matrix'])
        if edge:
            edge.update(image=record['image'], phase=record['phase'], position_um=record['position_um'])
            edges.append(edge)
    result = fit_outline(edges, discovery['seed_xy_um'], discovery['options']['search_bounds_um'], margin_um)
    result.update(evidence={'folder':str(folder.resolve()), 'discovery_sha256':hashlib.sha256(source.read_bytes()).hexdigest(),
                            'inspected_images':inspected, 'coherent_edge_images':len(edges), 'cardinal_only':cardinal_only},
                  processing_seconds=time.perf_counter()-start)
    return result


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('discovery', type=Path)
    parser.add_argument('--output', required=True, type=Path)
    parser.add_argument('--cardinal-only', action='store_true')
    args=parser.parse_args()
    result=estimate_saved_outline(args.discovery, cardinal_only=args.cardinal_only)
    atomic_json(args.output, result)
    print(json.dumps({k:v for k,v in result.items() if k not in ('lines','polygon_um')}))


if __name__=='__main__':
    main()
