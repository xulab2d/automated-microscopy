from pathlib import Path
from dataclasses import asdict
from functools import lru_cache
import cv2, numpy as np
from PIL import Image
from .model import Frame, Position, atomic_json, AcquisitionError

@lru_cache(maxsize=16,typed=True)
def _preview_lut(levels,white_level):
    # Use the original arithmetic (including scalar dtype promotion) for
    # every possible camera level. The bounded cache holds at most 1 MiB.
    lut=np.uint8(np.clip(np.arange(levels,dtype=np.float32)/white_level,0,1)**0.6*255)
    lut.flags.writeable=False
    return lut

def preview(rgb,white_level=None):
    # Preview scaling is separate from saved scientific data.
    if white_level is None: white_level=max(float(np.percentile(rgb,99.7))*1.05,1)
    if (rgb.dtype.kind=='u' and rgb.dtype.itemsize<=2 and
            np.isscalar(white_level) and np.isfinite(white_level) and white_level>0):
        return _preview_lut(1 << (8*rgb.dtype.itemsize),white_level)[rgb]
    return np.uint8(np.clip(rgb.astype(np.float32)/white_level,0,1)**0.6*255)

def save_frame(folder,name,frame):
    from .storage import managed_output_path, StorageUnavailable
    folder=Path(folder)
    if managed_output_path(folder)!=folder.resolve():
        raise StorageUnavailable('Raw output still points at the local checkout. Resolve the run destination before capture.')
    folder.mkdir(parents=True,exist_ok=True)
    temp=folder/(name+'.tmp.tiff')
    if not cv2.imwrite(str(temp),frame.rgb[:,:,::-1]): raise OSError('Could not save raw RGB TIFF.')
    temp.replace(folder/(name+'.tiff'))
    Image.fromarray(preview(frame.rgb,frame.metadata.get('white_level'))).save(folder/(name+'.jpg'),quality=90)
    atomic_json(folder/(name+'.json'),{'position':asdict(frame.position),'pixel_size_um':frame.pixel_size_um,
        'sequence':frame.sequence,'acquired_at':frame.acquired_at,'metadata':frame.metadata})

def load_frame(path):
    import json
    path=Path(path)
    a=cv2.imread(str(path),cv2.IMREAD_UNCHANGED)
    if a is None: raise AcquisitionError(f'Cannot read {path}')
    d=json.loads(path.with_suffix('.json').read_text(encoding='utf-8'))
    if a.ndim==2: a=np.repeat(a[:,:,None],3,axis=2)
    else: a=a[:,:,::-1]
    return Frame(a,Position(**d['position']),tuple(d['pixel_size_um']),d['sequence'],d['acquired_at'],d['metadata'])

def background_surface(rgb,mask=None):
    """Robust quadratic substrate estimate; large debris is iteratively excluded."""
    h,w=rgb.shape[:2]
    stride=max(1,int(np.sqrt(h*w/30000)))
    # Only sampled pixels participate in the robust fit. Keep that fit in
    # float64 without allocating a full H x W x 6 design matrix.
    x=np.linspace(-1,1,w,dtype=np.float64)
    y=np.linspace(-1,1,h,dtype=np.float64)
    xx,yy=np.meshgrid(x[::stride],y[::stride])
    X=np.stack([np.ones_like(xx),xx,yy,xx*xx,xx*yy,yy*yy],axis=-1).reshape(-1,6)
    Y=np.asarray(rgb[::stride,::stride],dtype=np.float32).reshape(-1,3)
    valid=np.ones(len(Y),bool) if mask is None else mask[::stride,::stride].reshape(-1).astype(bool)
    base=valid.copy()
    if valid.sum()<100: raise AcquisitionError('Not enough exposed substrate to normalize this tile.')
    for _ in range(6):
        coeff=np.linalg.lstsq(X[valid],Y[valid],rcond=None)[0]
        pred=X@coeff
        residual=np.linalg.norm((Y-pred)/np.maximum(pred,1),axis=1)
        med=np.median(residual[valid]); mad=max(np.median(np.abs(residual[valid]-med))*1.4826,0.001)
        keep=base & (residual<med+2.5*mad)
        if keep.sum()<100: break
        valid=keep
    # Evaluate the same polynomial using 1-D axes and one reused float32
    # plane: c0+c1*x+c3*x*x + y*(c2+c4*x+c5*y).
    x=x.astype(np.float32)[None,:]
    y=y.astype(np.float32)[:,None]
    surface=np.empty((h,w,3),np.float32)
    work=np.empty((h,w),np.float32)
    for channel in range(3):
        c=coeff[:,channel].astype(np.float32)
        row=(c[3]*x+c[1])*x+c[0]
        np.add(c[5]*y,c[2]+c[4]*x,out=work)
        np.multiply(work,y,out=work)
        np.add(work,row,out=surface[:,:,channel])
    if np.min(surface)<=0: raise AcquisitionError('Invalid fitted background; supply a substrate mask or flat field.')
    return surface

def correct_flat_field(rgb,flat=None,dark=None):
    a=rgb.astype(np.float32)
    d=0 if dark is None else np.asarray(dark,dtype=np.float32)
    if dark is not None and d.shape!=a.shape: raise ValueError('Dark reference shape mismatch.')
    a=np.maximum(a-d,0)
    if flat is not None:
        f=np.asarray(flat,dtype=np.float32)
        if f.shape!=a.shape: raise ValueError('Flat reference shape mismatch.')
        denominator=f-d
        if np.any(denominator<=0): raise ValueError('Flat reference must be greater than dark in every pixel.')
        a=a/denominator*np.median(denominator,axis=(0,1))
    return a
