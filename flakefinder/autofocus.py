from __future__ import annotations
from dataclasses import dataclass, asdict
from pathlib import Path
import cv2, numpy as np
from .model import AcquisitionError, SafetyError, Cancelled, atomic_json
from .motion import move_z_bounded

@dataclass
class FocusResult:
    success: bool
    z_um: float
    score: float
    prominence: float
    reason: str
    samples: list

def make_focus_mask(rgb):
    """Exclude a dark holder and chip edge when a bright substrate is resolved."""
    g=rgb[:,:,1].astype(np.float32)
    lo,hi=np.percentile(g,[1,99])
    scaled=np.uint8(np.clip((g-lo)/max(hi-lo,1)*255,0,255))
    _,mask=cv2.threshold(scaled,0,255,cv2.THRESH_BINARY|cv2.THRESH_OTSU)
    keep=mask>0;fraction=keep.mean()
    if not .1<fraction<.99:return np.ones(g.shape,np.uint8)
    if np.median(g[keep])<2.5*max(float(np.median(g[~keep])),1):return np.ones(g.shape,np.uint8)
    return np.uint8(cv2.erode(mask,np.ones((51,51),np.uint8))>0)

def focus_mask_for_frame(frame):
    if frame.metadata.get('focus_mask_policy')=='include_chip_edge':
        return np.ones(frame.rgb.shape[:2],np.uint8)
    return make_focus_mask(frame.rgb)


def focus_metrics_for_frame(frame,mask=None):
    if frame.metadata.get('focus_mask_policy')=='include_chip_edge':
        return {**focus_metrics(frame.rgb,mask,mode='chip_edge'),
                'focus_metric':'chip_edge_mean_gradient_energy'}
    return focus_metrics(frame.rgb,mask)


def focus_metrics(rgb,mask=None,*,mode='specimen'):
    """Multiscale gradients; suppress sensor noise before differentiating.

    Use the green channel and fixed normalization, never per-frame auto contrast.
    Low-frequency texture must be present to accept a peak.
    A registered chip-edge ROI uses mean gradient energy over every valid
    pixel. Its green mean normalization avoids a discontinuous median when
    bright substrate occupies almost exactly half the fixed ROI.
    """
    if mode not in ('specimen','chip_edge'):raise ValueError('Unknown focus metric mode.')
    g=rgb[:,:,1].astype(np.float32)
    valid=np.ones(g.shape,np.uint8) if mask is None else np.uint8(mask>0)
    if max(g.shape)>1024:
        valid=cv2.resize(valid,None,fx=1024/max(g.shape),fy=1024/max(g.shape),interpolation=cv2.INTER_NEAREST)
        g=cv2.resize(g,None,fx=1024/max(g.shape),fy=1024/max(g.shape),interpolation=cv2.INTER_AREA)
    valid=valid.astype(bool)
    if valid.sum()<100:return {'score':0.,'texture':0.,'noise':0.,'mean':float(g.mean())}
    normalization=np.mean(g[valid]) if mode=='chip_edge' else np.median(g[valid])
    g=g/(float(normalization)+1)
    smooth=cv2.GaussianBlur(g,(0,0),1.6)
    gx=cv2.Sobel(smooth,cv2.CV_32F,1,0,ksize=3)
    gy=cv2.Sobel(smooth,cv2.CV_32F,0,1,ksize=3)
    energy=gx*gx+gy*gy
    if mode=='chip_edge':
        score=float(np.mean(energy[valid]))
    else:
        # Tile aggregation resists dust in specimen mode. For chip-edge focus
        # it would discard the very boundary whose sharpness we must measure.
        tiles=[]
        for row,mask_row in zip(np.array_split(energy,4,axis=0),np.array_split(valid,4,axis=0)):
            for t,m in zip(np.array_split(row,4,axis=1),np.array_split(mask_row,4,axis=1)):
                if m.sum()>max(100,.2*m.size):tiles.append(float(np.mean(t[m])))
        if not tiles:return {'score':0.,'texture':0.,'noise':0.,'mean':float(g.mean())}
        tiles=np.sort(tiles);score=float(np.mean(tiles[int(.2*len(tiles)):max(int(.9*len(tiles)),1)]))
    coarse=cv2.GaussianBlur(g,(0,0),3)
    texture=float(np.percentile(coarse[valid],95)-np.percentile(coarse[valid],5))
    noise=float(np.median(np.abs(g-cv2.GaussianBlur(g,(0,0),0.8))[valid]))
    return {'score':score,'texture':texture,'noise':noise,'mean':float(rgb[:,:,1].mean())}

def evaluate_curve(samples,min_prominence=0.15,min_texture=0.015):
    scores=np.asarray([s['score'] for s in samples])
    k=int(np.argmax(scores))
    best=samples[k]
    base=float(np.percentile(scores,20))
    prominence=float((scores[k]-base)/max(scores[k],1e-12))
    reason='Focused'
    ok=True
    if k in (0,len(samples)-1): ok,reason=False,'Peak at search boundary; extend the approved Z range or seed focus manually.'
    elif prominence<min_prominence: ok,reason=False,'Flat focus curve; no resolved focus peak.'
    elif best['texture']<min_texture: ok,reason=False,'Insufficient specimen texture; image may be blank or noise.'
    elif best['score']<1e-6: ok,reason=False,'Focus signal too weak.'
    return FocusResult(ok,best['z_um'],best['score'],prominence,reason,samples)

def evaluate_fine_curve(samples):
    """A broad low-NA focus peak can vary only 1% over a fine sweep.

    Require concave curvature and a peak resolved above fit residuals rather
    than demanding a large percentage change over a deliberately small range.
    """
    result=evaluate_curve(samples,min_prominence=0.002)
    if not result.success:return result
    z=np.asarray([v['z_um'] for v in samples]);scores=np.asarray([v['score'] for v in samples])
    x=z-z.mean();fit=np.polyfit(x,scores,2);pred=np.polyval(fit,x)
    residual=scores-pred
    noise=max(float(np.median(abs(residual-np.median(residual)))*1.4826),1e-12)
    dynamic=float(scores.max()-scores.min())
    vertex=-fit[1]/(2*fit[0]) if fit[0]<0 else float('inf')
    r2=1-float(np.sum(residual**2))/max(float(np.sum((scores-scores.mean())**2)),1e-12)
    if not (fit[0]<0 and x.min()<vertex<x.max() and dynamic>6*noise and r2>0.75):
        result.success=False;result.reason='Fine focus curvature is not resolved above image noise.'
    return result


def fit_quick_focus_curve(samples):
    """Resolve a quadratic vertex from five measured, ordered focus samples."""
    if len(samples)!=5:raise ValueError('Quick focus needs exactly five samples.')
    z=np.asarray([s['z_um'] for s in samples],float)
    if not np.isfinite(z).all() or np.any(np.diff(z)<=0):
        raise ValueError('Quick focus samples need distinct increasing actual heights.')
    result=evaluate_fine_curve(samples)
    if not result.success:return result
    origin=float(z.mean());fit=np.polyfit(z-origin,[s['score'] for s in samples],2)
    result.z_um=float(origin-fit[1]/(2*fit[0]))
    result.reason='Quick quadratic focus peak; confirmation image still required.'
    return result


def quick_autofocus(scope,half_range_um=2.,output=None,callback=None,*,
                    fallback_half_range_um=12.,fallback_coarse_step_um=3.,fallback_fine_step_um=.5):
    """Five local samples, fitted-peak confirmation, then full sweep if needed.

    Intended for a plane-seeded location. A successful quick path takes six
    images. Unresolved/noisy/boundary peaks retain the established autofocus
    fallback; set fallback_half_range_um=None to return the unresolved result.
    The caller still owns ROI selection, travel, limits, lease and scope life.
    """
    if not np.isfinite(half_range_um) or half_range_um<=0:raise ValueError('Invalid quick focus range.')
    if not getattr(scope,'armed',False) or scope.limits is None:raise SafetyError('Arm a travel envelope before quick autofocus.')
    initial=scope.position();tolerance=float(getattr(scope,'focus_target_tolerance_um',.25));guard=tolerance+.001
    if not np.isfinite(tolerance) or tolerance<=0 or half_range_um<2*tolerance:
        raise SafetyError('Quick focus range is too small for the controller positioning precision.')
    low,high=initial.z-half_range_um,initial.z+half_range_um
    if low<scope.limits.z_min+guard or high>scope.limits.z_max-guard:
        raise SafetyError('Quick focus interval lacks controller readback headroom at a Z limit.')
    fallback=None
    if fallback_half_range_um is not None:
        if not np.isfinite([fallback_half_range_um,fallback_coarse_step_um,fallback_fine_step_um]).all() or fallback_half_range_um<=0 or not 0<fallback_fine_step_um<=fallback_coarse_step_um:
            raise ValueError('Invalid full-sweep fallback parameters.')
        fallback=[max(scope.limits.z_min+guard,initial.z-fallback_half_range_um),
                  min(scope.limits.z_max-guard,initial.z+fallback_half_range_um)]
        if fallback[1]-fallback[0]<2*fallback_coarse_step_um:raise SafetyError('Insufficient bounded Z interval for the configured fallback.')
    from .storage import managed_output_path
    folder=managed_output_path(output) if output is not None else None
    if folder:folder.mkdir(parents=True,exist_ok=True)
    record={'schema':1,'state':'sampling','initial_position_um':asdict(initial),'quick_bounds_um':[low,high],
            'fallback_bounds_um':fallback,'quick_samples':[],'quick_path_image_budget':6}
    samples=record['quick_samples'];seed_frame=None;mask=None
    def save():
        if folder:atomic_json(folder/'quick-focus.json',record)
    def measured(frame,phase):
        nonlocal seed_frame,mask
        if seed_frame is None:seed_frame=frame;mask=focus_mask_for_frame(frame)
        keys=('camera_job_template','camera_resources','objective_position','illumination_percent','focus_mask_policy')
        if frame.rgb.shape!=seed_frame.rgb.shape or not np.allclose(frame.pixel_size_um,seed_frame.pixel_size_um,rtol=.01,atol=0) or any(frame.metadata.get(k)!=seed_frame.metadata.get(k) for k in keys):
            raise SafetyError('Optical profile or focus ROI changed during quick autofocus.')
        physical=frame.metadata.get('physical_stage_position',asdict(frame.position))
        if np.hypot(physical['x']-initial.x,physical['y']-initial.y)>.5:raise SafetyError('XY moved during quick autofocus.')
        row={'z_um':frame.position.z,'phase':phase,**focus_metrics_for_frame(frame,mask)}
        if callback:callback(frame,row)
        return row
    try:
        for z in np.linspace(low,high,5):
            scope.check_cancel();move_z_bounded(scope,float(z));scope.check_cancel()
            samples.append(measured(scope.capture(),'quick'));save()
        try:result=fit_quick_focus_curve(samples)
        except ValueError as exc:result=FocusResult(False,scope.position().z,0.,0.,str(exc),list(samples))
        record['fitted_quick_result']=asdict(result)
        if result.success:
            move_z_bounded(scope,result.z_um);scope.check_cancel();frame=scope.capture();confirmation=measured(frame,'confirmation')
            record['confirmation']=confirmation
            valid=(abs(frame.position.z-result.z_um)<=tolerance and confirmation['texture']>=.015 and
                   confirmation['score']>=max(1e-6,.95*max(s['score'] for s in samples)))
            if valid:
                result=FocusResult(True,frame.position.z,confirmation['score'],result.prominence,
                                   'Quick quadratic focus verified by a confirmation image.',list(samples)+[confirmation])
            else:result=FocusResult(False,frame.position.z,confirmation['score'],result.prominence,
                                    'Quick fitted peak failed its confirmation image.',list(samples)+[confirmation])
        if not result.success and fallback is not None:
            record.update(state='full_sweep_fallback',quick_failure=result.reason);save()
            result=autofocus_range(scope,*fallback,fallback_coarse_step_um,fallback_fine_step_um,
                                   folder/'fallback' if folder else None,callback)
        record.update(state='focused' if result.success else 'unresolved',result=asdict(result));save()
        if folder:atomic_json(folder/'focus-result.json',asdict(result))
        return result
    except Cancelled:
        record.update(state='stopped');save();raise
    except BaseException as exc:
        record.update(state='failed',error=str(exc));save();scope.stop();raise

def autofocus(scope,half_range_um=20,coarse_step_um=4,fine_step_um=0.5,output=None,callback=None):
    if not np.isfinite(half_range_um) or half_range_um<=0:
        raise ValueError('Invalid autofocus sampling parameters.')
    initial=scope.position()
    return autofocus_range(scope,initial.z-half_range_um,initial.z+half_range_um,
                           coarse_step_um,fine_step_um,output,callback)


def autofocus_range(scope,low,high,coarse_step_um=5,fine_step_um=0.5,output=None,callback=None,*,resolved_bracket=False):
    """Search an explicit absolute Z interval, including an initially defocused sample.

    Range is never expanded automatically. Sampling must be fine enough to intersect
    the objective's focus response; a coarse step is not a guarantee of detection.
    ``resolved_bracket`` is for a local interval already bracketed by a fresh
    wider sweep. It permits low prominence only when the existing curvature
    and fit-noise checks resolve an interior peak; it never accepts a flat fit.
    """
    if not np.isfinite([low,high,coarse_step_um,fine_step_um]).all() or low>=high or coarse_step_um<=0 or not 0<fine_step_um<=coarse_step_um:
        raise ValueError('Invalid autofocus sampling parameters.')
    if scope.limits is None or not scope.armed: raise SafetyError('Arm a travel envelope before autofocus.')
    initial=scope.position()
    if low<scope.limits.z_min or high>scope.limits.z_max:
        raise SafetyError('Autofocus range would cross a Z limit; it was not clipped silently.')
    if coarse_step_um>scope.limits.max_z_step_um:
        raise SafetyError('Autofocus sampling step exceeds Z increment limit.')
    from .storage import managed_output_path
    if output is not None:output=managed_output_path(output)
    all_samples=[]
    seed_frame=scope.capture()
    focus_mask=focus_mask_for_frame(seed_frame)
    if output:
        Path(output).mkdir(parents=True,exist_ok=True)
        cv2.imwrite(str(Path(output)/'focus-mask.png'),focus_mask*255)
    def to_z(z):
        move_z_bounded(scope,z)
    def sweep(start,end,step,phase):
        samples=[]
        count=max(3,int(np.ceil((end-start)/step))+1)
        if count>1001: raise SafetyError('Autofocus exceeds 1001 samples per sweep.')
        for z in np.linspace(start,end,count):
            scope.check_cancel()
            to_z(float(z))
            f=scope.capture()
            if f.rgb.shape!=seed_frame.rgb.shape or not np.allclose(f.pixel_size_um,seed_frame.pixel_size_um,rtol=.01,atol=0):
                raise SafetyError('Camera dimensions or objective scale changed during autofocus.')
            if f.metadata.get('focus_mask_policy')!=seed_frame.metadata.get('focus_mask_policy'):
                raise SafetyError('Focus mask policy changed during autofocus.')
            sample={'z_um':f.position.z,'phase':phase,**focus_metrics_for_frame(f,focus_mask)}
            samples.append(sample); all_samples.append(sample)
            if output:
                atomic_json(Path(output)/'focus-curve.json',{'samples':all_samples})
            if callback: callback(f,sample)
        return samples
    try:
        coarse=sweep(low,high,coarse_step_um,'coarse')
        result=evaluate_curve(coarse)
        if not result.success and result.reason.startswith('Flat focus curve') and (result.prominence>.03 or resolved_bracket):
            resolved=evaluate_fine_curve(coarse)
            if resolved.success:
                result=resolved
                result.reason='Focus peak resolved by concave curvature above measurement noise.'
        if result.success:
            finer=sweep(max(low,result.z_um-coarse_step_um),min(high,result.z_um+coarse_step_um),fine_step_um,'fine')
            fine=evaluate_fine_curve(finer)
            if fine.success: result=fine
            else: result=FocusResult(False,initial.z,fine.score,fine.prominence,'Fine sweep failed: '+fine.reason,all_samples)
        to_z(result.z_um if result.success else initial.z)
        result.z_um=scope.position().z
        result.samples=all_samples
        if output: atomic_json(Path(output)/'focus-result.json',asdict(result))
        return result
    except Cancelled:
        # A requested stop must not trigger a return motion.
        raise
    except BaseException:
        scope.stop()
        raise
