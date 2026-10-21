"""Pure search-order hints from an uncertain, translated and rotated chip grid.

Only measured enclosure polygons anchor the fit. Predicted sites are search
proposals, never chip detections or permission to omit the ordinary lattice.
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

    def __post_init__(self):
        for key in ('rows', 'columns', 'max_hypotheses'):
            value = getattr(self, key)
            if type(value) is not int or not 1 <= value <= 64:
                raise ValueError(key + ' must be an integer from 1 to 64.')
        if not 2 <= self.rows * self.columns <= 64:
            raise ValueError('Grid must contain 2 to 64 sites.')
        if not np.isfinite([self.spacing_um, self.spacing_tolerance_fraction,
                            self.max_residual_um]).all():
            raise ValueError('Grid parameters must be finite.')
        if self.spacing_um <= 0 or self.max_residual_um <= 0:
            raise ValueError('Grid spacing and residual tolerance must be positive.')
        if not 0 <= self.spacing_tolerance_fraction < .5:
            raise ValueError('Spacing tolerance must be between zero and one half.')


def _measured_centres(observations):
    centres, identifiers = [], []
    for item in observations:
        if item.get('boundary_status') != 'measured_enclosure':
            continue
        polygon = np.asarray(item.get('stage_polygon_um', []), dtype=float)
        if polygon.ndim != 2 or polygon.shape[1] != 2 or len(polygon) < 3 or not np.isfinite(polygon).all():
            raise ValueError('Measured enclosure needs a finite polygon.')
        # Translate before shoelace arithmetic to preserve precision at large XY.
        origin = polygon[0]
        p = polygon - origin
        q = np.roll(p, -1, axis=0)
        cross = p[:, 0] * q[:, 1] - q[:, 0] * p[:, 1]
        area2 = float(cross.sum())
        if abs(area2) < 1e-9:
            raise ValueError('Measured enclosure has zero area.')
        centre = origin + ((p + q) * cross[:, None]).sum(axis=0) / (3 * area2)
        identifier = item.get('id', item.get('chip_id'))
        if identifier is not None and identifier in identifiers:
            raise ValueError('Duplicate measured chip identifier.')
        if any(np.linalg.norm(centre - old) < 1. for old in centres):
            raise ValueError('Duplicate measured chip centre.')
        centres.append(centre)
        identifiers.append(identifier)
    return np.asarray(centres, dtype=float).reshape((-1, 2)), identifiers


def fit_grid_prior(observations, options=None):
    """Fit bounded similarity-grid hypotheses to measured polygon centroids.

    Enumerate assignments of the farthest observed pair to grid sites, then
    refine translation, rotation and one shared spacing against all anchors.
    This is a heuristic for a roughly regular grid, not an exhaustive solver
    for arbitrary per-chip displacements. Retained ambiguity is explicit;
    truncation never changes the baseline search or validates a chip.
    """
    options = options if isinstance(options, GridPriorOptions) else GridPriorOptions(**(options or {}))
    centres, identifiers = _measured_centres(observations)
    result = {'schema': 1, 'options': asdict(options), 'anchor_count': len(centres),
              'anchor_ids': identifiers, 'anchor_centres_um': centres.tolist(),
              'status': 'insufficient_evidence', 'hypotheses': [],
              'candidate_centres_um': [], 'all_chips_found': False,
              'qualification': 'Grid candidates are unverified search priorities; retain every baseline lattice sample.',
              'search_bounds_policy': 'Explicit physical search envelope required; arbitrary starting XY does not locate the grid.'}
    if len(centres) < 2:
        return result
    if len(centres) > options.rows * options.columns:
        result['status'] = 'inconsistent'
        return result
    grid = np.asarray([complex(x, y) for y in range(options.rows) for x in range(options.columns)])
    observed = centres[:, 0] + 1j * centres[:, 1]
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
            if float(residuals.max()) > options.max_residual_um:
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
    candidates = {}
    for hypothesis in result['hypotheses']:
        for point in hypothesis['candidate_centres_um']:
            candidates[tuple(round(v, 1) for v in point)] = point
    result['candidate_centres_um'] = list(candidates.values())
    return result


def grid_probe_order(plan, pending, current_xy, fit):
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
    candidates = np.asarray(fit.get('candidate_centres_um', []), dtype=float).reshape((-1, 2))
    bounds = plan.get('bounds_um')
    if bounds and len(candidates):
        x0, x1, y0, y1 = bounds
        candidates = candidates[(candidates[:, 0] >= x0) & (candidates[:, 0] <= x1) &
                                (candidates[:, 1] >= y0) & (candidates[:, 1] <= y1)]
    def key(index):
        distance = float(np.sum((points[index] - current) ** 2))
        if not len(candidates):
            return (distance, index)
        priority = float(np.min(np.sum((candidates - points[index]) ** 2, axis=1)))
        return (priority, distance, index)
    return sorted(indices, key=key)
