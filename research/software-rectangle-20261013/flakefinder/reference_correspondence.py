"""Frozen outside-ROI empirical correspondence; never a calibration claim.

The two-pixel premise is empirical. Every model/corner-derived raw crop must
pass the original score. No target pixels enter registration, no resampling.
"""
from pathlib import Path
import hashlib,json,math,gc
import cv2,numpy as np
from .model import AcquisitionError,SafetyError,atomic_json
from .scan_continuation import digest,read
from .memory_capacity import require,snapshot
POLICY_SHA='3bcdd6935cf656378dd890babaa34dc39fc20b1b340a2ab1aa4be04332376a8a'
# Additional allocation estimate, excluding already resident input arrays.
# Covers four raw decode/hash/control copies (~122MB), one native tile pyramid
# (~75MB), bounded4608^2 float distance workspace (~85MB), descriptors (~15MB)
# and ~100MB native/Python headroom. Measured additional peak174MB. Existing
# require adds25%; this is commit/address capacity, never a physical RAM floor.
WORKING_BYTES=384*1024**2
def capacity(check=lambda:None):
    check();value=require(snapshot(),WORKING_BYTES,purpose='bounded reference correspondence');check();return value
policy={'schema': 1, 'kind': 'outside_roi_rigid_field_empirical_v1', 'frozen_at': '2026-10-11T22:39:45.303728+00:00', 'authorization': 'Offline prototype/negative validation only; no production/hardware grant', 'supersedes': 'Only the arbitrary32px feature-hull DISTANCE rejection in prospective-correspondence-gate.txt; historical rejected policies/evidence remain immutable', 'estimator_files_sha256': {'v3-roi-aware-protocol.json': '1bb8ea4788b4c9980ed4c6f14fa4dab792d4cd75e1f419fb492916730a389c95', 'v3-roi-aware-geometry.json': '8a61dc250c3986c44f330c7e34616ff83be6f1b98e897e6ec35c4d4ba11e99ed', 'evaluate_v3_roi_aware_geometry.py': '73d9cb128584a3842a616f8b4318b5162e12f7f4cd4dfa9914d1fca09a33c2d5'}, 'scientific_premise': 'Same optics, rigid specimen/field, near-global similarity/affine image correspondence. Empirical image-registration envelope, not certified physical calibration or guaranteed1px/2pxaccuracy. Arbitrary deformation confined to excluded target cannot be ruled out from outside pixels.', 'fixed_estimator': 'The frozen ROI-aware all-block GREEN estimator and complete leave-block-out protocol; no alternative partitions, feature settings or channels chosen after focus scores', 'geometry_guards': {'input_translation_axis_px': 32, 'minimum_full_field_overlap': 0.97, 'max_scale_delta': 0.0005, 'max_rotation_deg': 0.02, 'max_roi_nontranslation_corner_px': 0.25, 'green_blue_max_corner_disagreement_px': 0.5, 'similarity_affine_max_corner_disagreement_px': 0.5, 'max_leave_block_corner_difference_px': 1, 'green_min_inliers': 50, 'blue_min_inliers': 25, 'min_train_consensus': 0.8, 'green_min_full_blocks': 4, 'blue_min_full_blocks': 3, 'min_delete_fit_blocks': 3, 'minimum_feature_hull_area_fraction': 0.1, 'maximum_hull_extrapolation_px': None, 'hull_extrapolation_policy': 'Record center AND all corner distances; account through prediction leverage; no fixed distance cutoff. All full/delete models must retain count,area,rank,consensus,block and stability requirements.', 'green_min_validation_matches': 30, 'blue_min_validation_matches': 20, 'min_validation_fraction_within_2px': 0.9, 'max_validation_median_px': 0.75, 'max_validation_p90_px': 2, 'minimum_supported_validation_blocks': 3, 'supported_block_min_compatible_matches': 3, 'supported_block_min_compatible_fraction': 0.8, 'max_one_block_fraction_of_compatible_validation': 0.75, 'competing_consensus_fraction_of_primary_reject': 0.25, 'finite_full_rank_model_required': True}, 'exclusion': {'max_target_displacement_px': 32, 'pixel_domains': 'Exactly frozen copied disjoint blocks outside originalROI expanded32px in BOTH images; arrays physically exclude target pixels', 'keypoint_margin': '6*size+32px inside copiedblock', 'features_per_block_channel': 256, 'max_features_per_channel': 4608, 'channel_order': 'sequential RGB,green primary,blue mandatory independent,red corroborative'}, 'envelope': {'effective_relative_coordinate_error_px_per_axis': 2, 'qualification': 'Prospective empirical engineering premise tested by negatives, not inferred as a guaranteed bound fromRANSAC cutoff or90%compatibility. A true localwarp may violate the premise without outside evidence.', 'model_family': 'G/Bfull similarity, EVERY supported G/Bdelete-block similarity, Gfullaffine diagnostic; no favorable subset', 'continuous_intervals': 'At each targetcorner/model: displacement plus/minus2times componentwiseL1norm of least-squares prediction weights; union across allcorners/models', 'quantization': 'Allintegerk whose[k-0.5,k+0.5]cell intersects the continuousinterval; form Cartesianproduct of x/y ranges', 'no_clipping': 'Reject if any derivedcrop falls outside rawbounds or if any possible displacement exceeds32px/axis; never shrink uncertaintyset', 'geometry_frozen_before_score': True, 'fixture_only_crop_set': '121shifts,x=-16..-6,y=4..14; derived from this saved raw only. Future frames independently recompute same frozen estimator/envelope; never hardcode its shift/crops.'}, 'scientific_gate': {'raw_roi_size_px': [370, 370], 'metric': 'Unchanged productionfocus_metrics(mode=chip_edge)', 'minimum_original_score_fraction': 0.95, 'minimum_texture': 0.015, 'aggregation': 'EVERY geometrically possible integercrop mustpass; reportminimum,notmaximum. No interpolation,sharpening,maskchange or normalizedmetricchange.', 'controls': 'Unchanged four hash-pinned chip/backing controls on unmodified freshfullframe,plusgenericreferencequality', 'fresh_frames': 2, 'fresh_frame_shift_agreement_axis_px': 1, 'both_required': 'Both independentstationaryfreshframes bind exactraw/metadata/vendor receipts and independently pass full geometry/envelope/allcrops/controls; compare each to originalpin', 'measured_pose': 'Same verified XY/optics/nativeframe and current independently measuredrefocusZ; no oldZrestoration', 'failure': 'No correction/geometry/progress/campaign acceptance on anyfailure; no promotion of previousmeasurement_onlyproof'}, 'resources': {'max_seconds': 600, 'max_captures': 64, 'scope': 'All captures,featureextraction,registration,control/scoreverification and finalreceipt validation in one sharedbudget. Nativeincremental commit/address guard; no physicalRAMfloor. Actualcapture count once; coordinate crops are not captures.', 'memory': 'Acquisition profiles frozenimplementation; no blanket384MiB certification by thisrole'}, 'required_validation_order': ['Freeze thispolicy and source/protocol hashes', 'Coordinate-level geometry failures; actual-imagewrongfield/ambiguity/blank/straightedge/nonrigidoutsidewarp negatives', 'Target-onlyblur with sharp outside landmarks must fail unchangedfocus check', 'Only then evaluate ALL frozen121rawcrops on savedrealfixture', 'Independent review of evidence and exactsource before any fresh-frame/hardwaregrant'], 'limitations': 'All-invisible target-only geometric warp cannot be guaranteed detectable by this outsideROI estimator; test and state that limit rather than claiming universalwarp rejection.'}
BOX=np.array([1051, 1585, 1421, 1955]);EX=np.array([1019, 1553, 1453, 1987]);CENTER=(BOX[:2]+BOX[2:])/2
CORNERS=np.array([[BOX[0],BOX[1]],[BOX[2],BOX[1]],[BOX[0],BOX[3]],[BOX[2],BOX[3]]],float);H,W=2056,2464
blocks=[{'id': 'top-0-0', 'box': [0, 0, 616, 517]}, {'id': 'top-1-0', 'box': [616, 0, 1232, 517]}, {'id': 'top-2-0', 'box': [1232, 0, 1848, 517]}, {'id': 'top-3-0', 'box': [1848, 0, 2464, 517]}, {'id': 'top-0-1', 'box': [0, 517, 616, 1035]}, {'id': 'top-1-1', 'box': [616, 517, 1232, 1035]}, {'id': 'top-2-1', 'box': [1232, 517, 1848, 1035]}, {'id': 'top-3-1', 'box': [1848, 517, 2464, 1035]}, {'id': 'top-0-2', 'box': [0, 1035, 616, 1553]}, {'id': 'top-1-2', 'box': [616, 1035, 1232, 1553]}, {'id': 'top-2-2', 'box': [1232, 1035, 1848, 1553]}, {'id': 'top-3-2', 'box': [1848, 1035, 2464, 1553]}, {'id': 'left-0', 'box': [0, 1553, 339, 1987]}, {'id': 'left-1', 'box': [339, 1553, 679, 1987]}, {'id': 'left-2', 'box': [679, 1553, 1019, 1987]}, {'id': 'right-0', 'box': [1453, 1553, 1790, 1987]}, {'id': 'right-1', 'box': [1790, 1553, 2127, 1987]}, {'id': 'right-2', 'box': [2127, 1553, 2464, 1987]}]
def predict(M, p):
    return p @ M[:, :2].T + M[:, 2]

def design(p, affine=False):
    n = len(p)
    if affine:
        A = np.zeros((2 * n, 6))
        A[::2, :3] = np.column_stack([p, np.ones(n)])
        A[1::2, 3:] = np.column_stack([p, np.ones(n)])
    else:
        A = np.zeros((2 * n, 4))
        A[::2, 0] = p[:, 0]
        A[::2, 1] = -p[:, 1]
        A[::2, 2] = 1
        A[1::2, 0] = p[:, 1]
        A[1::2, 1] = p[:, 0]
        A[1::2, 3] = 1
    return A

def matrix(beta, affine=False):
    if affine:
        return beta.reshape(2, 3)
    a, b, x, y = beta
    return np.array([[a, -b, x], [b, a, y]])

def model(records, minimum, affine=False):
    p = np.array([a['prior']['xy'] for a in records])
    q = np.array([a['fresh']['xy'] for a in records])
    cv2.setRNGSeed(12345)
    M, keep = cv2.estimateAffinePartial2D(p, q, method=cv2.RANSAC, ransacReprojThreshold=2, maxIters=4000, confidence=0.999, refineIters=30)
    keep = keep.ravel().astype(bool)
    pp = p[keep]
    qq = q[keep]
    A = design(pp, affine)
    pinv = np.linalg.pinv(A)
    M = matrix(pinv @ qq.reshape(-1), affine)
    weights = design(CORNERS, affine) @ pinv
    leverage = np.sum(np.abs(weights), axis=1).reshape(4, 2)
    hull = cv2.convexHull(pp.astype(np.float32))
    area = float(cv2.contourArea(hull) / (W * H))
    outside = max(0, -cv2.pointPolygonTest(hull, tuple(CENTER), True))
    tiles = sorted({r['prior']['block'] for r, k in zip(records, keep) if k})
    center = predict(M, CENTER) - CENTER
    corners = predict(M, CORNERS) - CORNERS
    bad = []
    if len(pp) < minimum:
        bad.append('count')
    if keep.mean() < 0.8:
        bad.append('consensus')
    if area < 0.1:
        bad.append('hull_area')
    if outside > 32:
        bad.append('roi_center_extrapolation')
    if len(tiles) < 3:
        bad.append('block_count')
    return {'matrix': M.tolist(), 'inliers': int(keep.sum()), 'count': len(records), 'consensus': float(keep.mean()), 'support_blocks': tiles, 'hull_area_fraction': area, 'roi_center_extrapolation_px': outside, 'roi_corner_extrapolation_px': [max(0, -cv2.pointPolygonTest(hull, tuple(c), True)) for c in CORNERS], 'roi_center_shift': center.tolist(), 'corner_shifts': corners.tolist(), 'prediction_L1_leverage': leverage.tolist(), 'scale': float(np.hypot(M[0, 0], M[1, 0])), 'rotation_deg': float(np.degrees(np.arctan2(M[1, 0], M[0, 0]))), 'maximum_nontranslation_corner': float(np.max(np.linalg.norm(corners - center, axis=1))), 'rejected_support_reasons': bad, 'inlier_indices': np.flatnonzero(keep).tolist()}

def extract(raw, check=lambda: None):
    assert raw.shape == (H, W, 3) and raw.dtype == np.uint16
    sift = cv2.SIFT_create(nfeatures=256, contrastThreshold=0.008, edgeThreshold=10)
    channels = []
    for c in [2, 1, 0]:
        features = []
        des = []
        for block in blocks:
            check()
            x, y, x1, y1 = block['box']
            work = np.right_shift(raw[y:y1, x:x1, c], 4)
            np.minimum(work, 255, out=work)
            g = np.uint8(work)
            del work
            kp, desc = sift.detectAndCompute(g, None)
            check()
            if desc is not None:
                ids = sorted(range(len(kp)), key=lambda j: (-kp[j].response, *kp[j].pt, kp[j].size, kp[j].angle))[:256]
                for j in ids:
                    k = kp[j]
                    u, v = k.pt
                    r = 6 * k.size + 32
                    if r < u < x1 - x - r and r < v < y1 - y - r:
                        features.append({'xy': [x + u, y + v], 'size': k.size, 'angle': k.angle, 'block': block['id'], 'box': block['box'], 'local_support_margin': r})
                        des.append(desc[j])
            del g, kp, desc
            gc.collect()
        channels.append((features, np.asarray(des, np.float32)))
    return channels

def geometry(a, b, check=lambda: None):
    if any((len(k) < 4 for k, d in [*a, *b])):
        raise ValueError('insufficient feature descriptors')
    matcher = cv2.BFMatcher()
    channels = []
    for c, name in enumerate(['red', 'green', 'blue']):
        check()
        k0, d0 = a[c]
        k1, d1 = b[c]
        aa = matcher.knnMatch(d0, d1, k=2)
        bb = matcher.knnMatch(d1, d0, k=2)
        back = {m.queryIdx: (m.trainIdx, m.distance / n.distance) for m, n in bb if m.distance < 0.75 * n.distance}
        matches = []
        seen = set()
        for m, n in aa:
            if m.distance >= 0.75 * n.distance or m.trainIdx not in back or back[m.trainIdx][0] != m.queryIdx:
                continue
            x, y = (k0[m.queryIdx], k1[m.trainIdx])
            key = tuple(np.rint(np.array(x['xy']) / 2).astype(int))
            if key in seen:
                continue
            seen.add(key)
            matches.append({'prior': x, 'fresh': y, 'ratio_forward': m.distance / n.distance, 'ratio_reverse': back[m.trainIdx][1]})
        item = {'channel': name, 'feature_counts': [len(k0), len(k1)], 'mutual_matches': len(matches), 'correspondences': matches}
        if name == 'red':
            channels.append(item)
            continue
        minimum = 50 if name == 'green' else 25
        if len(matches) < minimum:
            raise ValueError(name + ' insufficient mutual matches')
        main = model(matches, minimum)
        folds = []
        allerr = []
        for block in sorted({m['prior']['block'] for m in matches}):
            check()
            train = [m for m in matches if m['prior']['block'] != block and m['fresh']['block'] != block]
            test = [m for m in matches if m['prior']['block'] == block]
            if len(train) < minimum:
                raise ValueError(name + ' insufficient deleted-block matches')
            fold = model(train, minimum)
            M = np.array(fold['matrix'])
            errors = []
            for m in test:
                p = np.array(m['prior']['xy'])
                q = np.array(m['fresh']['xy'])
                errors.append(float(np.linalg.norm(q - predict(M, p))))
            fold.update(omitted_block=block, heldout_count=len(test), heldout_errors_px=errors, heldout_cross_block=sum((m['prior']['block'] != m['fresh']['block'] for m in test)), heldout_fraction_within_2px=float(np.mean(np.array(errors) <= 2)), maximum_corner_change_from_full_px=float(np.max(np.linalg.norm(np.array(fold['corner_shifts']) - np.array(main['corner_shifts']), axis=1))))
            folds.append(fold)
            allerr.extend(errors)
        item.update(full=main, folds=folds, validation_count=len(allerr), validation_fraction_within_2px=float(np.mean(np.array(allerr) <= 2)), validation_median_p90_px=np.percentile(allerr, [50, 90]).tolist(), supported_validation_blocks=[f['omitted_block'] for f in folds if sum((e <= 2 for e in f['heldout_errors_px'])) >= 3 and f['heldout_fraction_within_2px'] >= 0.8])
        if name == 'green':
            item['full_affine_diagnostic'] = model(matches, minimum, True)
        channels.append(item)
    return finish_geometry(channels)

def finish_geometry(channels):
    green, blue = channels[1:]
    families = []
    for channel in [green, blue]:
        families.append((channel['channel'] + '-full', channel['full']))
        families.extend(((channel['channel'] + '-without-' + m['omitted_block'], m) for m in channel['folds']))
    families.append(('green-full-affine', green['full_affine_diagnostic']))
    limits = []
    for name, m in families:
        d = np.array(m['corner_shifts'])
        rad = 2 * np.array(m['prediction_L1_leverage'])
        limits.append({'name': name, 'low_xy': np.min(d - rad, axis=0).tolist(), 'high_xy': np.max(d + rad, axis=0).tolist()})
    lo = np.min([x['low_xy'] for x in limits], axis=0)
    hi = np.max([x['high_xy'] for x in limits], axis=0)
    minimum = np.ceil(lo - 0.5).astype(int)
    maximum = np.floor(hi + 0.5).astype(int)
    if np.any(maximum - minimum > 128):
        raise ValueError('unbounded uncertainty crop range')
    candidate = [[x, y] for x in range(minimum[0], maximum[0] + 1) for y in range(minimum[1], maximum[1] + 1)]
    report = {'channels': channels, 'envelope_family': limits, 'empirical_continuous_shift_interval_xy': [lo.tolist(), hi.tolist()], 'possible_integer_shift_range_xy': [minimum.tolist(), maximum.tolist()], 'possible_integer_shifts': candidate, 'possible_integer_shift_count': len(candidate)}
    report['geometry_rejections'] = gate(report)
    report['geometry_accepted'] = not report['geometry_rejections']
    return report

def gate(report):
    reasons = []
    g = policy['geometry_guards']
    channels = report['channels']
    green, blue = channels[1:]
    for c in [green, blue]:
        name = c['channel']
        main = c['full']
        minimum = g[name + '_min_inliers']
        for tag, m in [('full', main), *[(f['omitted_block'], f) for f in c['folds']]]:
            bad = [s for s in m['rejected_support_reasons'] if s != 'roi_center_extrapolation']
            if bad:
                reasons.append(name + '/' + tag + '/support:' + ','.join(bad))
            if not np.isfinite(np.array(m['matrix'])).all() or not np.isfinite(np.array(m['prediction_L1_leverage'])).all():
                reasons.append(name + '/' + tag + '/nonfinite')
            records = c['correspondences'] if tag == 'full' else [r for r in c['correspondences'] if r['prior']['block'] != tag and r['fresh']['block'] != tag]
            points = np.array([records[i]['prior']['xy'] for i in m['inlier_indices']], float)
            if not np.isfinite(points).all():
                reasons.append(name + '/' + tag + '/nonfinite_points')
            else:
                normalized = (points - CENTER) / np.array([W, H])
                for affine in (False, True):
                    A = design(normalized, affine)
                    condition = np.linalg.cond(A)
                    if np.linalg.matrix_rank(A) != A.shape[1] or not math.isfinite(condition) or condition > 1 / math.sqrt(np.finfo(float).eps):
                        reasons.append(name + '/' + tag + '/numerically_singular')
        if len(main['support_blocks']) < g[name + '_min_full_blocks']:
            reasons.append(name + '/full_block_count')
        if abs(main['scale'] - 1) > g['max_scale_delta']:
            reasons.append(name + '/scale')
        if abs(main['rotation_deg']) > g['max_rotation_deg']:
            reasons.append(name + '/rotation')
        if main['maximum_nontranslation_corner'] > g['max_roi_nontranslation_corner_px']:
            reasons.append(name + '/ROI_deformation')
        if np.max(np.abs(main['corner_shifts'])) > 32:
            reasons.append(name + '/translation')
        if any((f['maximum_corner_change_from_full_px'] > 1 for f in c['folds'])):
            reasons.append(name + '/block_instability')
        if c['validation_count'] < g[name + '_min_validation_matches'] or c['validation_fraction_within_2px'] < 0.9 or c['validation_median_p90_px'][0] > 0.75 or (c['validation_median_p90_px'][1] > 2):
            reasons.append(name + '/heldout_residual')
        if len(c['supported_validation_blocks']) < 3:
            reasons.append(name + '/heldout_regions')
        compatible = [sum((e <= 2 for e in f['heldout_errors_px'])) for f in c['folds']]
        if not sum(compatible) or max(compatible) / sum(compatible) > 0.75:
            reasons.append(name + '/dominant_validation_block')
        remaining = [r for i, r in enumerate(c['correspondences']) if i not in main['inlier_indices']]
        if len(remaining) >= max(3, math.ceil(0.25 * main['inliers'])):
            p = np.array([r['prior']['xy'] for r in remaining])
            q = np.array([r['fresh']['xy'] for r in remaining])
            cv2.setRNGSeed(12345)
            M, k = cv2.estimateAffinePartial2D(p, q, method=cv2.RANSAC, ransacReprojThreshold=2, maxIters=4000, confidence=0.999, refineIters=30)
            if M is not None and int(k.sum()) >= 0.25 * main['inliers'] and (np.max(np.linalg.norm(predict(M, CORNERS) - predict(np.array(main['matrix']), CORNERS), axis=1)) > 1):
                reasons.append(name + '/competing_consensus')
    if np.max(np.linalg.norm(np.array(green['full']['corner_shifts']) - np.array(blue['full']['corner_shifts']), axis=1)) > 0.5:
        reasons.append('channel_disagreement')
    if np.max(np.linalg.norm(np.array(green['full']['corner_shifts']) - np.array(green['full_affine_diagnostic']['corner_shifts']), axis=1)) > 0.5:
        reasons.append('affine_disagreement')
    corners = np.array([[0, 0], [W, 0], [W, H], [0, H]], np.float32)
    area, _ = cv2.intersectConvexConvex(corners, predict(np.array(green['full']['matrix']), corners).astype(np.float32))
    if area / (W * H) < 0.97:
        reasons.append('overlap')
    interval = np.array(report['empirical_continuous_shift_interval_xy'], float)
    if interval.shape != (2, 2) or not np.isfinite(interval).all() or np.any(interval[0] > interval[1]) or (np.max(np.abs(interval)) > 32):
        reasons.append('continuous_uncertainty_outside_translation_cap')
    for dx, dy in report['possible_integer_shifts']:
        x, y, x1, y1 = BOX + [dx, dy, dx, dy]
        if abs(dx) > 32 or abs(dy) > 32 or (not (0 <= x < x1 <= W and 0 <= y < y1 <= H)):
            reasons.append('uncertainty_crop_bounds')
            break
    return reasons
def register(prior,frame,check=lambda:None):
    capacity(check);cv2.setNumThreads(1)
    if prior.rgb.shape!=(H,W,3) or frame.rgb.shape!=(H,W,3) or prior.rgb.dtype!=np.uint16 or frame.rgb.dtype!=np.uint16:
        raise SafetyError('Correspondence requires the exact pinned uint16 full-frame dimensions.')
    try:
        # Production Frame is RGB; frozen extractor consumes OpenCV BGR.
        a=extract(prior.rgb[:,:,::-1],check);b=extract(frame.rgb[:,:,::-1],check)
        report=geometry(a,b,check);check()
    except (ValueError,TypeError,cv2.error,np.linalg.LinAlgError) as exc:
        raise AcquisitionError('Reference correspondence is unresolved: '+str(exc)) from exc
    if not report['geometry_accepted']:
        raise AcquisitionError('Reference correspondence rejected: '+', '.join(report['geometry_rejections']))
    return report

def lock_decision(prior,frame,prior_raw,raw,folder,label,check=lambda:None):
    report=register(prior,frame,check)
    proof={'schema':1,'kind':'frozen_outside_roi_empirical_decision_v1','policy_sha256':POLICY_SHA,
        'prior_raw_sha256':digest(prior_raw),'prior_metadata_sha256':digest(Path(prior_raw).with_suffix('.json')),
        'raw_sha256':digest(raw),'metadata_sha256':digest(Path(raw).with_suffix('.json')),
        'roi_box_px':BOX.tolist(),'geometry':report}
    path=Path(folder)/(label+'.json');check();atomic_json(path,proof);check()
    return proof,path

def validate_decision(prior,frame,prior_raw,raw,path,proof,check=lambda:None):
    if (proof!=read(path) or proof.get('policy_sha256')!=POLICY_SHA or proof.get('roi_box_px')!=BOX.tolist()
            or proof.get('prior_raw_sha256')!=digest(prior_raw) or proof.get('prior_metadata_sha256')!=digest(Path(prior_raw).with_suffix('.json'))
            or proof.get('raw_sha256')!=digest(raw) or proof.get('metadata_sha256')!=digest(Path(raw).with_suffix('.json'))):
        raise SafetyError('Correspondence decision input or locked proof changed.')
    # Independent raw replay prevents forged/pruned match/model/crop evidence.
    if register(prior,frame,check)!=proof['geometry']:raise SafetyError('Correspondence proof differs from independent raw replay.')
    return [[int(x+dx),int(y+dy),int(x1+dx),int(y1+dy)] for dx,dy in proof['geometry']['possible_integer_shifts'] for x,y,x1,y1 in [BOX]]
