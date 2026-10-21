"""Material-independent flake proposals plus optional calibrated contrast classification."""
from dataclasses import dataclass,asdict
import json
from pathlib import Path
import cv2, numpy as np
from .imaging import background_surface,correct_flat_field
from .model import AcquisitionError, CameraTransform

@dataclass
class Flake:
    id: str
    centroid_px: list
    polygon_px: list
    area_um2: float
    contrast_rgb: list
    uniformity: float
    solidity: float
    touches_border: bool
    label: str = 'unclassified'
    distance: float | None = None
    stage_xy_um: list | None = None
    stage_polygon_um: list | None = None
    chip_id: str | None = None
    tile_id: str | None = None

class ContrastModel:
    def __init__(self,data):
        self.data=data
        if data.get('schema')!=1 or data.get('contrast_definition')!='(substrate-flake)/substrate':
            raise ValueError('Unsupported material calibration schema or contrast definition.')
        self.classes=data['classes']
        if not self.classes: raise ValueError('Material calibration contains no classes.')
        for c in self.classes:
            if len(c['mean'])!=3 or np.asarray(c['covariance']).shape!=(3,3): raise ValueError('Calibration must use RGB contrast vectors.')
            if np.linalg.eigvalsh(c['covariance']).min()<=0: raise ValueError('Contrast covariance must be positive definite.')

    @classmethod
    def load(cls,path): return cls(json.loads(Path(path).read_text()))

    def classify(self,contrast,context):
        for key,value in self.data['context'].items():
            if context.get(key)!=value:
                return 'calibration_mismatch',None
        distances=[]
        for c in self.classes:
            d=np.asarray(contrast)-c['mean']
            distances.append(float(d@np.linalg.solve(c['covariance'],d)))
        order=np.argsort(distances)
        k=int(order[0]); best=distances[k]
        if best>self.data.get('max_mahalanobis_squared',11.34): return 'out_of_distribution',best
        if len(order)>1 and distances[int(order[1])]-best<self.data.get('minimum_margin',3): return 'ambiguous',best
        return str(self.classes[k]['label']),best

def train_contrast_model(samples,context,material='unspecified'):
    """Samples are independent labeled flakes, not millions of correlated pixels."""
    groups={}
    for s in samples: groups.setdefault(s['label'],[]).append(s['contrast_rgb'])
    classes=[]
    for label,values in groups.items():
        if len(values)<5: raise ValueError(f'{label}: at least five independently labeled flakes are required.')
        a=np.asarray(values,dtype=float)
        if a.ndim!=2 or a.shape[1]!=3 or not np.isfinite(a).all(): raise ValueError('Invalid training contrasts.')
        cov=np.cov(a.T)+np.eye(3)*1e-5
        classes.append({'label':label,'mean':np.mean(a,axis=0).tolist(),'covariance':cov.tolist(),'flakes':len(a)})
    return {'schema':1,'material':material,'contrast_definition':'(substrate-flake)/substrate',
        'context':context,'classes':classes,'max_mahalanobis_squared':11.34,'minimum_margin':3,
        'status':'Empirical optical candidates; reference measurements required for layer labels.'}


def segment_seeded_roi(frame,bbox_px,seed_px,transform=None,padding_px=40):
    """ROI-assisted GrabCut reference mask, explicitly recording the supplied seed.

    This supports tracing a visually verified object for a positive control. It
    is not an unseeded detection result and supplies no material/thickness label.
    """
    x,y,w,h=map(int,bbox_px);height,width=frame.rgb.shape[:2]
    sx,sy=map(int,seed_px)
    if not (0<=x<x+w<=width and 0<=y<y+h<=height and x<=sx<x+w and y<=sy<y+h):
        raise ValueError('Seed must lie inside a valid source-pixel ROI.')
    x0=max(0,x-padding_px);y0=max(0,y-padding_px)
    x1=min(width,x+w+padding_px);y1=min(height,y+h+padding_px)
    rgb=frame.rgb[y0:y1,x0:x1].astype(np.float32)
    scale=max(float(np.percentile(rgb,99.8)),1)
    image=np.uint8(np.clip(rgb/scale,0,1)*255)
    mask=np.full(image.shape[:2],cv2.GC_BGD,np.uint8)
    mask[y-y0:y-y0+h,x-x0:x-x0+w]=cv2.GC_PR_FGD
    cv2.circle(mask,(sx-x0,sy-y0),3,cv2.GC_FGD,-1)
    cv2.grabCut(image,mask,None,np.zeros((1,65),float),np.zeros((1,65),float),8,cv2.GC_INIT_WITH_MASK)
    foreground=np.uint8((mask==cv2.GC_FGD)|(mask==cv2.GC_PR_FGD))
    n,labels,stats,centres=cv2.connectedComponentsWithStats(foreground,8)
    selected=int(labels[sy-y0,sx-x0])
    if not selected:raise AcquisitionError('Seed did not retain a connected foreground object.')
    foreground=np.uint8(labels==selected)
    contours,_=cv2.findContours(foreground,cv2.RETR_EXTERNAL,cv2.CHAIN_APPROX_SIMPLE)
    contour=max(contours,key=cv2.contourArea)
    poly=cv2.approxPolyDP(contour,.7,True)[:,0,:]+[x0,y0]
    centre=(centres[selected]+[x0,y0]).tolist()
    box=cv2.boundingRect(np.int32(poly).reshape(-1,1,2))
    row={'centroid_px':centre,'polygon_px':poly.tolist(),'bbox_px':list(box),
         'area_um2':float(foreground.sum()*np.prod(frame.pixel_size_um)),
         'label':'thick-looking crystal; layer unclassified','disposition':'candidate',
         'priority':'review_required','color_group':'unclassified','color_posterior':None,
         'quality_score':None,'filter_reasons':[],'contrast_rgb':None,'terraces':[],
         'extraction_method':'ROI-assisted GrabCut with one visually selected foreground seed',
         'seed_px':[sx,sy],'roi_px':[x,y,w,h],'validation_status':'visually_selected_positive_control',
         'stage_xy_um':None,'stage_polygon_um':None}
    if transform:
        row['stage_xy_um']=transform.stage_xy(centre,frame).tolist()
        row['stage_polygon_um']=[transform.stage_xy(p,frame).tolist() for p in poly]
    return row,foreground

def segment_flakes(frame,transform=None,min_area_um2=100,max_area_um2=20000,min_contrast=0.018,
                   model=None,context=None,substrate_mask=None,flat=None,dark=None):
    rgb=correct_flat_field(frame.rgb,flat,dark)
    background=background_surface(rgb,substrate_mask)
    contrast=(background-rgb)/np.maximum(background,1)
    smooth=cv2.GaussianBlur(contrast,(3,3),0.7)
    magnitude=np.linalg.norm(smooth,axis=2)
    population=magnitude if substrate_mask is None else magnitude[substrate_mask.astype(bool)]
    med=float(np.median(population)); mad=max(float(np.median(abs(population-med)))*1.4826,0.001)
    threshold=max(min_contrast,med+4*mad)
    mask=np.uint8(magnitude>threshold)*255
    if substrate_mask is not None: mask[~substrate_mask.astype(bool)]=0
    mask=cv2.morphologyEx(mask,cv2.MORPH_OPEN,np.ones((3,3),np.uint8))
    n,labels,stats,centres=cv2.connectedComponentsWithStats(mask,8)
    flakes=[]; pxarea=np.prod(frame.pixel_size_um)
    for label in range(1,n):
        area=float(stats[label,cv2.CC_STAT_AREA]*pxarea)
        if not min_area_um2<=area<=max_area_um2: continue
        local=np.uint8(labels==label)
        contours,_=cv2.findContours(local,cv2.RETR_EXTERNAL,cv2.CHAIN_APPROX_SIMPLE)
        contour=max(contours,key=cv2.contourArea)
        hull=cv2.convexHull(contour)
        solidity=float(cv2.contourArea(contour)/max(cv2.contourArea(hull),1))
        if solidity<0.45: continue
        interior=cv2.erode(local,np.ones((3,3),np.uint8)).astype(bool)
        if interior.sum()<5: interior=local.astype(bool)
        colours=contrast[interior]
        centre=np.median(colours,axis=0)
        uniformity=float(np.median(np.linalg.norm(colours-centre,axis=1)))
        # Reject very textured/dirty regions, but retain their pixels in the mask for review.
        if uniformity>0.06: continue
        x,y,w,h,_=stats[label]
        border=bool(x==0 or y==0 or x+w>=mask.shape[1] or y+h>=mask.shape[0])
        if substrate_mask is not None:
            border=border or bool(np.any(cv2.dilate(local,np.ones((3,3),np.uint8)).astype(bool)&~substrate_mask.astype(bool)))
        poly=cv2.approxPolyDP(contour,0.7,True)[:,0,:].tolist()
        flake=Flake(str(len(flakes)+1),centres[label].tolist(),poly,area,centre.tolist(),uniformity,solidity,border)
        if transform is not None:
            flake.stage_xy_um=transform.stage_xy(flake.centroid_px,frame).tolist()
            flake.stage_polygon_um=[transform.stage_xy(p,frame).tolist() for p in poly]
        if model is not None: flake.label,flake.distance=model.classify(centre,context or {})
        flakes.append(flake)
    return flakes,mask,{'contrast_threshold':threshold,'background_fraction':float(np.mean(mask==0)),
        'warning':'Regions are flake proposals, not confirmed layer assignments. Touching thickness domains may need review.'}
