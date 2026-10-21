"""Fresh focus at a previously registered edge before restarting discovery."""
from dataclasses import asdict
from pathlib import Path
import hashlib,json
from .model import CameraTransform,Position,SafetyError,AcquisitionError,atomic_json
from .imaging import load_frame,save_frame
from .full_chip import FocusROIAdapter,_camera_context
from .motion import move_z_bounded
from .focus_recovery import recover_focus
from .vendor_lifecycle import checkpoint_observation


def reusable_focus_request(project,prior_id,*,_seen=()):
    """Follow only hash-pinned, same-frame successful focus reuse receipts."""
    from .scan_continuation import _run,read,digest
    if prior_id in _seen or len(_seen)>=32:raise SafetyError('Invalid focus reuse lineage.')
    project=Path(project).resolve();prior=_run(project,prior_id)
    names=('focus-result.json','focused-full-field.tiff','focused-full-field.json')
    paths=[prior/'initial-edge-focus'/name for name in names]
    if all(p.is_file() and p.resolve().is_relative_to(prior) for p in paths):
        if read(paths[0]).get('success') is not True:raise SafetyError('Prior initial focus was not resolved.')
        return {'reuse_verified_from':prior_id,'source_run':prior_id,
                'source_sha256':{p.name:digest(p) for p in paths},'reuse_lineage_sha256':{}}
    receipt_path=prior/'initial-focus-reused.json';request_path=prior/'scan-request.json'
    receipt=read(receipt_path);request=read(request_path);batch=read(prior/'batch.json')
    start=read(prior/'worker-start.json');focus=request['focus_first'];contract=request['continuation']
    from .startup_continuation import donor_contract
    contract=donor_contract(contract)
    parent=contract['prior_run_id']
    if (start.get('request_sha256')!=digest(request_path) or focus.get('reuse_verified_from')!=parent
            or batch.get('coordinate_frame_id')!=contract.get('prior_coordinate_frame_id')):
        raise SafetyError('Focus reuse lineage request/frame changed.')
    inherited=reusable_focus_request(project,parent,_seen=(*_seen,prior_id))
    if (receipt.get('source_run')!=inherited['source_run'] or focus.get('source_run',parent)!=inherited['source_run']
            or focus.get('source_sha256')!=inherited['source_sha256']
            or focus.get('reuse_lineage_sha256',{})!=inherited['reuse_lineage_sha256']
            or receipt.get('result_sha256')!=inherited['source_sha256']['focus-result.json']):
        raise SafetyError('Focus reuse lineage does not match pinned source evidence.')
    parent_folder=_run(project,parent)
    if (contract['prior_files_sha256'].get('scan-request.json')!=digest(parent_folder/'scan-request.json')
            or read(parent_folder/'batch.json').get('coordinate_frame_id')!=batch.get('coordinate_frame_id')):
        raise SafetyError('Focus reuse parent identity/frame changed.')
    lineage=dict(inherited['reuse_lineage_sha256'])
    for p in (receipt_path,request_path,prior/'worker-start.json',prior/'batch.json'):
        lineage[p.relative_to(project).as_posix()]=digest(p)
    return {**inherited,'reuse_verified_from':prior_id,'reuse_lineage_sha256':lineage}


def focus_saved_edge(scope,config,reference_path,run,request,progress):
    project=Path(run).parent.parent.resolve()
    if request.get('reuse_verified_from'):
        if request['reuse_verified_from'] != config.get('context',{}).get('prior_run_id'):
            raise SafetyError('Reused focus must come from the verified same-placement parent.')
        if request.get('source_run',request['reuse_verified_from'])!=request['reuse_verified_from']:
            verified=reusable_focus_request(project,request['reuse_verified_from'])
            if any(request.get(key)!=value for key,value in verified.items()):
                raise SafetyError('Pinned focus reuse lineage changed.')
        source=(project/'data'/request.get('source_run',request['reuse_verified_from'])/'initial-edge-focus').resolve(strict=True)
        if not source.is_relative_to(project/'data'):raise SafetyError('Focus source escapes the project.')
        result_path=source/'focus-result.json';image_path=source/'focused-full-field.tiff'
        expected=request.get('source_sha256',{})
        for path in (result_path,image_path,image_path.with_suffix('.json')):
            if hashlib.sha256(path.read_bytes()).hexdigest()!=expected.get(path.name):
                raise SafetyError('Pinned reused focus evidence changed: '+path.name)
        result=json.loads(result_path.read_text(encoding='utf-8-sig'))
        frame=load_frame(image_path);reference=load_frame(reference_path)
        CameraTransform(**config['camera_transform']).validate_frame(frame)
        if not result.get('success') or abs(frame.position.z-result['z_um'])>.25 or _camera_context(frame)!=_camera_context(reference):
            raise SafetyError('Saved focus evidence does not match this acquisition profile.')
        move_z_bounded(scope,result['z_um']);scope.travel(reference.position.x,reference.position.y)
        atomic_json(Path(run)/'initial-focus-reused.json',{'source_run':request.get('source_run',request['reuse_verified_from']),
            'verified_parent_run':request['reuse_verified_from'],
            'z_um':result['z_um'],'source_result':str(result_path),'source_frame':str(image_path),
            'result_sha256':hashlib.sha256(result_path.read_bytes()).hexdigest(),
            'position_after_return_um':asdict(scope.position()),'reason':'Verified same-placement focus retained after a software-only handoff repair.'})
        progress('initial_focus_reused',frame,{'z_um':result['z_um'],'score':result['score']})
        return {'seed_focus_z_um':result['z_um'],'seed_focus_evidence':result_path,'seed_focus_frame':image_path}
    source=Path(request['selection_path']).resolve(strict=True)
    if not source.is_relative_to(project/'data'):
        raise SafetyError('Initial edge evidence is outside this project.')
    if hashlib.sha256(source.read_bytes()).hexdigest()!=request['selection_sha256']:
        raise SafetyError('Initial edge selection evidence changed.')
    selection=json.loads(source.read_text(encoding='utf-8-sig'))
    position=selection['selection_position_um']
    scope.limits.check(Position(position['x'],position['y'],scope.position().z))
    progress('returning_to_recorded_edge',None,{'z_um':scope.position().z})
    scope.travel(position['x'],position['y']);scope.check_cancel()
    reference=load_frame(reference_path)
    adapter=FocusROIAdapter(scope,64.,CameraTransform(**config['camera_transform']),reference_frame=reference,edge=True)
    folder=Path(run)/'initial-edge-focus'
    adapter.begin_focus_anchor();adapter.save_focus_selection(folder/'selection')
    progress('initial_edge_focus',None,{'z_um':scope.position().z})
    def focused_sample(frame,sample):progress('initial_edge_focus',frame,sample)
    result=recover_focus(adapter,scope.limits.z_min,scope.limits.z_max,output=folder,
                         callback=focused_sample,max_elapsed_s=420.,max_captures=48)
    atomic_json(folder/'focus-result.json',asdict(result))
    if not result.success:
        raise AcquisitionError('Initial recorded edge focus unresolved: '+result.reason)
    frame=scope.capture();save_frame(folder,'focused-full-field',frame)
    checkpoint_observation(scope,frame,folder,'initial_focus_full_field',{'z_um':result.z_um},
                           selected_raw=folder/'focused-full-field.tiff')
    progress('initial_focus_verified',frame,{'z_um':result.z_um,'score':result.score})
    # Campaign starts at its reference XY; retain the newly measured focus Z.
    scope.travel(reference.position.x,reference.position.y)
    return {'seed_focus_z_um':result.z_um,'seed_focus_evidence':folder/'focus-result.json',
            'seed_focus_frame':folder/'focused-full-field.tiff'}
