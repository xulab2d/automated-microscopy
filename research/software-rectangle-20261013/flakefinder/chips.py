"""Overview chip detection and stable grid addresses; a missing chip is preserved."""
from dataclasses import dataclass,asdict
import cv2, numpy as np
from scipy.optimize import linear_sum_assignment
from .model import AcquisitionError

@dataclass
class Chip:
    id: str
    polygon_px: list
    centre_px: list
    area_px: float
    row: int = -1
    column: int = -1
    stage_polygon_um: list | None = None

def detect_chips(rgb,min_area_px=500,polarity='bright',expected_count=None,grid_shape=None):
    if polarity not in ('bright','dark'):raise ValueError('Chip polarity must be bright or dark.')
    if expected_count is not None and (not isinstance(expected_count,int) or expected_count<1):raise ValueError('Expected chip count must be positive or null.')
    a=np.asarray(rgb)
    gray=np.mean(a.astype(np.float32),axis=2)
    lo,hi=np.percentile(gray,[1,99])
    if hi-lo<1: raise AcquisitionError('Overview is blank; cannot identify chips.')
    g=np.uint8(np.clip((gray-lo)/(hi-lo)*255,0,255))
    g=cv2.GaussianBlur(g,(5,5),0)
    mode=cv2.THRESH_BINARY if polarity=='bright' else cv2.THRESH_BINARY_INV
    _,mask=cv2.threshold(g,0,255,mode|cv2.THRESH_OTSU)
    mask=cv2.morphologyEx(mask,cv2.MORPH_CLOSE,np.ones((5,5),np.uint8))
    contours,_=cv2.findContours(mask,cv2.RETR_EXTERNAL,cv2.CHAIN_APPROX_SIMPLE)
    chips=[]
    for contour in contours:
        area=cv2.contourArea(contour)
        if area<min_area_px: continue
        rect=cv2.minAreaRect(contour)
        w,h=rect[1]
        if min(w,h)<=0 or area/(w*h)<0.6 or max(w,h)/min(w,h)>3: continue
        poly=cv2.approxPolyDP(contour,0.01*cv2.arcLength(contour,True),True)[:,0,:]
        if np.any(poly[:,0]<=1) or np.any(poly[:,1]<=1) or np.any(poly[:,0]>=a.shape[1]-2) or np.any(poly[:,1]>=a.shape[0]-2):
            raise AcquisitionError('A chip touches the overview boundary. Expand the survey before assigning chip IDs.')
        m=cv2.moments(contour)
        chips.append(Chip('',poly.tolist(),[m['m10']/m['m00'],m['m01']/m['m00']],float(area)))
    if not chips:raise AcquisitionError('No complete chips detected in the survey.')
    if expected_count is not None and len(chips)!=expected_count:
        raise AcquisitionError(f'Detected {len(chips)} chips; expected {expected_count}. Inspect the overview mask or adjust polarity/area.')
    if grid_shape is not None:return assign_grid(chips,grid_shape),mask
    # Unconstrained layout: every complete chip is usable without a known grid,
    # count, material, or a person clicking chip centres.
    chips.sort(key=lambda c:(c.centre_px[1],c.centre_px[0]))
    for i,chip in enumerate(chips):chip.id=f'chip-{i+1:03d}'
    return chips,mask

def assign_grid(chips,shape=(4,4)):
    if len(shape)!=2 or any(not isinstance(v,int) or v<1 for v in shape):raise ValueError('Grid shape requires positive integer rows and columns.')
    rows,cols=shape
    if len(chips)>rows*cols or len(chips)<max(rows,cols): raise AcquisitionError('Chip count is incompatible with the requested grid.')
    xy=np.array([c.centre_px for c in chips])
    # Use neighbouring chip centres: individual chips can be rotated independently.
    delta=xy[:,None,:]-xy[None,:,:];distance=np.linalg.norm(delta,axis=2)
    np.fill_diagonal(distance,np.inf)
    pitch_guess=float(np.median(distance.min(axis=1))) if len(chips)>1 else 1
    near=(distance<pitch_guess*1.3)&(distance>0)
    angles=np.arctan2(delta[:,:,1][near],delta[:,:,0][near])
    theta=float(np.angle(np.mean(np.exp(4j*angles)))/4) if len(angles) else 0
    rot=np.array([[np.cos(theta),np.sin(theta)],[-np.sin(theta),np.cos(theta)]])
    points=(xy-xy.mean(axis=0))@rot.T
    def centres(values,n):
        c=np.linspace(values.min(),values.max(),n)
        for _ in range(30):
            labels=np.argmin(abs(values[:,None]-c),axis=1)
            if any(np.sum(labels==j)==0 for j in range(n)):
                raise AcquisitionError('Cannot resolve all rows/columns; grid fit is ambiguous.')
            new=np.array([np.mean(values[labels==j]) for j in range(n)])
            if np.allclose(c,new): break
            c=new
        return np.sort(c)
    xc,yc=centres(points[:,0],cols),centres(points[:,1],rows)
    pitches=[float(np.median(np.diff(c))) for c in (xc,yc) if len(c)>1]
    pitch=min(pitches) if pitches else 1
    slots=np.array([(x,y) for y in yc for x in xc])
    distance=np.linalg.norm(points[:,None,:]-slots[None,:,:],axis=2)
    indices,assigned=linear_sum_assignment(distance)
    if np.max(distance[indices,assigned])>pitch*0.2:
        raise AcquisitionError('Detected chip centres do not fit the requested grid reliably.')
    for i,s in zip(indices,assigned):
        chip=chips[i]; chip.row=int(s//cols); chip.column=int(s%cols)
        chip.id=f'{chr(65+chip.row)}{chip.column+1}'
    return sorted(chips,key=lambda c:(c.row,c.column))
