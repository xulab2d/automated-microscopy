"""Resume an interrupted five-point fit at its missing independent confirmation."""
from dataclasses import asdict
from pathlib import Path
import json
import math
import shutil
from .model import SafetyError,atomic_json
from .chip_discovery import file_hash

def read(p):return json.loads(p.read_text(encoding='utf-8-sig'))

def pending_files(run):
    files=[]
    for path in run.glob('discovery/candidate*/edge-focus/attempt_*/**/quick-focus.json'):
        record=read(path)
        if (record.get('state')!='failed' or not str(record.get('error','')).startswith('Memory recovery exhausted after ')
                or record.get('confirmation') or len(record.get('quick_samples',[]))!=5
                or record.get('fitted_quick_result',{}).get('success') is not True):continue
        observations=[]
        for obs in (run/'observations').glob('*.json'):
            if obs.name.endswith('.vendor.json'):continue
            value=read(obs)
            if value.get('observation_kind')=='quick_focus_sample' and (
                Path(value.get('observation_context_folder','')).resolve()==path.parent.resolve()
                or obs.relative_to(run).as_posix() in record.get('inherited_observation_files',[])):
                observations.append(obs)
        if len(observations)==5:files.extend([path,*observations])
    return [p.relative_to(run).as_posix() for p in files]

def find_pending(folder,context,xy,reference):
    run=next((p for p in (Path(folder),*Path(folder).parents) if p.name.startswith('scan-') and p.parent.name=='data'),None)
    if run is None or not (context or {}).get('prior_run_id'):return None
    contract=read(run/'scan-request.json').get('continuation',{})
    if contract.get('continuation_kind')!='diagnosed_pre_capture_memory_recovery':return None
    if contract['prior_run_id']!=context['prior_run_id']:raise SafetyError('Pending focus parent mismatch')
    parent=(run.parent/contract['prior_run_id']).resolve(strict=True)
    if parent.parent!=run.parent.resolve():raise SafetyError('Pending focus parent escapes project')
    pinned=contract['prior_files_sha256'];observations={}
    for name,sha in pinned.items():
        if name.startswith('observations/') and name.endswith('.json'):
            p=(parent/name).resolve(strict=True)
            if not p.is_relative_to(parent) or file_hash(p)!=sha:raise SafetyError('Pinned focus observation changed')
            observations[name]=read(p)
    for name,sha in pinned.items():
        if not name.startswith('discovery/') or not name.endswith('/quick-focus.json'):continue
        path=(parent/name).resolve(strict=True)
        if not path.is_relative_to(parent) or file_hash(path)!=sha:raise SafetyError('Pinned focus fit changed')
        record=read(path);samples=record.get('quick_samples',[])
        if record.get('state')!='failed' or not record.get('error','').startswith('Memory recovery exhausted after ') or record.get('confirmation') or len(samples)!=5:continue
        from .autofocus import fit_quick_focus_curve
        result=fit_quick_focus_curve(samples)
        if not result.success or abs(result.z_um-record.get('fitted_quick_result',{}).get('z_um',float('inf')))>.001:continue
        matched=[];box=None;valid=True
        for sample in samples:
            matches=[(n,v) for n,v in observations.items() if v.get('measurement')==sample and
                (Path(v.get('observation_context_folder','')).resolve()==path.parent.resolve() or n in record.get('inherited_observation_files',[]))]
            if len(matches)!=1:valid=False;break
            n,v=matches[0];meta=v.get('analysis_frame',{}).get('metadata',{});physical=v.get('position',{})
            if (v.get('kind')!='persisted_vendor_observation' or v.get('observation_kind')!='quick_focus_sample'
                or not v.get('capture',{}).get('job_completed') or not v.get('capture',{}).get('job_unloaded')
                or any(abs(physical.get(a,float('inf'))-float(xy[j]))>.5 for j,a in enumerate('xy'))
                or any(meta.get(k)!=reference.metadata.get(k) for k in ('camera_job_template','camera_resources','objective_position','illumination_percent'))
                or meta.get('focus_mask_policy')!='include_chip_edge'
                or any(abs(a-b)>abs(b)*.01 for a,b in zip(v.get('pixel_size_um',[0,0]),reference.pixel_size_um))):valid=False;break
            candidate=meta.get('focus_only_roi_source_px');h,w=reference.rgb.shape[:2]
            if not isinstance(candidate,list) or len(candidate)!=4 or any(type(i) is not int for i in candidate):valid=False;break
            x0,y0,x1,y1=candidate
            if not(0<=x0<x1<=w and 0<=y0<y1<=h) or (box is not None and box!=candidate):valid=False;break
            box=candidate;matched.append(n)
        if valid:
            return {'source_run':parent.name,'source_quick_fit':name,'source_sha256':{n:pinned[n] for n in [name,*matched]},
                'box':box,'xy':list(xy),'samples':samples,'fitted':asdict(result),'observation_files':matched,
                'reference_metadata':{k:reference.metadata.get(k) for k in ('camera_job_template','camera_resources','objective_position','illumination_percent')},
                'pixel_size_um':list(reference.pixel_size_um)}
    return None

def confirm_pending(scope,pending,output,callback=None):
    from .autofocus import fit_quick_focus_curve,FocusResult,capture_metric,focus_metrics_for_frame,focus_mask_for_frame,checkpoint_observation
    from .motion import move_z_bounded
    output=Path(output);output.mkdir(parents=True,exist_ok=True)
    run=next(p for p in output.parents if p.name.startswith('scan-') and p.parent.name=='data')
    parent=run.parent/pending['source_run'];inherited=[]
    for name in pending['observation_files']:
        p=parent/name
        if file_hash(p)!=pending['source_sha256'][name]:raise SafetyError('Pending observation changed before reuse')
        dest=run/'observations'/('inherited-'+p.name);dest.parent.mkdir(exist_ok=True)
        if dest.exists() and file_hash(dest)!=file_hash(p):raise SafetyError('Inherited observation conflict')
        if not dest.exists():shutil.copy2(p,dest)
        inherited.append(dest.relative_to(run).as_posix())
    fitted=fit_quick_focus_curve(pending['samples'])
    if not fitted.success:raise SafetyError('Pending focus no longer fits a peak')
    record={'schema':1,'state':'confirming','quick_samples':pending['samples'],'fitted_quick_result':asdict(fitted),
        'inherited_observation_files':inherited,'source_proof':pending,'fresh_image_budget':1}
    def save():atomic_json(output/'quick-focus.json',record)
    save()
    try:
        scope.check_cancel();move_z_bounded(scope,fitted.z_um);scope.check_cancel()
        frame,metrics=capture_metric(scope,lambda f:focus_metrics_for_frame(f,focus_mask_for_frame(f)))
        physical=frame.metadata.get('physical_stage_position',asdict(frame.position))
        if (any(abs(physical[a]-pending['xy'][j])>.5 for j,a in enumerate('xy'))
                or frame.metadata.get('focus_only_roi_source_px')!=pending['box']
                or any(frame.metadata.get(k)!=v for k,v in pending['reference_metadata'].items())
                or any(abs(a-b)>abs(b)*.01 for a,b in zip(frame.pixel_size_um,pending['pixel_size_um']))):raise SafetyError('Pending focus confirmation context changed')
        row={'z_um':frame.position.z,'phase':'confirmation',**metrics};record['confirmation']=row;save()
        if callback:callback(frame,row)
        checkpoint_observation(scope,frame,output,'quick_focus_confirmation',row)
        success=(abs(frame.position.z-fitted.z_um)<=getattr(scope,'focus_target_tolerance_um',.25)
            and metrics['texture']>=.015 and metrics['score']>=max(1e-6,.95*max(s['score'] for s in pending['samples'])))
        result=FocusResult(success,frame.position.z,metrics['score'],fitted.prominence,
            'Saved five-point fit independently confirmed.' if success else 'Saved fit failed fresh independent confirmation.',pending['samples']+[row])
        record.update(state='focused' if success else 'unresolved',result=asdict(result));save();return result
    except BaseException as exc:
        record.update(state='failed',error=str(exc));save();raise
