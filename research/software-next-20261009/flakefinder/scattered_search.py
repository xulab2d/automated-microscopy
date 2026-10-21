"""Deterministic spatial exploration over a certified finite seed lattice.

This changes order, never the lattice's geometric guarantee or probe membership.
The minimum size means an unoccluded inscribed disk, not arbitrary caliper width.
No number or arrangement of substrate points establishes separate chips.
"""
from collections import deque
import hashlib
import json

import cv2
import numpy as np


def coverage_order(points):
    """Breadth-first spatial bisection: spread early probes, then fill every gap.

    Each node emits its centre-nearest point and bisects the remaining points
    along the wider coordinate span. Stable index tie breaks make serialization
    and interrupted continuation reproducible. O(N log² N), O(N) storage.
    """
    points=np.asarray(points,float)
    if points.ndim!=2 or points.shape[1]!=2 or not np.isfinite(points).all():
        raise ValueError('Finite XY probe centres required.')
    queue=deque([np.arange(len(points))]);order=[]
    while queue:
        indices=queue.popleft()
        if not len(indices):continue
        local=points[indices];centre=(local.min(axis=0)+local.max(axis=0))/2
        position=int(np.argmin(np.sum((local-centre)**2,axis=1)))
        order.append(int(indices[position]));indices=np.delete(indices,position)
        if len(indices):
            axis=int(np.argmax(np.ptp(points[indices],axis=0)))
            indices=indices[np.lexsort((indices,points[indices,axis]))]
            middle=(len(indices)+1)//2;queue.extend((indices[:middle],indices[middle:]))
    return order


def attach_scattered_plan(plan):
    """Persist the complete fallback order and explicit adaptive-order policy."""
    order=coverage_order(plan['probe_centres_um'])
    policy={'algorithm':'spatial_bisection_frontier_v1','frontier_probes':8,
            'distance_scale_um':float(plan['minimum_inscribed_diameter_um']),
            'estimated_outline_weight':.25,
            'qualification':'Ordering only: estimates and isolated misses never remove probes; no point-grouping chip inference.'}
    fingerprint=hashlib.sha256(json.dumps({'points':plan['probe_centres_um'],'order':order,'policy':policy},
                                        sort_keys=True,separators=(',',':')).encode()).hexdigest()
    return {**plan,'coverage_order':order,'scattered_policy':policy,'scattered_plan_sha256':fingerprint,
            'ordering':'Spatially dispersed coarse-to-fine probes, adapting a bounded frontier to travel and prior observations; exhaustive finite lattice fallback.'}


def next_scattered_probe(plan,pending,current_xy,*,observations=(),estimated_outlines=()):
    """Choose one pending index; never infer absence or merge substrate points.

    A frontier of eight earliest remaining spatial-order probes prevents distant
    speculative jumps and starvation. Novelty rewards distance from previously
    sampled XY; travel divides that reward by 1 + distance/minimum diameter.
    Estimated interiors are downweighted, never excluded. Even all misses or a
    wholly wrong outline leave the complete finite fallback reachable.
    """
    current=np.asarray(current_xy,float)
    if current.shape!=(2,) or not np.isfinite(current).all():raise ValueError('Finite current XY required.')
    remaining=set(pending)
    if not remaining:raise ValueError('No pending seed probes.')
    if any(type(i) is not int or not 0<=i<plan['probe_count'] for i in remaining):raise ValueError('Invalid pending probe index.')
    order=plan['coverage_order'];policy=plan['scattered_policy']
    frontier=[]
    for i in order:
        if i in remaining:frontier.append(i)
        if len(frontier)>=policy['frontier_probes']:break
    if not frontier:raise ValueError('Saved coverage order does not cover remaining probes.')
    scale=policy['distance_scale_um'];points=np.asarray(plan['probe_centres_um'],float)
    sampled=[]
    for observation in observations:
        xy=observation.get('requested_xy_um')
        if xy is not None and np.asarray(xy).shape==(2,) and np.isfinite(xy).all():sampled.append(xy)
    sampled=np.asarray(sampled or [current],float)
    polygons=[]
    for polygon in estimated_outlines:
        p=np.asarray(polygon,np.float32)
        if p.ndim==2 and p.shape[1]==2 and len(p)>=3 and np.isfinite(p).all():polygons.append(p)
    scores=[]
    for rank,index in enumerate(frontier):
        xy=points[index];travel=float(np.linalg.norm(xy-current))
        novelty=float(np.min(np.linalg.norm(sampled-xy,axis=1)))
        inside=any(cv2.pointPolygonTest(p,tuple(map(float,xy)),False)>0 for p in polygons)
        weight=policy['estimated_outline_weight'] if inside else 1.
        score=weight*(1+novelty/scale)/(1+travel/scale)
        scores.append((score,-travel,-rank,index,inside,novelty))
    score,negative_travel,_,index,inside,novelty=max(scores)
    return {'index':int(index),'method':policy['algorithm'],'frontier_size':len(frontier),
            'travel_um':-negative_travel,'nearest_sample_distance_um':novelty,
            'inside_estimated_outline':inside,'ordering_score':score,
            'plan_sha256':plan['scattered_plan_sha256'],
            'qualification':'Unverified seed proposal; order does not establish chip identity or exclude unobserved space.'}


def sampling_status(plan,pending,records,*,measured_count,observed_count,completed_count,expected_count=None,unfinished_discoveries=0):
    """Truthful geometry/count status independent of acquisition success."""
    ambiguous=sum(r.get('classification') not in ('substrate_seed','non_substrate_sample',
                  'skipped_inside_measured_chip','skipped_observed_connected_substrate') for r in records)
    exhaustive=not pending and not unfinished_discoveries
    if expected_count is not None and completed_count>=expected_count:status='completed_count_reached'
    elif expected_count is not None and observed_count>=expected_count:status='observed_count_reached_validation_or_scan_pending'
    elif exhaustive:status='sampling_exhausted_with_unresolved_evidence' if ambiguous else 'sampling_exhausted_target_unconfirmed'
    else:status='search_in_progress'
    return {'method':plan['scattered_policy']['algorithm'],'expected_chip_count':expected_count,
            'measured_distinct_chip_count':measured_count,'observed_distinct_chip_count':observed_count,
            'completed_distinct_chip_count':completed_count,
            'expected_chip_count_remaining':None if expected_count is None else max(0,expected_count-observed_count),
            'expected_completed_chip_count_remaining':None if expected_count is None else max(0,expected_count-completed_count),
            'scattered_target_status':status,'remaining_lattice_probes':len(pending),
            'sampling_complete':exhaustive,'remaining_boundary_attempts':unfinished_discoveries,
            'uncertain_seed_record_count':ambiguous,'all_chips_found':False,
            'coverage_qualification':'Finite inscribed-disk sampling within configured bounds; missed classification, occlusion, irregular narrow chips and boundary-crossing chips remain outside the guarantee.'}
