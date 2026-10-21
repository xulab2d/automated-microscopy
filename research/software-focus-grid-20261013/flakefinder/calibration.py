from dataclasses import asdict
import cv2, numpy as np
from scipy.spatial import Delaunay, ConvexHull
from .model import CameraTransform, AcquisitionError, SafetyError, Position, Cancelled

def register_translation(a,b):
    """Return the displacement of sample features in image b relative to a."""
    x=a[:,:,1].astype(np.float32); y=b[:,:,1].astype(np.float32)
    if x.shape!=y.shape: raise AcquisitionError('Registration requires identical camera dimensions.')
    # Suppress illumination ramps and flat background before phase correlation.
    x=x-cv2.GaussianBlur(x,(0,0),12)
    y=y-cv2.GaussianBlur(y,(0,0),12)
    window=cv2.createHanningWindow((x.shape[1],x.shape[0]),cv2.CV_32F)
    shift,response=cv2.phaseCorrelate(x,y,window)
    if not np.isfinite(shift).all() or response<0.3:
        return register_features(a,b)
    return np.asarray(shift),float(response)

def register_features(a,b):
    """Translation consensus from specimen keypoints when a long edge dominates FFT registration."""
    def features(rgb):
        g=rgb[:,:,1].astype(np.float32)
        lo,hi=np.percentile(g,[.5,99.5])
        g=np.uint8(np.clip((g-lo)/max(hi-lo,1)*255,0,255))
        g=cv2.createCLAHE(clipLimit=3,tileGridSize=(8,8)).apply(g)
        return cv2.SIFT_create(nfeatures=3000,contrastThreshold=.015).detectAndCompute(g,None)
    ka,da=features(a);kb,db=features(b)
    if da is None or db is None:raise AcquisitionError('No specimen features available for registration.')
    pairs=cv2.BFMatcher().knnMatch(da,db,k=2)
    good=[p[0] for p in pairs if len(p)==2 and p[0].distance<.72*p[1].distance]
    if len(good)<8:raise AcquisitionError('Fewer than eight independent feature matches for calibration.')
    src=np.array([ka[m.queryIdx].pt for m in good]);dst=np.array([kb[m.trainIdx].pt for m in good])
    delta=dst-src;shift=np.median(delta,axis=0)
    inliers=np.linalg.norm(delta-shift,axis=1)<1.5
    if inliers.sum()<8 or inliers.mean()<.65 or np.ptp(src[inliers],axis=0).min()<30:
        raise AcquisitionError('Specimen feature translations do not agree reliably.')
    return np.median(delta[inliers],axis=0),float(inliers.mean())

def calibrate_camera(scope,step_um=20,output=None):
    from .storage import managed_output_path
    if output is not None:output=managed_output_path(output)
    if not scope.armed: raise SafetyError('Arm a local travel envelope first.')
    if step_um<=0 or step_um>100: raise ValueError('Calibration step must be in (0,100] um.')
    scope.expected_scale=None  # explicit recalibration establishes the current objective's scale
    origin=scope.position()
    scope.travel(origin.x,origin.y)  # establishes a consistent mechanical approach
    base=scope.capture(); shifts=[]; responses=[]; commanded=[]
    scope.expected_scale=base.pixel_size_um
    if output:
        from .imaging import save_frame
        save_frame(output,'origin',base)
    try:
        for index,(dx,dy) in enumerate([(step_um,0),(0,step_um),(-step_um,0),(0,-step_um)]):
            scope.travel(origin.x+dx,origin.y+dy)
            f=scope.capture()
            if output:save_frame(output,f'shift_{index}',f)
            shift,response=register_translation(base.rgb,f.rgb)
            shifts.append(shift); responses.append(response);commanded.append([f.position.x-origin.x,f.position.y-origin.y])
            scope.travel(origin.x,origin.y)
        J=np.linalg.lstsq(np.array(commanded),np.array(shifts),rcond=None)[0].T
        if abs(np.linalg.det(J))<1e-6 or np.linalg.cond(J)>3:
            raise AcquisitionError('Degenerate stage/camera transform; registration failed.')
        matrix=-np.linalg.inv(J)
        norms=np.linalg.norm(matrix,axis=0)
        if not np.allclose(norms,base.pixel_size_um,rtol=0.15):
            raise AcquisitionError('Measured stage scale disagrees with camera metadata by more than 15%.')
        residual=np.array(shifts)+np.array(commanded)@np.linalg.inv(matrix).T
        max_error=float(np.linalg.norm(residual@matrix.T,axis=1).max())
        returned=scope.capture()
        if output:save_frame(output,'returned',returned)
        return_shift,return_response=register_translation(base.rgb,returned.rgb)
        return_error=float(np.linalg.norm(matrix@return_shift))
        quality={'responses':responses,'feature_shifts_px':[s.tolist() for s in shifts],
            'max_fit_error_um':max_error,'return_error_um':return_error,'return_response':return_response}
        if output:
            from .model import atomic_json
            from pathlib import Path
            atomic_json(Path(output)/'quality.json',quality)
        if max_error>1 or return_error>1:
            raise AcquisitionError(f'Camera/stage calibration is not repeatable: fit error {max_error:.2f} µm, return error {return_error:.2f} µm. Check settling, backlash and specimen stability.')
        return CameraTransform(matrix.tolist(),base.pixel_size_um,True),quality
    except Cancelled: raise
    except BaseException:
        # No automatic recovery movement after an uncertain hardware error.
        scope.stop()
        raise

class FocusMap:
    """Robust per-chip plane, used only inside the sampled convex hull."""
    def __init__(self,points,max_residual_um=3):
        a=np.asarray(points,dtype=float)
        if a.ndim!=2 or a.shape[1]!=3 or len(a)<3 or not np.isfinite(a).all():
            raise ValueError('A focus map needs at least three finite XYZ samples.')
        self.origin=np.mean(a[:,:2],axis=0)
        X=np.column_stack([a[:,:2]-self.origin,np.ones(len(a))])
        if np.linalg.matrix_rank(X)<3: raise ValueError('Focus map anchors must not be collinear.')
        good=np.ones(len(a),bool)
        for _ in range(4):
            coeff=np.linalg.lstsq(X[good],a[good,2],rcond=None)[0]
            residual=abs(X@coeff-a[:,2]); new=residual<=max_residual_um
            if new.sum()<3: raise AcquisitionError('Focus anchors do not define a consistent plane.')
            if np.array_equal(new,good): break
            good=new
        self.coeff=coeff; self.points=a[good]
        self.hull=Delaunay(self.points[:,:2])
        self.hull_equations=ConvexHull(self.points[:,:2]).equations
        self.rms_um=float(np.sqrt(np.mean((X[good]@coeff-a[good,2])**2)))

    def predict(self,x,y):
        if self.hull.find_simplex([[x,y]])[0]<0:
            # ZEISS reports coordinates rounded to 0.001 um. A requested hull
            # corner can therefore be a fraction of a nanometre outside its
            # measured counterpart. Accept only this readback rounding shell.
            distances=self.hull_equations[:,:2]@[x,y]+self.hull_equations[:,2]
            if np.max(distances)>.002:
                raise AcquisitionError('Position lies outside the measured focus map; autofocus is required.')
        return float(np.dot([x-self.origin[0],y-self.origin[1],1],self.coeff))
