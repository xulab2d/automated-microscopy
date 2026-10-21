"""Fresh-placement reference bootstrap using a measured bright-chip/dark-holder edge.

The caller owns and arms the microscope. No saved placement, focus height, or
substrate model enters this search. A search image is never a verified focus.
"""
from dataclasses import asdict, dataclass
from pathlib import Path
import math

import numpy as np
import cv2

from .acquisition_budget import AcquisitionBudget, BudgetScope
from .config import transform_from_config
from .full_chip import FocusROIAdapter, edge_focus_roi
from .focus_recovery import recover_focus
from .imaging import save_frame
from .layout import _context, resize_rgb_float32, SubstrateReference, classify_seed_patch
from .model import AcquisitionError, Cancelled, SafetyError, CameraTransform, Frame, atomic_json
from .photometric_edges import validate_photometric_edge
from .vendor_lifecycle import acknowledge_raw, file_hash


@dataclass(frozen=True)
class BootstrapOptions:
    max_seconds: float = 900.
    max_captures: int = 128
    max_search_fields: int = 64
    minimum_chip_um: float = 5000.
    focus_roi_um: float = 64.
    interior_margin_um: float = 60.
    max_xy_travel_um: float = 200000.

    def __post_init__(self):
        values = asdict(self)
        if any(not math.isfinite(v) or v <= 0 for v in values.values()):
            raise ValueError('Bootstrap limits must be finite and positive.')
        if type(self.max_captures) is not int or type(self.max_search_fields) is not int:
            raise ValueError('Bootstrap capture limits must be integers.')


def search_points(position, bounds, step, maximum):
    """A finite square spiral, clipped to today's explicitly supplied rectangle."""
    x0, x1, y0, y1 = bounds
    yield (position.x, position.y)
    emitted = 1
    rings = math.ceil(max(abs(x0-position.x), abs(x1-position.x),
                          abs(y0-position.y), abs(y1-position.y))/step)
    for ring in range(1, rings+1):
        perimeter = ([(ring, y) for y in range(-ring+1, ring+1)] +
                     [(x, ring) for x in range(ring-1, -ring-1, -1)] +
                     [(-ring, y) for y in range(ring-1, -ring-1, -1)] +
                     [(x, -ring) for x in range(-ring+1, ring+1)])
        for ix, iy in perimeter:
            x, y = position.x+ix*step, position.y+iy*step
            if x0 <= x <= x1 and y0 <= y <= y1:
                if emitted >= maximum:
                    return
                yield (x, y)
                emitted += 1


def select_edge_roi(frame, roi_um, transform):
    """Current-frame focus proposal, with bounded low-pass texture suppression.

    Strong flakes can dominate the normal-resolution gradient after focusing.
    A single 12-working-pixel low-pass retry finds the large-scale separator;
    neither its direction nor the smoothed image is accepted as raw evidence.
    """
    try:
        return edge_focus_roi(frame, roi_um, transform)
    except AcquisitionError:
        transform.validate_frame(frame)
        h,w=frame.rgb.shape[:2];scale=min(1.,512/max(h,w));ww,hh=round(w*scale),round(h*scale)
        ratio=np.array([w/ww,h/hh])
        rgb=cv2.GaussianBlur(resize_rgb_float32(frame.rgb,(ww,hh)),(0,0),12.)
        working_transform=CameraTransform((np.asarray(transform.matrix)@np.diag(ratio)).tolist(),
                                          tuple(np.asarray(frame.pixel_size_um)*ratio))
        working=Frame(rgb,frame.position,working_transform.pixel_size_um,frame.sequence,frame.acquired_at,frame.metadata)
        selected=edge_focus_roi(working,roi_um,working_transform)
        centre=(np.asarray(selected['edge_centre_px'])+.5)*ratio-.5
        width,height=np.ceil(roi_um/np.asarray(frame.pixel_size_um)).astype(int)
        x0,y0=np.rint(centre-[width/2,height/2]).astype(int);x1,y1=x0+width,y0+height
        if x0<0 or y0<0 or x1>w or y1>h:
            raise AcquisitionError('Low-pass edge proposal does not fit the fixed raw focus ROI.')
        selected.update(box_px=[int(x0),int(y0),int(x1),int(y1)],edge_centre_px=centre.tolist(),
            roi_centre_px=[float((x0+x1-1)/2),float((y0+y1-1)/2)],
            point_xy_um=transform.stage_xy(centre,frame).tolist(),
            method='current_frame_lowpass_separator_proposal',proposal_smoothing_sigma_working_px=12.,
            policy='Low-pass current-image focus proposal only; full raw RGB line validation and independent focus confirmation remain required.')
        return selected


def edge_evidence(frame, transform, roi_um=64., *, selection_hint=None):
    """Fit current grayscale direction, then require independent RGB line support.

    The normal points from the bright chip towards the dark holder. Neither
    side is chosen by a model trained on the proposed reference.
    """
    selection = selection_hint or select_edge_roi(frame, roi_um, transform)
    if selection_hint is not None:
        selected_position=selection.get('selection_position_um',{})
        if math.hypot(selected_position.get('x',float('inf'))-frame.position.x,
                      selected_position.get('y',float('inf'))-frame.position.y)>.5:
            raise SafetyError('Edge proposal is not from the same current XY field.')
    h, w = frame.rgb.shape[:2]
    working_scale = min(1., 512/max(h, w))
    linear = np.asarray(transform.matrix) @ np.diag([w/round(w*working_scale), h/round(h*working_scale)])
    normal = np.linalg.solve(linear.T, selection['normal_working_pixels'])
    normal /= np.linalg.norm(normal)
    point = transform.stage_xy(selection['edge_centre_px'], frame)
    accepted = []
    for sign in (1., -1.):
        candidate = {'normal': (sign*normal).tolist(), 'offset_um': float(sign*normal@point)}
        validation = validate_photometric_edge(frame, candidate, transform.matrix)
        proposal_validation=None
        if validation['reason'] in ('Candidate direction disagrees with the measured raw edge.',
                                     'Candidate lies outside the measured raw transition.'):
            # Only a coherent full-span signed raw line reaches these two
            # registration failures. Refit it once and rerun every raw gate.
            proposal_validation=validation
            wh,ww=validation['raw_working_shape']
            measured_linear=np.asarray(transform.matrix)@np.diag([w/ww,h/wh])
            origin=np.array([frame.position.x,frame.position.y])+np.asarray(transform.matrix)@[(w/ww-w)/2,(h/wh-h)/2]
            points=np.asarray(validation['measured_raw_points_image'])@measured_linear.T+origin
            refined_normal=np.linalg.solve(measured_linear.T,validation['raw_normal_image'])
            refined_normal/=np.linalg.norm(refined_normal)
            candidate={'normal':refined_normal.tolist(),'offset_um':float(np.median(points@refined_normal))}
            validation=validate_photometric_edge(frame,candidate,transform.matrix)
            validation={**validation,'proposal_validation':proposal_validation,'proposal_refined_once':True}
        if validation['accepted']:
            accepted.append((candidate, validation))
    if len(accepted) != 1:
        raise AcquisitionError('Fresh edge lacks one independently supported bright-chip/dark-holder polarity.')
    edge, validation = accepted[0]
    signed_normal=np.asarray(edge['normal'])
    point=point+signed_normal*(edge['offset_um']-point@signed_normal)
    return {'edge': edge, 'raw_validation': validation, 'selection': selection,
            'edge_point_xy_um': point.tolist(), 'assumption': 'Known bright chip on dark holder; no chemical identification.'}


def verify_reference(reference, edge_frame, evidence, transform):
    """Compare final substrate directly against independently located raw sides."""
    if _context(reference) != _context(edge_frame):
        raise SafetyError('Bootstrap reference and independent edge have different optical contexts.')
    h, w = edge_frame.rgb.shape[:2]
    stride = max(1, math.ceil(max(h, w)/512))
    control_rgb = edge_frame.rgb[::stride, ::stride]
    yy, xx = np.mgrid[0:h:stride, 0:w:stride]
    normal = np.asarray(evidence['edge']['normal'])
    pixel_normal = np.asarray(transform.matrix).T @ normal
    distance = ((xx-(w-1)/2)*pixel_normal[0] + (yy-(h-1)/2)*pixel_normal[1] +
                np.array([edge_frame.position.x, edge_frame.position.y])@normal - evidence['edge']['offset_um'])
    inside, outside = distance < -20., distance > 20.
    if min(int(inside.sum()), int(outside.sum())) < .05*inside.size:
        raise AcquisitionError('Measured edge has insufficient independent bright and dark control pixels.')
    bright = np.median(control_rgb[inside], axis=0).astype(float)
    dark = np.median(control_rgb[outside], axis=0).astype(float)
    delta = bright-dark
    raw_pixels = reference.rgb.reshape(-1, 3)
    pixels = raw_pixels[::max(1, math.ceil(len(raw_pixels)/100000))].astype(float)
    centre = np.median(pixels, axis=0)
    tolerance = np.maximum(.2*bright, .3*delta)
    matches = np.all(abs(pixels-bright) <= tolerance, axis=1)
    separated = np.all(centre > dark+.6*delta)
    accepted = bool(np.all(delta > .1*np.maximum(bright, 1)) and separated and np.mean(matches) >= .85)
    report = {'accepted': accepted, 'bright_control_rgb': bright.tolist(), 'dark_control_rgb': dark.tolist(),
              'reference_median_rgb': centre.tolist(), 'bright_match_fraction': float(np.mean(matches)),
              'minimum_bright_match_fraction': .85, 'control_sampling_stride_px': stride,
              'reference_sample_pixels': len(pixels),
              'qualification': 'Independent raw edge polarity and bright-side correspondence, not chemical identity.'}
    if not accepted:
        raise AcquisitionError('Fresh reference does not correspond to the independently measured bright chip side.')
    return report


def clean_reference_target(frame, transform, evidence, bounds, *, margin_um=60., attempted=()):
    """Select a fully supported 40 um patch observed in this current bright field.

    Color is used only after independent bright-edge correspondence. The whole
    next camera field must remain inside the already measured bright half-plane.
    Fresh captures still need the unchanged final reference gates.
    """
    labels=SubstrateReference.from_frame(frame).classify(frame)
    field_support=float(np.mean(labels==1))
    if field_support<.55:
        raise AcquisitionError('Observed bright field has insufficient substrate support for clean-patch retry.')
    mh,mw=labels.shape;h,w=frame.rgb.shape[:2]
    linear=np.asarray(transform.matrix)@np.diag([w/mw,h/mh])
    # A conservative metric radius includes interpolation/classification pixel
    # uncertainty. Padding makes image borders unsupported as well.
    metric=float(np.linalg.svd(linear,compute_uv=False).min())
    clearance=cv2.distanceTransform(np.pad(np.uint8(labels==1),1),cv2.DIST_L2,5)[1:-1,1:-1]
    yy,xx=np.nonzero(clearance>20./metric+1.)
    if not len(xx):raise AcquisitionError('No fully supported observed 40 um substrate patch for reference retry.')
    pixels=(np.column_stack([xx,yy])+.5)*[w/mw,h/mh]-.5
    targets=(pixels-[(w-1)/2,(h-1)/2])@np.asarray(transform.matrix).T+[frame.position.x,frame.position.y]
    normal=np.asarray(evidence['edge']['normal'])
    half_extent=np.abs(np.asarray(transform.matrix).T@normal)@np.array([w/2,h/2])
    valid=((targets[:,0]>=bounds[0])&(targets[:,0]<=bounds[1])&
           (targets[:,1]>=bounds[2])&(targets[:,1]<=bounds[3])&
           (targets@normal+half_extent+margin_um<=evidence['edge']['offset_um']))
    for old in attempted:
        valid &= np.linalg.norm(targets-np.asarray(old),axis=1)>=20.
    indices=np.flatnonzero(valid)
    if not len(indices):raise AcquisitionError('Observed clean reference patches lack safe full-field interior clearance.')
    selected=max(indices,key=lambda i:(float(clearance[yy[i],xx[i]]),
        -float(np.linalg.norm(targets[i]-[frame.position.x,frame.position.y]))))
    return {'point_xy_um':targets[selected].tolist(),'source_pixel':pixels[selected].tolist(),
            'fully_supported_radius_um':float(clearance[yy[selected],xx[selected]]*metric),
            'field_substrate_fraction':field_support,
            'previous_central_patch':classify_seed_patch(labels,frame.rgb.shape,transform.matrix,40.),
            'selection':'Largest observed fully supported substrate patch with measured bright-side full-field clearance.'}


def acquire_reference(scope, config, settings, run, position, bounds, check, update,
                      capture_reference, validate_final, *, options=None):
    """Return (frame, exposure_audit, receipt, proof); failure never starts a scan.

    ``validate_final(frame)`` is the worker's existing optical/reference gate.
    The 5 mm minimum chip informs only search pitch, never material acceptance.
    """
    options = options or BootstrapOptions()
    run = Path(run)
    folder = run/'autonomous-reference'
    folder.mkdir(parents=True, exist_ok=False)
    transform = transform_from_config(config)
    bounds = list(map(float, bounds))
    if (len(bounds) != 4 or not np.isfinite(bounds).all() or bounds[0] >= bounds[1] or bounds[2] >= bounds[3]):
        raise SafetyError('Bootstrap needs finite current-placement XY bounds.')
    if not getattr(scope, 'armed', False) or scope.limits is None:
        raise SafetyError('Bootstrap requires an already armed acquisition owner.')
    if not (scope.limits.x_min <= bounds[0] < bounds[1] <= scope.limits.x_max and
            scope.limits.y_min <= bounds[2] < bounds[3] <= scope.limits.y_max):
        raise SafetyError('Bootstrap rectangle exceeds armed motion limits.')
    budget = AcquisitionBudget(options.max_seconds, options.max_captures)
    full = BudgetScope(scope, budget)
    original_check = full.check_cancel
    def checked():
        check()
        original_check()
        if (folder/'STOP').exists():
            raise Cancelled('Autonomous reference STOP requested.')
    full.check_cancel = checked
    proof = {'schema': 1, 'state': 'searching', 'initial_position_um': asdict(position),
             'bounds_um': bounds, 'options': asdict(options), 'observations': [], 'xy_travel_um': 0.}

    def persist():
        proof['budget'] = budget.report()
        atomic_json(folder/'proof.json', proof)

    def travel(x, y):
        checked()
        if not (bounds[0] <= x <= bounds[1] and bounds[2] <= y <= bounds[3]):
            raise SafetyError('Bootstrap target lies outside the new placement rectangle.')
        current = full.position()
        distance = math.hypot(x-current.x, y-current.y)
        if proof['xy_travel_um']+distance > options.max_xy_travel_um:
            raise AcquisitionError('Autonomous reference XY travel budget exhausted.')
        proof['xy_travel_um'] += distance
        full.travel(float(x), float(y))
        arrived = full.position()
        if math.hypot(arrived.x-x, arrived.y-y) > .5:
            raise SafetyError('Bootstrap XY arrival was not verified.')

    def capture(name):
        frame = full.capture()
        save_frame(folder, name, frame)
        path = folder/(name+'.tiff')
        receipt = acknowledge_raw(full, frame, path)
        proof['observations'].append({'raw_path': str(path), 'raw_sha256': file_hash(path),
            'metadata_sha256': file_hash(path.with_suffix('.json')), 'vendor_receipt': receipt,
            'position_um': asdict(frame.position)})
        persist()
        checked()
        transform.validate_frame(frame)
        update('autonomous_reference', bootstrap_state=proof['state'], bootstrap_budget=budget.report())
        return frame, receipt

    def selection(frame):
        try:
            return select_edge_roi(frame, options.focus_roi_um, transform)
        except AcquisitionError:
            return None

    def median(frame):
        pixels = frame.rgb.reshape(-1, 3)
        return np.median(pixels[::max(1, math.ceil(len(pixels)/100000))], axis=0).astype(float)

    try:
        checked()
        now = full.position()
        if math.hypot(now.x-position.x, now.y-position.y) > .5 or abs(now.z-position.z) > .5:
            raise SafetyError('Bootstrap origin changed before acquisition.')
        if not (bounds[0] <= now.x <= bounds[1] and bounds[2] <= now.y <= bounds[3]):
            raise SafetyError('Bootstrap origin lies outside the new placement rectangle.')
        previous = None
        chosen = None
        for index, point in enumerate(search_points(position, bounds, options.minimum_chip_um/2, options.max_search_fields)):
            if index:
                travel(*point)
            frame, _ = capture(f'search-{index:03d}')
            chosen = selection(frame)
            if chosen:
                break
            # Coarse homogeneous fields can bracket a chip. Bisect only a
            # consistent broadband brightness change, at unchanged optics.
            if previous is not None and _context(previous) == _context(frame):
                a, b = median(previous), median(frame)
                change = (a-b)/np.maximum(np.maximum(a, b), 1)
                if np.all(change > .25) or np.all(change < -.25):
                    left, right = previous, frame
                    for refinement in range(12):
                        if math.hypot(left.position.x-right.position.x, left.position.y-right.position.y) < 10:
                            break
                        travel((left.position.x+right.position.x)/2, (left.position.y+right.position.y)/2)
                        frame, _ = capture(f'bracket-{index:03d}-{refinement:02d}')
                        chosen = selection(frame)
                        if chosen:
                            break
                        value = median(frame)
                        if np.linalg.norm(value-median(left)) < np.linalg.norm(value-median(right)):
                            left = frame
                        else:
                            right = frame
                    if chosen:
                        break
            previous = frame
        if chosen is None:
            raise AcquisitionError('Bounded fresh-placement search found no focusable chip/holder edge.')
        proof.update(state='edge_focus', edge_selection=chosen)
        persist()
        # Adapt exposure while the independently selected edge is still in view.
        edge_run = folder/'edge-exposure'
        edge_run.mkdir()
        edge_frame, edge_audit, _ = capture_reference(full, config, settings, edge_run, full.position(), checked, update)
        chosen = select_edge_roi(edge_frame, options.focus_roi_um, transform)
        proof['edge_selection']=chosen
        roi = FocusROIAdapter(full, options.focus_roi_um, transform, box=chosen['box_px'], reference_frame=edge_frame, edge=True)
        roi.selection = chosen
        # Count the physical capture once; the ROI wraps the original scope.
        roi.scope = scope
        counted_roi = BudgetScope(roi, budget)
        counted_roi.check_cancel = checked
        result = recover_focus(counted_roi, scope.limits.z_min, scope.limits.z_max, output=folder/'focus',
                               initial_step_um=2., growth=2., check_cancel=check,
                               callback=lambda _frame, row: update('autonomous_reference_focus', focus_sample=row))
        proof['focus_result'] = asdict(result)
        if not result.success or result.outcome != 'resolved_peak':
            raise AcquisitionError('Autonomous reference edge focus unresolved: '+result.reason)
        focused, _ = capture('focused-edge')
        control_path = folder/'focused-edge.tiff'
        if abs(focused.position.z-result.z_um) > .25:
            raise SafetyError('Bootstrap edge frame differs from independently confirmed focus height.')
        evidence = edge_evidence(focused, transform, options.focus_roi_um, selection_hint=chosen)
        proof.update(state='bright_interior', edge_evidence=evidence)
        normal = np.asarray(evidence['edge']['normal'])
        h, w = focused.rgb.shape[:2]
        half_extent = np.abs(np.asarray(transform.matrix).T@normal) @ np.array([w/2, h/2])
        target = np.asarray(evidence['edge_point_xy_um']) - normal*(half_extent+options.interior_margin_um)
        travel(*target)
        proof['reference_candidates']=[];attempted=[]
        for candidate_index in range(3):
            candidate_run=run if candidate_index==0 else folder/f'reference-candidate-{candidate_index+1:02d}'
            candidate_run.mkdir(parents=True,exist_ok=True)
            reference_settings = dict(settings, exposure_ms=config['context']['exposure_ms'])
            frame, audit, receipt = capture_reference(full, config, reference_settings, candidate_run, full.position(), checked, update)
            attempted.append([frame.position.x,frame.position.y])
            # Exposure adjustment needs new independent controls at the exact
            # exposure. Every candidate has unique raw and control filenames.
            if _context(frame) != _context(focused):
                reference_position = full.position()
                travel(focused.position.x, focused.position.y)
                control_name='final-exposure-edge' if candidate_index==0 else f'final-exposure-edge-{candidate_index+1:02d}'
                focused, _ = capture(control_name)
                control_path = folder/(control_name+'.tiff')
                evidence = edge_evidence(focused, transform, options.focus_roi_um, selection_hint=chosen)
                travel(reference_position.x, reference_position.y)
            proof['reference_controls'] = verify_reference(frame, focused, evidence, transform)
            if abs(frame.position.z-result.z_um) > .25:
                raise SafetyError('Final substrate reference lost the independently verified edge focus height.')
            record={'candidate':candidate_index+1,'position_um':asdict(frame.position),'exposure_audit':dict(audit),
                    'reference_controls':proof['reference_controls'],'accepted':False}
            proof['reference_candidates'].append(record)
            try:
                validate_final(frame)
            except AcquisitionError as exc:
                record['reason']=str(exc);persist();checked()
                if candidate_index==2:raise
                proposal=clean_reference_target(frame,transform,evidence,bounds,
                    margin_um=options.interior_margin_um,attempted=attempted)
                record['next_observed_patch']=proposal;persist()
                update('autonomous_reference_clean_patch',reference_candidate=candidate_index+2,clean_patch=proposal)
                travel(*proposal['point_xy_um'])
            else:
                record['accepted']=True
                break
        checked()
        proof.update(state='accepted', edge_evidence=evidence, reference_position_um=asdict(frame.position),
                     focus_result_path=str(folder/'focus'/'focus-result.json'),
                     focused_edge_path=str(control_path), focused_edge_sha256=file_hash(control_path),
                     focused_edge_metadata_sha256=file_hash(control_path.with_suffix('.json')), edge_exposure=edge_audit)
        persist()
        audit.update(qualification='Autonomous fresh-placement reference with independent bright-chip/dark-holder evidence.',
                     bootstrap_proof=str(folder/'proof.json'), bootstrap_proof_sha256=file_hash(folder/'proof.json'))
        atomic_json(run/'exposure-adjustment.json', audit)
        checked()
        return frame, audit, receipt, proof
    except Exception as exc:
        proof.update(state='failed', error_type=type(exc).__name__, error=str(exc))
        persist()
        raise
