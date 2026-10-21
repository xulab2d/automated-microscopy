"""Budgeted fresh-height focus recovery; no implicit hardware connection.

Sparse search is conditional on a resolved focus response. A narrow peak can
fall between probes; exhaustion is a failure, never a claim of verified focus.
"""
from dataclasses import asdict
from pathlib import Path

import numpy as np

from .autofocus import FocusResult,autofocus_range,focus_metrics,focus_mask_for_frame,focus_metrics_for_frame
from .imaging import save_frame
from .vendor_lifecycle import checkpoint_observation,acknowledge_raw
from .model import AcquisitionError,Cancelled,SafetyError,atomic_json
from .motion import move_z_bounded
from .capture_overlap import capture_metric
from .acquisition_budget import bounded_focus,BudgetExhausted


@bounded_focus(180.,48)
def recover_focus(scope,low,high,output=None,*,initial_step_um=10.,growth=2.,max_probes=24,
                  max_bracket_width_um=6.,coarse_step_um=2.,fine_step_um=.5,
                  callback=None,check_cancel=None):
    """Return a FocusResult after sparse bracketing and a verified local sweep.

    ``low/high`` are explicit current-coordinate absolute Z limits. Start must
    lie strictly inside them. At most ``max_probes`` exploratory images are
    acquired, including two initial repeats. The final existing autofocus uses
    only a bracket no wider than ``max_bracket_width_um`` and is followed by one
    confirmation image. Thus its additional image count is bounded by
    2 + max(3,ceil(width/coarse)+1) + max(3,ceil(2*coarse/fine)+1).

    A failed sparse search leaves the last observed height. Cancellation and
    hardware errors cause no return motion. The caller owns/arms/closes scope.
    The optional output/STOP file is checked inside motion and final autofocus.
    """
    values=[low,high,initial_step_um,growth,max_bracket_width_um,coarse_step_um,fine_step_um]
    if not np.isfinite(values).all() or low>=high or initial_step_um<=0 or growth<=1 or max_bracket_width_um<2*coarse_step_um:
        raise ValueError('Invalid bounded focus recovery parameters.')
    if not isinstance(max_probes,int) or not 6<=max_probes<=64 or not 0<fine_step_um<=coarse_step_um:
        raise ValueError('Need 6..64 exploratory probes and valid fine/coarse steps.')
    if not getattr(scope,'armed',False) or scope.limits is None:
        raise SafetyError('Arm explicit travel limits before focus recovery.')
    initial=scope.position()
    if not scope.limits.z_min<=low<initial.z<high<=scope.limits.z_max:
        raise SafetyError('Recovery interval must contain the current height inside the armed Z limits.')
    requested_bounds=[float(low),float(high)]
    headroom=float(getattr(scope,'focus_target_tolerance_um',.25))+.001
    if not np.isfinite(headroom) or headroom<=0:
        raise SafetyError('Controller focus precision must be finite and positive.')
    low=max(float(low),scope.limits.z_min+headroom)
    high=min(float(high),scope.limits.z_max-headroom)
    if not low<initial.z<high or high-low<2*coarse_step_um:
        raise SafetyError('Recovery interval has insufficient controller readback headroom.')
    if coarse_step_um>scope.limits.max_z_step_um:
        raise SafetyError('Final coarse sampling exceeds the permitted Z increment.')
    out=Path(output) if output is not None else None
    if out:out.mkdir(parents=True,exist_ok=True)
    original_check=scope.check_cancel
    def check():
        original_check()
        if check_cancel:check_cancel()
        if out is not None and (out/'STOP').exists():raise Cancelled('Focus recovery STOP requested.')
    scope.check_cancel=check
    report={'schema':1,'state':'searching','initial_position_um':asdict(initial),'search_bounds_um':[float(low),float(high)],
            'requested_search_bounds_um':requested_bounds,'hard_limit_target_headroom_um':headroom,
            'max_exploratory_images':max_probes,'max_bracket_width_um':max_bracket_width_um,'samples':[],
            'limitation':'Sparse sampling can miss a narrow peak; no completeness or thin-flake sensitivity guarantee.',
            'direction':None,'fallback_used':False}
    exploratory=[];reference=None;mask=None;noise=0.;final_samples=[]
    def save():
        if out:atomic_json(out/'recovery.json',report)
    def validate(frame):
        if reference is None:return
        keys=('camera_job_template','camera_resources','objective_position','illumination_percent','focus_mask_policy')
        if frame.rgb.shape!=reference.rgb.shape or not np.allclose(frame.pixel_size_um,reference.pixel_size_um,rtol=.01,atol=0):
            raise SafetyError('Camera dimensions or objective scale changed during recovery.')
        if any(frame.metadata.get(k)!=reference.metadata.get(k) for k in keys):
            raise SafetyError('Optical acquisition profile changed during recovery.')
        actual=frame.metadata.get('physical_stage_position',asdict(frame.position))
        if np.hypot(actual['x']-initial.x,actual['y']-initial.y)>.5:
            raise SafetyError('XY changed during the Z-only recovery.')
    def points():
        # Repeats describe one height rather than manufacturing a bracket.
        grouped={}
        for s in exploratory:grouped.setdefault(round(s['z_um'],3),[]).append(s)
        return sorted([{'z_um':float(np.mean([s['z_um'] for s in rows])),
                        'score':float(np.median([s['score'] for s in rows])),
                        'texture':float(np.median([s['texture'] for s in rows]))} for rows in grouped.values()],key=lambda s:s['z_um'])
    def probe(z,phase,repeat=False):
        nonlocal reference,mask
        check();z=float(np.clip(z,low,high))
        if not repeat and any(abs(z-s['requested_z_um'])<.001 for s in exploratory):return
        if len(exploratory)>=max_probes:return
        move_z_bounded(scope,z);check();frame,metrics=capture_metric(scope,lambda f:focus_metrics_for_frame(f,mask if mask is not None else focus_mask_for_frame(f)));validate(frame)
        if reference is None:reference=frame;mask=focus_mask_for_frame(frame)
        sample={'z_um':frame.position.z,'requested_z_um':z,'phase':phase,**metrics}
        previous=max((s['score'] for s in exploratory),default=-1)
        exploratory.append(sample);report['samples'].append(sample)
        if sample['score']>previous:
            report['best_observed']=dict(sample)
            if out:save_frame(out,'best_observed',frame)
        save()
        if callback:callback(frame,sample)
        checkpoint_observation(scope,frame,out,'focus_recovery_sample',sample,
            selected_raw=(out/'best_observed.tiff') if out and sample['score']>previous else None)
    def bracket():
        p=points()
        if len(p)<3:return None
        k=int(np.argmax([s['score'] for s in p]));best=p[k]
        if best['texture']<.015 or best['score']<1e-6:return None
        margin=max(2*noise,.003*best['score'],1e-10)
        left=[s for s in p[:k] if best['score']-s['score']>margin]
        right=[s for s in p[k+1:] if best['score']-s['score']>margin]
        return (left[-1],best,right[0]) if left and right else None
    def failure(reason):
        p=points();best=max(p,key=lambda s:s['score']) if p else {'score':0.}
        result=FocusResult(False,scope.position().z,best['score'],0.,reason,list(report['samples']))
        report.update(state='unresolved',result=asdict(result));save();return result
    try:
        check()
        if getattr(scope,'lease',None):scope.lease.acquire()
        probe(initial.z,'initial');probe(initial.z,'initial_repeat',repeat=True)
        noise=abs(exploratory[0]['score']-exploratory[1]['score'])/np.sqrt(2)
        report['initial_repeat_score_noise']=float(noise)
        for distance in (initial_step_um,2*initial_step_um):
            for sign in (-1,1):probe(initial.z+sign*distance,'direction_probe')
            if bracket() is not None:break
        if bracket() is None:
            p=points();best=max(p,key=lambda s:s['score']);start=np.median([s['score'] for s in exploratory[:2]])
            if best['score']-start>max(3*noise,.05*max(best['score'],1e-12)):
                direction=1 if best['z_um']>initial.z else -1
                report['direction']=direction;distance=2*initial_step_um
                while len(exploratory)<max_probes and bracket() is None:
                    distance*=growth;target=float(np.clip(initial.z+direction*distance,low,high))
                    if any(abs(target-s['requested_z_um'])<.001 for s in exploratory):break
                    probe(target,'exponential_bracket')
                    if target in (low,high):break
        # Reserve a few probes for tightening; only one bounded fallback search.
        fallback_limit=max(6,max_probes-6)
        while bracket() is None and len(exploratory)<fallback_limit:
            report['fallback_used']=True
            missing=[z for z in (low,high) if not any(abs(z-s['requested_z_um'])<.001 for s in exploratory)]
            if missing:target=missing[0]
            else:
                z=sorted(s['z_um'] for s in points());j=int(np.argmax(np.diff(z)));target=(z[j]+z[j+1])/2
            before=len(exploratory);probe(target,'bounded_gap_fallback')
            if len(exploratory)==before:break
        b=bracket()
        while b is not None and b[2]['z_um']-b[0]['z_um']>max_bracket_width_um and len(exploratory)<max_probes:
            left,best,right=b
            target=(left['z_um']+best['z_um'])/2 if best['z_um']-left['z_um']>=right['z_um']-best['z_um'] else (best['z_um']+right['z_um'])/2
            before=len(exploratory);probe(target,'tighten_bracket');b=bracket()
            if len(exploratory)==before:break
        if b is None:return failure('No resolved focus bracket within the exploratory image budget.')
        lo,hi=b[0]['z_um'],b[2]['z_um']
        if hi-lo>max_bracket_width_um:return failure('Resolved bracket remains too wide for the bounded final sweep.')
        # Readback rounding may extend an endpoint slightly beyond a requested
        # search boundary; the final sweep remains in the declared interval.
        lo=max(float(lo),float(low));hi=min(float(hi),float(high))
        report.update(state='verifying_local_peak',bracket_um=[lo,hi]);save()
        def final_progress(frame,sample):
            validate(frame);sample={**sample,'phase':'verification_'+sample['phase']}
            # Final autofocus forms its own mask near focus. Retain a second
            # score on our fixed search mask for an apples-to-apples repeat.
            sample['recovery_mask_score']=focus_metrics_for_frame(frame,mask)['score']
            final_samples.append(sample);report['samples'].append(sample);save()
            if callback:callback(frame,sample)
        result=autofocus_range(scope,lo,hi,coarse_step_um,fine_step_um,out/'verification' if out else None,final_progress,
                               resolved_bracket=True)
        if result.success:
            check();frame,measured=capture_metric(scope,lambda f:focus_metrics_for_frame(f,mask));validate(frame)
            report['confirmation']={'z_um':frame.position.z,**measured}
            tolerance=float(getattr(scope,'focus_target_tolerance_um',.25))
            same_mask_best=max(s['recovery_mask_score'] for s in final_samples)
            if abs(frame.position.z-result.z_um)>tolerance or measured['texture']<.015 or measured['score']<max(1e-6,.75*same_mask_best):
                result.success=False;result.reason='Final focused-image confirmation failed.'
            elif out:save_frame(out,'verified_focus',frame)
            save()
            checkpoint_observation(scope,frame,out,'focus_recovery_confirmation',report['confirmation'],
                selected_raw=(out/'verified_focus.tiff') if out and result.success else None)
        result.samples=list(report['samples']);result.z_um=scope.position().z
        report.update(state='focused' if result.success else 'unresolved',result=asdict(result),exploratory_images=len(exploratory));save()
        return result
    except BudgetExhausted as exc:
        report.update(state='unresolved',outcome=exc.outcome,error=str(exc));save();raise
    except Cancelled:
        report.update(state='stopped');save();raise
    except BaseException as exc:
        report.update(state='failed',error=str(exc));save();scope.stop();raise
    finally:
        scope.check_cancel=original_check
