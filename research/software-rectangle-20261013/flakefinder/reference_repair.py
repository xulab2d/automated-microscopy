"""Explicit focused-reference correction with independent chip/backing controls."""
from pathlib import Path
import json,hashlib
import numpy as np
from .model import SafetyError,AcquisitionError,Position,atomic_json
from .imaging import load_frame
from .layout import SubstrateReference,_context
from .autofocus import focus_metrics

PLAN='supervision/reference-repair/plan.json'

def read(path):return json.loads(Path(path).read_text(encoding='utf-8-sig'))
def digest(path):return hashlib.sha256(Path(path).read_bytes()).hexdigest()

def validate_plan(parent):
    parent=Path(parent).resolve();path=parent/PLAN;plan=read(path);base=path.parent
    if (plan.get('schema')!=1 or plan.get('kind')!='focused_reference_with_independent_controls_v1'
            or plan.get('prior_request_sha256')!=digest(parent/'scan-request.json')):
        raise SafetyError('Reference correction is not bound to its parent request.')
    files=[PLAN]
    for name,expected in plan['files_sha256'].items():
        p=(base/name).resolve(strict=True)
        if p.parent!=base or digest(p)!=expected:raise SafetyError('Reference correction asset changed.')
        files.append(p.relative_to(parent).as_posix())
    meta=read(base/Path(plan['focused_frame']).with_suffix('.json'));result=read(base/plan['focus_result'])
    if not result.get('success') or abs(meta['position']['z']-result['z_um'])>.25 or meta['position']!=plan['position_um']:
        raise SafetyError('Reference correction requires independently resolved focus at its actual position.')
    if len(plan['chip_controls'])<2 or len(plan['backing_controls'])<2 or plan.get('fresh_focus_score_fraction')!=.95:
        raise SafetyError('Reference correction lacks independent controls or confirmation policy.')
    for name in [plan['focused_frame'],plan['focus_result'],plan['selection'],*plan['chip_controls'],*plan['backing_controls']]:
        if name not in plan['files_sha256']:raise SafetyError('Unpinned reference correction input.')
        if name.endswith('.tiff') and str(Path(name).with_suffix('.json')) not in plan['files_sha256']:
            raise SafetyError('Unpinned reference correction metadata.')
    return plan,files

def check_controls(frame,base,plan,*,confirm_focus=True,current_boxes=None,check=lambda:None):
    """Controls never contribute pixels to training the new colour reference."""
    base=Path(base);known=load_frame(base/plan['focused_frame'])
    if _context(frame)!=_context(known) or frame.rgb.shape!=known.rgb.shape or frame.pixel_size_um!=known.pixel_size_um:
        raise SafetyError('Replacement reference optics or dimensions changed.')
    if any(abs(getattr(frame.position,a)-plan['position_um'][a])>(.25 if a=='z' else .5) for a in 'xyz'):
        raise SafetyError('Replacement reference position changed.')
    model=SubstrateReference.from_frame(frame);checks=[]
    for group,wanted,threshold in [('chip_controls',1,.8),('backing_controls',2,.95)]:
        for name in plan[group]:
            check()
            labels=model.classify(load_frame(base/name));fraction=float(np.mean(labels==wanted))
            checks.append(dict(image=name,expected_label=wanted,fraction=fraction,required=threshold,accepted=fraction>=threshold))
    x0,y0,x1,y1=read(base/plan['selection'])['box_px']
    if not 0<=x0<x1<=frame.rgb.shape[1] or not 0<=y0<y1<=frame.rgb.shape[0]:raise SafetyError('Invalid pinned focus ROI.')
    prior=focus_metrics(known.rgb[y0:y1,x0:x1],mode='chip_edge');fresh=focus_metrics(frame.rgb[y0:y1,x0:x1],mode='chip_edge')
    focus_ok=fresh['texture']>=.015 and fresh['score']>=.95*prior['score']
    result=dict(accepted=all(c['accepted'] for c in checks) and (focus_ok or not confirm_focus),
        controls=checks,focus=dict(accepted=focus_ok,prior=prior,fresh=fresh,minimum_score_fraction=.95),
        reference_model=model.to_dict(),qualification='Independent optical chip/backing controls; not chemical identification.')
    if current_boxes is not None:
        if not current_boxes:raise SafetyError('Correspondence has no geometrically possible raw crop.')
        rows=[]
        for box in current_boxes:
            check();a,b,c,d=box
            if c-a!=x1-x0 or d-b!=y1-y0 or not 0<=a<c<=frame.rgb.shape[1] or not 0<=b<d<=frame.rgb.shape[0]:raise SafetyError('Corresponding raw crop changed size or bounds.')
            metrics=focus_metrics(frame.rgb[b:d,a:c],mode='chip_edge')
            rows.append({'box_px':list(box),'metrics':metrics,'accepted':metrics['texture']>=.015 and metrics['score']>=.95*prior['score']})
        focus_ok=all(r['accepted'] for r in rows)
        result['focus'].update(accepted=focus_ok,fresh=min(rows,key=lambda r:r['metrics']['score'])['metrics'],all_corresponding_crops=rows)
        result['accepted']=all(c['accepted'] for c in checks) and focus_ok
    check()
    return result


def verify_corresponding(frame,parent,run,decision,decision_path,raw,check):
    from .reference_correspondence import validate_decision,POLICY_SHA
    from .correspondence_continuation import KIND
    import copy
    contract=read(run/'scan-request.json')['continuation'];policy=contract.get('reference_correspondence',{})
    if contract.get('continuation_kind')!=KIND or policy.get('policy_sha256')!=POLICY_SHA:raise SafetyError('Correspondence is not authorized by this exact request.')
    plan,_=validate_plan(parent);base=Path(parent)/Path(PLAN).parent;prior_raw=base/plan['focused_frame']
    prior=load_frame(prior_raw);boxes=validate_decision(prior,frame,prior_raw,raw,decision_path,decision,check);del prior
    expected=policy['measured_position_um']
    if any(abs(getattr(frame.position,a)-expected[a])>(.25 if a=='z' else .5) for a in 'xyz'):raise SafetyError('Corresponding reference differs from measured stationary pose.')
    measured=copy.deepcopy(plan);measured['position_um']['z']=expected['z']
    quality=check_controls(frame,base,measured,current_boxes=boxes,check=check)
    quality['correspondence_decision_sha256']=digest(decision_path)
    if not quality['accepted']:
        atomic_json(run/'reference-correction-verification.json',quality)
        raise AcquisitionError('Corresponding reference failed one or more unchanged focus/texture/control gates.')
    check();return quality

def prepare_position(scope,parent,contract,update):
    plan,_=validate_plan(parent)
    if plan!=contract.get('reference_correction'):raise SafetyError('Reference correction differs from the verified contract.')
    from .motion import move_z_bounded
    p=Position(**plan['position_um']);scope.limits.check(p)
    update('returning_to_focused_reference');scope.check_cancel();scope.travel(p.x,p.y);move_z_bounded(scope,p.z);scope.check_cancel()
    actual=scope.position()
    if any(abs(getattr(actual,a)-getattr(p,a))>(.25 if a=='z' else .5) for a in 'xyz'):raise SafetyError('Focused reference return differs from readback.')
    return actual

def verify_fresh(frame,parent,run,*,measured_focus=None,persist=True):
    plan,_=validate_plan(parent)
    if measured_focus is not None:
        from .reference_focus_recovery import validate_measured_proof
        plan=validate_measured_proof(frame,parent,run,measured_focus)
    proof=check_controls(frame,Path(parent)/Path(PLAN).parent,plan)
    if measured_focus is not None:proof['measured_focus_proof_sha256']=digest(Path(run)/'reference-refocus-proof.json')
    if persist or not proof['accepted']:atomic_json(Path(run)/'reference-correction-verification.json',proof)
    if not proof['accepted']:raise AcquisitionError('Focused reference failed fresh focus confirmation or independent chip/backing controls.')
    return proof

def preserved_search(out,planned):
    """Preserve lattice coordinates and indices when the colour-reference XY changes."""
    request=read(Path(out)/'scan-request.json');contract=request.get('continuation',{})
    from .startup_continuation import donor_contract
    contract=donor_contract(contract)
    if not contract.get('reference_correction'):return planned
    parent=Path(out).parent/contract['prior_run_id'];old=read(parent/'search-plan.json')
    if contract['prior_files_sha256'].get('search-plan.json')!=digest(parent/'search-plan.json'):
        raise SafetyError('Reference repair search lattice is unpinned.')
    omit={'start_xy_um','initial_nearest_order'}
    if {k:v for k,v in old.items() if k not in omit}!={k:v for k,v in planned.items() if k not in omit}:
        raise SafetyError('Reference repair would alter the search lattice.')
    return old

def reclassify_progress(out,state,config,reference,transform,search,old,seeds):
    from .chip_campaign import _seed_classification
    from .imaging import save_frame
    from dataclasses import asdict
    model=SubstrateReference.from_frame(reference);changes=[]
    attempts=json.loads(json.dumps(old.get('boundary_attempts',[])))
    for a in attempts:
        a['prior_state']=a['state'];a['state']='superseded_reference_labels'
        a['qualification']='Raw measurements retained; incorrect reference labels do not establish chip outlines.'
    # Start with the newly confirmed reference; old chip-colour positives follow
    # from saved frames without repeating their seed captures.
    name=f'seed_{len(seeds):06d}';save_frame(out/'search/frames',name,reference)
    paths=[f'search/frames/{name}{suffix}' for suffix in ('.tiff','.json')]
    fresh={'index':None,'requested_xy_um':[reference.position.x,reference.position.y],
        'position_um':asdict(reference.position),'image':paths[0],'hashes':{p:digest(out/p) for p in paths},'reference_reused':True,
        **_seed_classification(reference,model,transform,40.)}
    fresh_index=len(seeds);seeds.append(fresh);positive=[]
    for i,r in enumerate(seeds):
        if not r.get('image'):raise SafetyError('Reference repair cannot discard unobserved old skip decisions.')
        updated=_seed_classification(load_frame(out/r['image']),model,transform,40.)
        changes.append({'seed_record_index':i,'prior_classification':r.get('classification'),'new_classification':updated['classification']})
        r['prior_classification']=r.get('classification');r.update(updated,reference_reclassified=True)
        if updated['classification']=='substrate_seed':positive.append(i)
    if fresh_index not in positive:raise SafetyError('Fresh corrected reference is not a supported seed.')
    positive=[fresh_index]+[i for i in positive if i!=fresh_index];pending=[]
    for i in positive:
        key=f'candidate{len(attempts)+1:03d}'
        a={'id':key,'seed_record_index':i,'seed_xy_um':seeds[i]['requested_xy_um'],
            'state':'queued_reference_seed','folder':str(out/'discovery'/key),'reference_reclassified_seed':True}
        attempts.append(a);pending.append((i,a))
    state.update(seed_records=seeds,boundary_attempts=attempts,progress_continuation={
        'prior_run':old.get('id') or config['context']['prior_run_id'],'preserved_seed_count':len(seeds)-1,
        'reference_reclassified':True,'old_files_modified':False,'resumed_discoveries':[a['id'] for _,a in pending]})
    atomic_json(out/'reference-reclassified-progress.json',{'changes':changes,'old_raw_geometry_preserved_by_lineage':True,
        'qualification':'Previous edge labels invalidated explicitly; no accepted chip/raster work exists or is reset.'})
    atomic_json(out/'progress-reused.json',state['progress_continuation'])
    seen={r['index'] for r in seeds if r.get('index') is not None}
    return {'pending':set(range(search['probe_count']))-seen,'discoveries':pending}
