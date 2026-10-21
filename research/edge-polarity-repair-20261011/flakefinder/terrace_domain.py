"""Saved chip support and independently supported raw-RGB normalization."""
from dataclasses import dataclass
from pathlib import Path
import hashlib
import json

import cv2
import numpy as np
from scipy.ndimage import distance_transform_edt

from .model import AcquisitionError


def digest(path):
    value=hashlib.sha256()
    with Path(path).open('rb') as stream:
        for block in iter(lambda:stream.read(1024*1024),b''):value.update(block)
    return value.hexdigest()


@dataclass
class ChipDomain:
    folder: Path
    manifest: dict
    polygon_um: np.ndarray
    geometry_status: str
    qc: dict
    preview: np.ndarray | None = None

    def masks(self,item,frame,edge_margin_um=12.):
        h,w=frame.rgb.shape[:2]
        polygon=(self.polygon_um-[frame.position.x,frame.position.y])@np.linalg.inv(item['transform'].matrix).T+[(w-1)/2,(h-1)/2]
        pad=int(np.ceil(edge_margin_um/min(frame.pixel_size_um)))+2
        extended=np.zeros((h+2*pad,w+2*pad),np.uint8)
        cv2.fillPoly(extended,[np.int32(np.round(polygon))+pad],1)
        inside=extended[pad:pad+h,pad:pad+w]
        # Project into a larger canvas so a camera border inside the chip is not
        # mistaken for the specimen edge; only physical chip edges get a margin.
        edge_distance=distance_transform_edt(np.pad(extended,1),sampling=(frame.pixel_size_um[1],frame.pixel_size_um[0]))[pad+1:pad+1+h,pad+1:pad+1+w]
        interior=(inside>0)&(edge_distance>edge_margin_um)
        if interior.sum()<1000:
            raise DomainExclusion('off_chip_or_edge_margin','No usable interior remains inside the saved chip boundary and margin.')
        path=self.folder/'boundary-labels'/(item['tile_id']+'.npz')
        expected=self.manifest['completed'][item['tile_id']].get('hashes',{}).get('boundary-labels/'+path.name)
        if not path.is_file() or not expected or digest(path)!=expected:
            raise AcquisitionError('Missing or changed independently recorded substrate labels.')
        with np.load(path,allow_pickle=False) as saved:labels=np.asarray(saved['labels'])
        support=cv2.resize(np.uint8(labels==1),(w,h),interpolation=cv2.INTER_NEAREST)>0
        support&=interior
        support&=distance_transform_edt(np.pad(support,1),sampling=(frame.pixel_size_um[1],frame.pixel_size_um[0]))[1:-1,1:-1]>1.
        if support.sum()<1000:
            raise AcquisitionError('No sufficient independently recorded bare-substrate support; no color measurement or zero-contrast pass.')
        return interior,support,{'geometry_status':self.geometry_status,'domain_fraction':float(interior.mean()),
            'substrate_support_fraction':float(support.mean()),'edge_margin_um':edge_margin_um,
            'substrate_label_sha256':expected,'domain_is_not_substrate_color_mask':True,
            'qualification':'Recorded color labels supply background samples only. Differently colored objects remain eligible within geometry.'}


class DomainExclusion(AcquisitionError):
    def __init__(self,kind,reason):
        self.kind=kind
        super().__init__(reason)


def build_chip_domain(folder,options=None):
    """Low-resolution saved-preview atlas for QC, never for radiometry."""
    folder=Path(folder).resolve();manifest=json.loads((folder/'manifest.json').read_text(encoding='utf-8'))
    measured=manifest.get('boundary_validation',{}).get('acquired_raster_evidence',{})
    verified=measured.get('status')=='enclosed' and measured.get('outline_um')
    polygon=np.asarray(measured['outline_um'] if verified else manifest['plan']['polygon_um'],float)
    if polygon.ndim!=2 or polygon.shape[1]!=2 or len(polygon)<3 or not np.isfinite(polygon).all():
        raise AcquisitionError('No finite saved chip geometry.')
    scale=float(getattr(options,'atlas_pixel_um',8.));low=polygon.min(axis=0)-500;high=polygon.max(axis=0)+500
    size=np.ceil((high-low)/scale).astype(int)+1
    if int(np.prod(size))>8_000_000:raise AcquisitionError('Chip atlas exceeds the bounded display size.')
    canvas=np.zeros((size[1],size[0],3),np.uint8);covered=np.zeros(canvas.shape[:2],np.uint8)
    support=np.zeros_like(covered);matrix=np.asarray(manifest['transform']['matrix'])
    raw_h,raw_w=manifest.get('camera_shape',[2056,2464])[:2];fields=[]
    for tile,record in sorted(manifest.get('completed',{}).items()):
        path=folder/'tiles'/(tile+'.jpg');preview=cv2.imread(str(path))
        if preview is None:continue
        h,w=preview.shape[:2];position=record['position']
        A=matrix@np.diag([raw_w/w,raw_h/h])/scale
        shift=(np.array([position['x'],position['y']])-matrix@[(raw_w-1)/2,(raw_h-1)/2]-low)/scale
        transform=np.column_stack([A,shift])
        corners=np.array([[0,0],[w,0],[w,h],[0,h]])@A.T+shift
        x0,y0=np.maximum(np.floor(corners.min(axis=0)).astype(int)-1,0)
        x1,y1=np.minimum(np.ceil(corners.max(axis=0)).astype(int)+1,size)
        if x1<=x0 or y1<=y0:continue
        local=transform.copy();local[:,2]-=[x0,y0]
        shape=(int(x1-x0),int(y1-y0))
        mask=cv2.warpAffine(np.ones((h,w),np.uint8),local,shape,flags=cv2.INTER_NEAREST)>0
        warped=cv2.warpAffine(preview,local,shape,flags=cv2.INTER_AREA)
        canvas[y0:y1,x0:x1][mask]=warped[mask];covered[y0:y1,x0:x1][mask]=1
        label_path=folder/'boundary-labels'/(tile+'.npz');fraction=None
        if label_path.is_file():
            expected=record.get('hashes',{}).get('boundary-labels/'+label_path.name)
            if expected and digest(label_path)==expected:
                with np.load(label_path,allow_pickle=False) as saved:labels=saved['labels']
                binary=cv2.resize(np.uint8(labels==1),(w,h),interpolation=cv2.INTER_NEAREST)
                support[y0:y1,x0:x1]|=cv2.warpAffine(binary,local,shape,flags=cv2.INTER_NEAREST)
                fraction=float(np.mean(labels==1))
        fields.append({'tile_id':tile,'position':position,'substrate_fraction':fraction})
    outline=np.int32(np.round((polygon-low)/scale));cv2.polylines(canvas,[outline],True,(70,230,100),1)
    qc={'method':'saved_preview_stage_atlas_with_independent_substrate_labels','geometry_status':'observed_enclosed' if verified else 'provisional_plan_unclosed',
        'outline_um':polygon.tolist(),'atlas_origin_um':low.tolist(),'atlas_pixel_um':scale,
        'covered_pixels':int(covered.sum()),'recorded_substrate_pixels':int(support.sum()),'fields':fields,
        'offchip_veto':'Outside observed enclosure' if verified else 'Outside planned support; enclosure remains unverified',
        'qualification':'Preview is QC only; no photometry from JPEG or overlap blending. Substrate support is not an object-domain veto. Saved stage registration is not pixel-perfect.'}
    domain=ChipDomain(folder,manifest,polygon,qc['geometry_status'],qc,canvas)
    from .terrace_substrate import build_substrate_reference
    domain.qc['substrate_consistency_reference']=build_substrate_reference(domain)
    return domain


def normalize_supported(frame,valid,support):
    """Fit only independently supported raw substrate; validate spatially."""
    h,w=valid.shape;step=max(1,int(np.sqrt(h*w/30000)))
    xx,yy=np.meshgrid(np.linspace(-1,1,w)[::step],np.linspace(-1,1,h)[::step])
    X=np.stack([np.ones_like(xx),xx,yy,xx*xx,xx*yy,yy*yy],-1).reshape(-1,6)
    Y=np.float64(frame.rgb[::step,::step].reshape(-1,3));base=support[::step,::step].ravel()
    if base.sum()<100:raise AcquisitionError('Insufficient independent substrate samples.')
    def fit(design,mask):
        keep=mask.copy()
        for _ in range(6):
            if keep.sum()<100:raise AcquisitionError('Insufficient retained substrate samples.')
            coeff=np.linalg.lstsq(design[keep],Y[keep],rcond=None)[0]
            prediction=design@coeff
            error=np.linalg.norm((Y-prediction)/np.maximum(prediction,1),axis=1)
            middle=np.median(error[keep]);mad=max(1.4826*np.median(abs(error[keep]-middle)),.001)
            update=mask&(error<middle+2.5*mad)
            if update.sum()<100:break
            keep=update
        return coeff
    folds=((np.minimum(((xx+1)*2).astype(int),3)+np.minimum(((yy+1)*2).astype(int),3))%4).ravel()
    choices=[]
    for degree,columns in ((0,1),(1,3),(2,6)):
        scores=[]
        for fold in range(4):
            train=base&(folds!=fold);test=base&(folds==fold)
            if train.sum()<100 or test.sum()<25:continue
            coefficient=fit(X[:,:columns],train);pred=X[test,:columns]@coefficient
            if (pred<=0).any():continue
            scores.append(float(np.median(np.linalg.norm((Y[test]-pred)/pred,axis=1))))
        if len(scores)>=3:choices.append({'degree':degree,'columns':columns,'mean':float(np.mean(scores)),
            'se':float(np.std(scores,ddof=1)/np.sqrt(len(scores))),'fold_scores':scores})
    if not choices:raise AcquisitionError('Substrate support cannot sustain spatial held-out fit validation.')
    best=min(choices,key=lambda c:c['mean']);chosen=next(c for c in choices if c['mean']<=best['mean']+best['se'])
    coeff=np.zeros((6,3),np.float32);coeff[:chosen['columns']]=fit(X[:,:chosen['columns']],base)
    x=np.linspace(-1,1,w,dtype=np.float32)[None,:];y=np.linspace(-1,1,h,dtype=np.float32)[:,None]
    background=np.empty(frame.rgb.shape,np.float32)
    for channel,c in enumerate(coeff.T):background[:,:,channel]=c[0]+c[1]*x+c[2]*y+c[3]*x*x+c[4]*x*y+c[5]*y*y
    if (background[valid]<=0).any():raise AcquisitionError('Nonpositive supported-domain background; fit rejected.')
    contrast=np.zeros(frame.rgb.shape,np.float32)
    contrast[valid]=(background[valid]-frame.rgb[valid])/background[valid]
    # Invalid values are represented by the explicit mask, not photometric zeros.
    return contrast,{'fit_models':choices,'chosen_degree':chosen['degree'],'coefficients':coeff.tolist(),
        'background_sample_count':int(base.sum()),'outside_domain':'Not measured; masked from every operation',
        'fit_support_independent_of_feature_color':True}
