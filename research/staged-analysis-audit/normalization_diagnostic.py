"""Isolated fit diagnostic; no production imports are edited or monkeypatched."""
from audit_saved_outputs import *

def sampled_fit(rgb,mask,degree=2):
    h,w=rgb.shape[:2];stride=max(1,int(np.sqrt(h*w/30000)))
    xx,yy=np.meshgrid(np.linspace(-1,1,w)[::stride],np.linspace(-1,1,h)[::stride])
    X=np.stack([np.ones_like(xx),xx,yy,xx*xx,xx*yy,yy*yy],-1).reshape(-1,6)[:,:{0:1,1:3,2:6}[degree]]
    Y=np.asarray(rgb[::stride,::stride],np.float32).reshape(-1,3)
    base=mask[::stride,::stride].reshape(-1);keep=base.copy()
    if keep.sum()<100:return {'status':'insufficient_measured_substrate','samples':int(keep.sum())},None
    for _ in range(6):
        coeff=np.linalg.lstsq(X[keep],Y[keep],rcond=None)[0];pred=X@coeff
        residual=np.linalg.norm((Y-pred)/np.maximum(pred,1),axis=1)
        med=np.median(residual[keep]);mad=max(np.median(np.abs(residual[keep]-med))*1.4826,.001)
        update=base&(residual<med+2.5*mad)
        if update.sum()<100:break
        keep=update
    pred=X@coeff
    return {'status':'fit','sample_stride':stride,'initial_samples':int(base.sum()),'retained_samples':int(keep.sum()),
      'degree':degree,'sampled_design_condition':float(np.linalg.cond(X[keep])),
      'minimum_anywhere_rgb':pred.min(axis=0).tolist(),'minimum_inside_fit_support_rgb':pred[base].min(axis=0).tolist(),
      'nonpositive_anywhere_fraction':float((pred<=0).any(axis=1).mean()),
      'nonpositive_inside_fit_support_fraction':float((pred[base]<=0).any(axis=1).mean()),
      'robust_relative_residual_median':float(np.median(residual[keep])),
      'retained_relative_residual_p90':float(np.percentile(residual[keep],90)),
      'coefficients':coeff.tolist()},pred.reshape(xx.shape+(3,))

def main():
    from flakefinder.analysis import tile_region
    from flakefinder.model import CameraTransform
    manifest=read(CHIP/'manifest.json');results=[]
    for number in (0,1,7,32,180,354):
        tile=f'tile_{number:06d}';frame=load_frame(CHIP/'tiles'/f'{tile}.tiff')
        item={'manifest':manifest,'transform':CameraTransform(**manifest['transform'])}
        mask=tile_region(item,frame)
        radius=int(np.ceil(12/min(frame.pixel_size_um)))
        original=cv2.erode(mask,np.ones((2*radius+1,2*radius+1),np.uint8)).astype(bool)
        labels=np.load(CHIP/'boundary-labels'/f'{tile}.npz')['labels']
        known=cv2.resize(np.uint8(labels==1),(frame.rgb.shape[1],frame.rgb.shape[0]),interpolation=cv2.INTER_NEAREST)
        # Same physical edge clearance as production, now applied to observed support.
        known=cv2.erode(known,np.ones((2*radius+1,2*radius+1),np.uint8)).astype(bool)
        entry={'tile_id':tile,'known_support_fraction':float(known.mean())}
        entry['original_polygon_fit'],_=sampled_fit(frame.rgb,original)
        entry['observed_substrate_quadratic_fit'],_=sampled_fit(frame.rgb,known)
        entry['observed_substrate_affine_fit'],_=sampled_fit(frame.rgb,known,degree=1)
        results.append(entry)
    write('normalization-fit-diagnostic.json',results)
    for r in results:
        print(r['tile_id'],r['known_support_fraction'])
        for k in ('original_polygon_fit','observed_substrate_quadratic_fit','observed_substrate_affine_fit'):
            d=r[k];print(k,{x:d.get(x) for x in ('status','initial_samples','minimum_anywhere_rgb','minimum_inside_fit_support_rgb','retained_relative_residual_p90')})
if __name__=='__main__':main()
