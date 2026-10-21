"""Hash-pinned local focus reuse after verified same-placement continuation."""
from pathlib import Path
import json
import math
from .model import SafetyError
from .chip_discovery import file_hash
from .imaging import load_frame
from .full_chip import _camera_context


def find_local_focus(folder,context,xy,reference,*,partial=False):
    run=next((p for p in (Path(folder),*Path(folder).parents) if p.name.startswith('scan-') and p.parent.name=='data'),None)
    if run is None or not (context or {}).get('prior_run_id'):return None
    request=json.loads((run/'scan-request.json').read_text(encoding='utf-8-sig'))
    contract=request.get('continuation',{})
    from .startup_continuation import donor_contract
    donor=donor_contract(contract)
    if donor!=contract:
        proof=json.loads((run/'continuation-verification.json').read_text(encoding='utf-8-sig'))
        if proof.get('continuation')!=contract or proof.get('checkpoint',{}).get('verified_before_arm') is not True:
            raise SafetyError('Local focus donor lacks the verified outer continuation.')
    contract=donor
    if contract.get('prior_run_id')!=context['prior_run_id']:raise SafetyError('Local focus parent differs from verified continuation.')
    parent=(run.parent/contract['prior_run_id']).resolve(strict=True)
    if parent.parent!=run.parent.resolve():raise SafetyError('Local focus parent escapes project.')
    pinned=contract['prior_files_sha256']
    if partial and contract.get('continuation_kind')!='diagnosed_pre_capture_memory_recovery':return None
    for name in sorted(pinned):
        if not (name.startswith('discovery/') and name.endswith('/recovery.json' if partial else '/focus-result.json')):continue
        result_path=(parent/name).resolve(strict=True)
        image=result_path.with_name('best_observed.tiff' if partial else 'verified_focus.tiff');metadata=image.with_suffix('.json')
        paths=(result_path,image,metadata)
        if any(not p.is_relative_to(parent) for p in paths):raise SafetyError('Local focus evidence escapes parent.')
        if any(p.relative_to(parent).as_posix() not in pinned for p in paths):continue
        if any(file_hash(p)!=pinned[p.relative_to(parent).as_posix()] for p in paths):raise SafetyError('Pinned local focus evidence changed.')
        result=json.loads(result_path.read_text(encoding='utf-8-sig'));frame=load_frame(image)
        physical=frame.metadata.get('physical_stage_position',{})
        if not all(type(physical.get(a)) in (int,float) and math.isfinite(physical[a]) for a in 'xyz'):continue
        if any(abs(float(xy[j])-physical[a])>.5 for j,a in enumerate('xy')):continue
        if partial:
            recovery=result;result=recovery.get('best_observed',{})
            bracket=recovery.get('bracket_um',[])
            if (recovery.get('state')!='failed' or not str(recovery.get('error','')).startswith('Memory recovery exhausted after ')
                    or len(bracket)!=2 or not all(type(x) in (int,float) and math.isfinite(x) for x in bracket)
                    or not bracket[0]<result.get('z_um',float('inf'))<bracket[1] or bracket[1]-bracket[0]>6.):continue
        if ((not partial and (result.get('success') is not True or result.get('outcome')!='resolved_peak'))
                or abs(frame.position.z-result['z_um'])>.25 or _camera_context(frame)!=_camera_context(reference)
                or frame.metadata.get('focus_mask_policy')!='include_chip_edge'):continue
        box=frame.metadata.get('focus_only_roi_source_px')
        if not isinstance(box,list) or len(box)!=4 or any(type(n) is not int for n in box):continue
        x0,y0,x1,y1=box;h,w=reference.rgb.shape[:2]
        if not (0<=x0<x1<=w and 0<=y0<y1<=h and frame.rgb.shape[:2]==(y1-y0,x1-x0)):continue
        saved={'source_run':parent.name,'source_result':name,'source_sha256':{p.relative_to(parent).as_posix():pinned[p.relative_to(parent).as_posix()] for p in paths},
                'result':result,'box':box,'frame':frame}
        if partial:saved.update(partial=True,bracket_um=bracket,prior_samples=len(recovery.get('samples',[])),
            qualification='Interrupted search seed only; a new local fit and independent confirmation are required.')
        return saved
    return None


def confirmation_ok(saved,fresh,metrics,tolerance):
    return (abs(fresh.position.z-saved['result']['z_um'])<=tolerance
        and metrics['texture']>=.015 and metrics['score']>=max(1e-6,.75*saved['result']['score']))
