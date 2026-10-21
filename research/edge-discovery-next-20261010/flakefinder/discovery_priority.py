"""Pure search ordering hints; no boundary, connectedness or coverage claim."""
import math
from statistics import median


def interior_reseed(discovery, centres, pending, *, minimum_shift_um):
    """Prioritize one existing lattice sample after an observed open ray hits bounds.

    Only a pending lattice point can be returned. The normal campaign must still
    acquire and classify it. Coarse substrate observations can cross gaps, so this
    is explicitly a search-order heuristic and cannot suppress any other sample.
    """
    seed = discovery.get('seed_xy_um', discovery.get('seed_um'))
    if not seed or len(seed) != 2 or not pending:
        return None
    bounds = discovery.get('options', {}).get('search_bounds_um')
    if not bounds or len(bounds) != 4:
        return None
    exhausted = False
    for ray in discovery.get('rays', {}).values():
        if ray.get('status') != 'unresolved' or ray.get('first_holder_distance_um') is not None:
            continue
        direction = ray.get('direction', [])
        if len(direction) != 2:
            continue
        maximum = min(((bounds[2*i+(1 if direction[i]>0 else 0)]-seed[i])/direction[i]
                       for i in range(2) if abs(direction[i]) > 1e-9), default=0)
        if maximum > 0 and ray.get('last_substrate_distance_um', 0) >= maximum - .5:
            exhausted = True
    if not exhausted:
        return None
    points = [r['position_um'][:2] for r in discovery.get('records', [])
              if r.get('classification') == 'substrate' and len(r.get('position_um', [])) >= 2]
    if len(points) < 3 or not all(math.isfinite(v) for p in points for v in p):
        return None
    target = [median(p[i] for p in points) for i in range(2)]
    index = min(pending, key=lambda j: (sum((centres[j][i]-target[i])**2 for i in range(2)),j))
    xy = centres[index]
    if math.dist(seed, xy) < minimum_shift_um:
        return None
    return {'index': int(index), 'requested_xy_um': list(xy), 'observed_substrate_median_um': target,
            'source_record_count': len(points), 'method': 'observed_substrate_extent_search_order_v1',
            'qualification': 'Unverified next lattice sample; no connected-chip, boundary, or coverage claim.'}
