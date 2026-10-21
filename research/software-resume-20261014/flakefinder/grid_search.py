"""Pure search-order hints from an uncertain, translated and rotated chip grid.

Measured enclosures and explicitly uncertain four-side rectangle estimates can
anchor search proposals. Predicted sites never count as detected chips and
never remove samples from the ordinary lattice.
An arbitrary starting XY cannot establish grid bounds: callers retain their
explicit, physically permitted search envelope. Imports perform no I/O.
"""
from dataclasses import asdict, dataclass
import math

import numpy as np


@dataclass(frozen=True)
class GridPriorOptions:
    rows: int = 4
    columns: int = 4
    spacing_um: float = 10000.
    spacing_tolerance_fraction: float = .2
    max_residual_um: float = 1000.
    max_hypotheses: int = 64
    max_prior_probes: int = 8
    minimum_chip_diameter_um: float = 5000.

    def __post_init__(self):
        for key in ('rows', 'columns', 'max_hypotheses', 'max_prior_probes'):
            value = getattr(self, key)
            if type(value) is not int or not 1 <= value <= 64:
                raise ValueError(key + ' must be an integer from 1 to 64.')
        if not 2 <= self.rows * self.columns <= 64:
            raise ValueError('Grid must contain 2 to 64 sites.')
        if not np.isfinite([self.spacing_um, self.spacing_tolerance_fraction,
                            self.max_residual_um, self.minimum_chip_diameter_um]).all():
            raise ValueError('Grid parameters must be finite.')
        if min(self.spacing_um, self.max_residual_um, self.minimum_chip_diameter_um) <= 0:
            raise ValueError('Grid spacing and residual tolerance must be positive.')
        if not 0 <= self.spacing_tolerance_fraction < .5:
            raise ValueError('Spacing tolerance must be between zero and one half.')


def _search_anchors(observations):
    centres, identifiers, evidence = [], [], []
    for item in observations:
        measured = item.get('boundary_status') == 'measured_enclosure'
        outline = item.get('estimated_outline') or {}
        estimated = (item.get('boundary_status') == 'estimated'
                     and outline.get('status') == 'estimated'
                     and outline.get('method') == 'measured_sides_shared_rectangle_orientation'
                     and len(outline.get('lines', [])) == 4
                     and all(line.get('edge_point_count', 0) >= 3 and line.get('source_images')
                             for line in outline['lines']))
        if not measured and not estimated:
            continue
        polygon = np.asarray(item.get('stage_polygon_um', []) if measured else outline.get('polygon_um', []), dtype=float)
        if polygon.ndim != 2 or polygon.shape[1] != 2 or len(polygon) < 3 or not np.isfinite(polygon).all():
            raise ValueError('Search anchor needs a finite observed polygon.')
        # Translate before shoelace arithmetic to preserve precision at large XY.
        origin = polygon[0]
        p = polygon - origin
        q = np.roll(p, -1, axis=0)
        cross = p[:, 0] * q[:, 1] - q[:, 0] * p[:, 1]
        area2 = float(cross.sum())
        if abs(area2) < 1e-9:
            raise ValueError('Search anchor has zero area.')
        centre = origin + ((p + q) * cross[:, None]).sum(axis=0) / (3 * area2)
        identifier = item.get('id', item.get('chip_id'))
        if identifier is not None and identifier in identifiers:
            raise ValueError('Duplicate search chip identifier.')
        if any(np.linalg.norm(centre - old) < 1. for old in centres):
            raise ValueError('Duplicate search chip centre.')
        uncertainty = 0. if measured else float(outline.get('uncertainty_um', float('nan')))
        if not math.isfinite(uncertainty) or uncertainty < 0:
            raise ValueError('Estimated rectangle needs finite nonnegative uncertainty.')
        direction = polygon[1]-polygon[0]
        orientation = float((outline.get('orientation') or {}).get('orientation_deg',
                            math.degrees(math.atan2(direction[1], direction[0]))))
        if not math.isfinite(orientation):
            raise ValueError('Anchor orientation proposal must be finite.')
        centres.append(centre)
        identifiers.append(identifier)
        evidence.append({'id': identifier, 'centre_um': centre.tolist(),
                         'kind': 'measured_enclosure' if measured else 'estimated_rectangle_search_anchor',
                         'measured_extent': measured, 'uncertainty_um': uncertainty,
                         'orientation_proposal_deg': orientation,
                         'orientation_qualification': 'Chip-side direction is a search proposal, not measured grid rotation.'})
    return np.asarray(centres, dtype=float).reshape((-1, 2)), identifiers, evidence


def fit_grid_prior(observations, options=None, *, misses=()):
    """Fit bounded similarity-grid hypotheses to measured polygon centroids.

    Enumerate assignments of the farthest observed pair to grid sites, then
    refine translation, rotation and one shared spacing against all anchors.
    This is a heuristic for a roughly regular grid, not an exhaustive solver
    for arbitrary per-chip displacements. Retained ambiguity is explicit;
    truncation never changes the baseline search or validates a chip.
    """
    options = options if isinstance(options, GridPriorOptions) else GridPriorOptions(**(options or {}))
    centres, identifiers, anchors = _search_anchors(observations)
    result = {'schema': 1, 'options': asdict(options), 'anchor_count': len(centres),
              'anchor_ids': identifiers, 'anchor_centres_um': centres.tolist(),
              'anchors': anchors, 'measured_anchor_count': sum(a['measured_extent'] for a in anchors),
              'search_anchor_count': len(anchors),
              'status': 'insufficient_evidence', 'hypotheses': [],
              'candidate_centres_um': [], 'all_chips_found': False,
              'qualification': 'Grid candidates are unverified search priorities; retain every baseline lattice sample.',
              'search_bounds_policy': 'Explicit physical search envelope required; arbitrary starting XY does not locate the grid.'}
    if not len(centres):
        return result
    if len(centres) > options.rows * options.columns:
        result['status'] = 'inconsistent'
        return result
    grid = np.asarray([complex(x, y) for y in range(options.rows) for x in range(options.columns)])
    observed = centres[:, 0] + 1j * centres[:, 1]
    if len(centres) == 1:
        theta = math.radians(anchors[0]['orientation_proposal_deg'])
        scale = options.spacing_um * np.exp(1j*theta)
        # Retain every possible first-chip index. There is no claim that its
        # row/column, the holder centre, or grid orientation has been measured.
        for slot in range(len(grid)):
            origin = observed[0]-scale*grid[slot]
            sites = origin+scale*grid
            xy = np.column_stack((sites.real, sites.imag))
            result['hypotheses'].append({'origin_um':[float(origin.real),float(origin.imag)],
                'rotation_deg':math.degrees(theta), 'spacing_um':options.spacing_um,
                'site_centres_um':xy.tolist(), 'candidate_centres_um':[p.tolist() for i,p in enumerate(xy) if i!=slot],
                'anchor_site_indices':[slot], 'first_chip_slot':[slot//options.columns,slot%options.columns],
                'max_residual_um':0., 'rms_residual_um':0., 'grid_rotation_measured':False})
        result.update(status='one_anchor_proposals', hypothesis_count_before_limit=len(grid),
                      hypotheses_truncated=False, grid_orientation_status='unmeasured',
                      orientation_uncertainty_deg=90.,
                      orientation_qualification='All grid rotations remain possible; chip orientation only orders early neighbor probes.')
        # One-anchor placement ambiguity has at most 64 entries and is kept in
        # full even when the fitted-hypothesis budget is smaller.
        return _candidate_evidence(result, options, misses, angular_neighbors=True)
    distances = abs(observed[:, None] - observed[None, :])
    a, b = np.unravel_index(np.argmax(distances), distances.shape)
    low = options.spacing_um * (1 - options.spacing_tolerance_fraction)
    high = options.spacing_um * (1 + options.spacing_tolerance_fraction)
    hypotheses = {}
    for first in range(len(grid)):
        for second in range(len(grid)):
            if first == second:
                continue
            scale = (observed[b] - observed[a]) / (grid[second] - grid[first])
            if not low <= abs(scale) <= high:
                continue
            origin = observed[a] - scale * grid[first]
            for _ in range(3):
                sites = origin + scale * grid
                assignment = np.argmin(abs(observed[:, None] - sites[None, :]), axis=1)
                if len(set(assignment.tolist())) != len(observed):
                    break
                assigned = grid[assignment]
                centred_grid = assigned - assigned.mean()
                denominator = float(np.sum(abs(centred_grid) ** 2))
                scale = np.sum(np.conj(centred_grid) * (observed - observed.mean())) / denominator
                origin = observed.mean() - scale * assigned.mean()
            if len(set(assignment.tolist())) != len(observed) or not low <= abs(scale) <= high:
                continue
            sites = origin + scale * grid
            residuals = abs(observed - sites[assignment])
            if np.any(residuals > options.max_residual_um + np.asarray([a['uncertainty_um'] for a in anchors])):
                continue
            xy = np.column_stack((sites.real, sites.imag))
            # Equivalent rotations/reflections of square-grid indexing have
            # identical physical sites; do not count them as distinct layouts.
            key = tuple(sorted((round(float(x), 1), round(float(y), 1)) for x, y in xy))
            candidates = [point.tolist() for i, point in enumerate(xy) if i not in assignment]
            hypothesis = {'origin_um': [float(origin.real), float(origin.imag)],
                          'rotation_deg': math.degrees(float(np.angle(scale))),
                          'spacing_um': float(abs(scale)), 'site_centres_um': xy.tolist(),
                          'candidate_centres_um': candidates, 'anchor_site_indices': assignment.tolist(),
                          'max_residual_um': float(residuals.max()),
                          'rms_residual_um': float(np.sqrt(np.mean(residuals ** 2)))}
            if key not in hypotheses or hypothesis['rms_residual_um'] < hypotheses[key]['rms_residual_um']:
                hypotheses[key] = hypothesis
    ranked = sorted(hypotheses.values(), key=lambda h: (round(h['rms_residual_um'], 6),
                    abs(h['spacing_um'] - options.spacing_um), h['origin_um']))
    result['hypothesis_count_before_limit'] = len(ranked)
    result['hypotheses_truncated'] = len(ranked) > options.max_hypotheses
    result['hypotheses'] = ranked[:options.max_hypotheses]
    result['status'] = 'inconsistent' if not ranked else 'supported' if len(ranked) == 1 else 'ambiguous'
    result['grid_orientation_status'] = 'supported_from_centres' if len(ranked)==1 else 'ambiguous_from_centres'
    return _candidate_evidence(result, options, misses)


def _candidate_evidence(result, options, misses, angular_neighbors=False):
    """Attach local-neighbor support and soft, corroborated miss penalties."""
    candidates = {}
    anchors = np.asarray(result['anchor_centres_um'])
    uncertainty = max(a['uncertainty_um'] for a in result['anchors'])
    for hypothesis in result['hypotheses']:
        pitch = hypothesis['spacing_um']
        for point in hypothesis['candidate_centres_um']:
            key = tuple(round(v, 1) for v in point)
            distance = float(np.min(np.linalg.norm(anchors-point, axis=1)))
            item = candidates.setdefault(key, {'xy_um':point, 'hypothesis_support':0,
                'neighbor_steps':max(1, int(math.ceil(distance/pitch-.15))),
                'orientation_probe_offset_deg':0.,
                'uncertainty_um':uncertainty+options.spacing_um*options.spacing_tolerance_fraction
                                   if len(anchors)==1 else uncertainty+options.max_residual_um,
                'kind':'unverified_grid_site'})
            item['hypothesis_support'] += 1
    if angular_neighbors:
        # Cover the unknown angular relation with nearby proposals on the first
        # neighbor ring. They do not become additional placement hypotheses or
        # imply that any rotation is excluded. The baseline lattice remains.
        theta = math.radians(result['anchors'][0]['orientation_proposal_deg'])
        for delta in (-30.,-15.,15.,30.,45.):
            for quarter in range(4):
                angle = theta+math.radians(delta)+quarter*math.pi/2
                point = (anchors[0]+options.spacing_um*np.array([math.cos(angle),math.sin(angle)])).tolist()
                key = tuple(round(v,1) for v in point)
                candidates.setdefault(key, {'xy_um':point,'hypothesis_support':0,'neighbor_steps':1,
                    'orientation_probe_offset_deg':abs(delta),'uncertainty_um':uncertainty+options.spacing_um*options.spacing_tolerance_fraction,
                    'kind':'unverified_orientation_neighbor_probe'})
    miss_points=[]
    for record in misses:
        if not isinstance(record,dict) or record.get('classification')!='non_substrate_sample':
            continue
        point=record.get('position_um') or record.get('requested_xy_um')
        if isinstance(point,dict):point=[point.get('x'),point.get('y')]
        if not isinstance(point,(list,tuple)) or len(point)<2:
            continue
        point=np.asarray(point[:2],float)
        if np.isfinite(point).all():miss_points.append(point)
    for item in candidates.values():
        local=[]
        for point in miss_points:
            if np.linalg.norm(point-item['xy_um'])>options.minimum_chip_diameter_um/2:
                continue
            if all(np.linalg.norm(point-old)>=min(500.,options.spacing_um*.05) for old in local):
                local.append(point)
        item['corroborating_local_misses']=len(local)
        item['miss_penalty']=min(3.,max(0,len(local)-1)*.75)
        item['miss_qualification']='Separated nearby non-substrate samples change priority only; no site or hypothesis is removed.'
    result['candidate_sites']=list(candidates.values())
    result['candidate_centres_um']=[item['xy_um'] for item in candidates.values()]
    result['miss_policy']='One isolated miss has no exclusion effect; repeated separated local misses only reduce priority.'
    return result


def grid_probe_order(plan, pending, current_xy, fit, *, priority_streak=0):
    """Prioritize existing probes near predicted sites; never remove a probe.

    Candidates outside the explicit envelope cannot expand it. Proposals need
    normal image classification, boundary measurement and focus verification.
    """
    current = np.asarray(current_xy, dtype=float)
    if current.shape != (2,) or not np.isfinite(current).all():
        raise ValueError('Current XY must be a finite pair.')
    points = np.asarray(plan['probe_centres_um'], dtype=float)
    indices = sorted(set(pending))
    if any(type(i) is not int or not 0 <= i < len(points) for i in indices):
        raise ValueError('Pending probe index is outside the plan.')
    options=GridPriorOptions(**fit.get('options',{}))
    baseline=sorted(indices,key=lambda i:(float(np.sum((points[i]-current)**2)),i))
    if priority_streak >= options.max_prior_probes or not indices:
        return baseline
    candidates=fit.get('candidate_sites') or [dict(xy_um=p,neighbor_steps=1,miss_penalty=0.,
                    orientation_probe_offset_deg=0.,hypothesis_support=0) for p in fit.get('candidate_centres_um',[])]
    bounds = plan.get('bounds_um')
    if bounds and candidates:
        x0, x1, y0, y1 = bounds
        candidates=[item for item in candidates if x0<=item['xy_um'][0]<=x1 and y0<=item['xy_um'][1]<=y1]
    ranked={}
    for item in candidates:
        target=np.asarray(item['xy_um'])
        index=min(indices,key=lambda i:(float(np.sum((points[i]-target)**2)),i))
        error=float(np.linalg.norm(points[index]-target))
        # Do not pull a distant remaining baseline sample toward a candidate
        # whose neighborhood has already been sampled or lies outside bounds.
        if error>options.minimum_chip_diameter_um/2:
            continue
        travel=float(np.linalg.norm(points[index]-current))/options.spacing_um
        score=(item.get('neighbor_steps',1),
               travel+.5*error/options.spacing_um+item.get('miss_penalty',0.)+
               .3*item.get('orientation_probe_offset_deg',0.)/45.,
               -item.get('hypothesis_support',0), index)
        if index not in ranked or score<ranked[index]:ranked[index]=score
    priority=sorted(ranked,key=ranked.get)[:options.max_prior_probes]
    chosen=set(priority)
    return priority+[i for i in baseline if i not in chosen]
