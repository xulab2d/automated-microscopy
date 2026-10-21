"""Explicit same-placement reuse of a focused reference refused only by old central-patch gate.

No search bounds or continuation state are inherited. The explicitly authorized
return uses the retained frame's position; raw evidence remains immutable.
"""
from pathlib import Path
import json,math,re

from .model import SafetyError,atomic_json
from .imaging import load_frame
from .vendor_lifecycle import file_hash

KIND='explicit_same_placement_focused_reference_reuse_v1'
OLD_REFUSAL='Fresh reference lacks a coherent bare-substrate field and central 40 µm patch.'


def _read(path):return json.loads(Path(path).read_text(encoding='utf-8'))


def _evidence(project,prior_run_id):
    from .model import CameraTransform
    from .autonomous_reference import verify_reference
    from .photometric_edges import validate_photometric_edge
    from .reference_focus_recovery import validate_raw_receipt
    project=Path(project).resolve()
    if not re.fullmatch(r'scan-[A-Za-z0-9-]+',prior_run_id):raise SafetyError('Invalid retained-reference donor identity.')
    donor=project/'data'/prior_run_id
    if donor.resolve()!=donor:raise SafetyError('Retained-reference donor is redirected.')
    names=['scan-request.json','scan-status.json','worker-start.json','camera-profile-used.json','exposure-adjustment.json',
           'autonomous-reference/proof.json','autonomous-reference/focus/focus-result.json',
           'autonomous-reference/focus/recovery.json','autonomous-reference/focus/verified_focus.tiff',
           'autonomous-reference/focus/verified_focus.json']
    request,status,worker,profile,audit,proof,result,recovery=[_read(donor/n) for n in names[:8]]
    if (status.get('state')!='failed' or status.get('hardware_closed') is not True
            or status.get('error')!=OLD_REFUSAL or status.get('error_type')!='AcquisitionError'
            or status.get('stop_errors')!=[] or status.get('watchdog_reason') is not None
            or proof.get('error')!=OLD_REFUSAL or proof.get('state')!='failed'
            or status.get('request_sha256')!=file_hash(donor/'scan-request.json')
            or worker.get('request_sha256')!=status.get('request_sha256')
            or status.get('source_release')!=worker.get('source_release')
            or status.get('source_release')!=request.get('source_release')
            or proof.get('focus_result')!=result or result.get('success') is not True
            or result.get('outcome')!='resolved_peak' or recovery.get('state')!='focused'
            or not recovery.get('confirmation') or proof.get('reference_controls',{}).get('accepted') is not True):
        raise SafetyError('Retained reference lacks the exact central-only refusal and verified focus provenance.')
    attempt=audit['attempts'][-1];raw=Path(attempt['raw_path'])
    if not raw.is_relative_to(donor/'calibration') or raw.resolve()!=raw:
        raise SafetyError('Retained raw reference is outside the exact donor calibration folder.')
    frame=load_frame(raw);validate_raw_receipt(frame,raw,attempt['vendor_receipt'])
    if (attempt['raw_sha256']!=file_hash(raw) or attempt['metadata_sha256']!=file_hash(raw.with_suffix('.json'))
            or abs(frame.position.z-result['z_um'])>.25
            or abs(frame.position.z-recovery['confirmation']['z_um'])>.25):
        raise SafetyError('Retained raw reference differs from its independent focus confirmation.')
    edge_name='autonomous-reference/focused-edge.tiff'
    if (donor/'autonomous-reference/final-exposure-edge.tiff').exists():edge_name='autonomous-reference/final-exposure-edge.tiff'
    edge=load_frame(donor/edge_name);transform=CameraTransform(**profile['camera_transform'])
    evidence=proof['edge_evidence']
    if (abs(edge.position.z-result['z_um'])>.25 or evidence.get('raw_validation',{}).get('accepted') is not True
            or not validate_photometric_edge(edge,evidence['edge'],transform.matrix)['accepted']):
        raise SafetyError('Retained reference lacks independent current-placement raw edge support.')
    controls=verify_reference(frame,edge,evidence,transform)
    if controls!=proof['reference_controls']:raise SafetyError('Retained bright-side reference correspondence changed.')
    names += [raw.relative_to(donor).as_posix(),raw.with_suffix('.json').relative_to(donor).as_posix(),
              raw.with_suffix('.zen-vendor.json').relative_to(donor).as_posix(),edge_name,str(Path(edge_name).with_suffix('.json'))]
    hashes={}
    for name in names:
        path=donor/name
        if path.resolve()!=path:raise SafetyError('Retained reference evidence is redirected.')
        hashes[name]=file_hash(path)
    exposure=float(frame.metadata['camera_resources']['ff_exposure_ms'])
    if not math.isfinite(exposure) or exposure<=0:raise SafetyError('Retained reference exposure is invalid.')
    return donor,frame,audit,attempt['vendor_receipt'],proof,hashes,exposure


def build_contract(project,prior_run_id):
    """Caller explicitly authorizes reuse and return in the same unmoved placement."""
    donor,_,_,_,_,hashes,exposure=_evidence(project,prior_run_id)
    return {'schema':1,'kind':KIND,'prior_run_id':prior_run_id,'same_placement_authorized':True,
            'central_patch_policy':'informational_only','exposure_ms':exposure,'donor_files_sha256':hashes,
            'camera_transform':_read(donor/'camera-profile-used.json')['camera_transform'],
            'return_to_retained_reference':True,'inherit_search_bounds':False,
            'qualification':'Explicit reuse and bounded return to retained focused bright-side reference in the same current placement; new search bounds use live starting position.'}


def verify_contract(project,contract):
    if not isinstance(contract,dict) or contract!=build_contract(project,contract.get('prior_run_id','')):
        raise SafetyError('Retained-reference request or pinned donor evidence changed.')
    return contract


def acquire_reference(scope,config,project,run,contract,check,validate_final,*,bounds):
    verify_contract(project,contract);check()
    donor,frame,audit,receipt,donor_proof,_,exposure=_evidence(project,contract['prior_run_id'])
    # JSON stores pixel-size tuples as lists; compare canonical serialized
    # values so identical measured calibration survives request persistence.
    if json.dumps(config['camera_transform'],sort_keys=True)!=json.dumps(contract['camera_transform'],sort_keys=True):
        raise SafetyError('Current camera transform differs from retained-reference calibration.')
    # The owner already verified the engaged objective and set the requested
    # lamp. Match exposure exactly; final validation checks that current config
    # and the retained raw camera/objective/illumination metadata agree.
    scope.set_exposure_ms(exposure)
    config['zen']['camera_resources']['ff_exposure_ms']=exposure
    config['context']['exposure_ms']=exposure
    validate_final(frame);check()
    if not (bounds[0]<=frame.position.x<=bounds[1] and bounds[2]<=frame.position.y<=bounds[3]):
        raise SafetyError('Retained-reference return lies outside the new live-position search bounds.')
    scope.limits.check(frame.position)
    from .motion import move_z_bounded
    before=scope.position()
    move_z_bounded(scope,frame.position.z);check()
    scope.travel(frame.position.x,frame.position.y);check()
    arrived=scope.position()
    if math.hypot(arrived.x-frame.position.x,arrived.y-frame.position.y)>.5 or abs(arrived.z-frame.position.z)>.25:
        raise SafetyError('Return to retained-reference XYZ was not verified.')
    from dataclasses import asdict
    proof={'schema':1,'kind':KIND,'state':'accepted_retained_reference','contract':contract,
           'source_run':donor.name,'reference_controls':donor_proof['reference_controls'],
           'focus_result':donor_proof['focus_result'],'fresh_capture_performed':False,'stage_motion_performed':True,
           'return_from_um':asdict(before),'return_verified_um':asdict(arrived),'new_search_bounds_um':list(bounds)}
    atomic_json(Path(run)/'retained-reference-proof.json',proof)
    audit={**audit,'choice':'explicit_same_placement_retained_focused_reference','fresh_capture_performed':False,
           'source_run':donor.name,'qualification':contract['qualification']}
    atomic_json(Path(run)/'exposure-adjustment.json',audit);check()
    return frame,audit,receipt,proof
