"""Offline equivalence/latency checks for proposed raster optimizations only."""
import os
for name in ('OMP_NUM_THREADS','OPENBLAS_NUM_THREADS','MKL_NUM_THREADS'):os.environ[name]='2'
import json,time,sys
from pathlib import Path
import cv2,numpy as np
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from flakefinder.imaging import load_frame,preview
from flakefinder.model import CameraTransform,atomic_json
from flakefinder.layout import SubstrateReference,_features,_working_rgb,SUBSTRATE,NON_SUBSTRATE
from flakefinder.chip_discovery import BoundaryOptions,classify_boundary
from flakefinder.scan import Mosaic
cv2.setNumThreads(2)
root=Path('data/whole-chip-standardized-01');out=Path('data/raster-performance-audit-01')
previous=json.loads((out/'report.json').read_text());frame=load_frame(previous['source'])
ref=load_frame(root/'reference/substrate.tiff')
config=json.loads((root/'configuration.json').read_text());plan=json.loads((root/'chip001/coverage-plan.json').read_text())
transform=CameraTransform(**config['camera_transform']);bounds=plan['scan_plan']['bounds_um']
results={}
def bench(name,fn):
    times=[]
    for _ in range(2):
        t=time.perf_counter();value=fn();times.append(time.perf_counter()-t)
    results[name]={'median_s':float(np.median(times)),'seconds':times};return value
white=frame.metadata['white_level']
lut=preview(np.arange(65536,dtype=np.uint16),white)
standard=bench('preview_standard',lambda:preview(frame.rgb,white))
fast=bench('preview_exact_lut',lambda:lut[frame.rgb])
results['preview_exact_equal']=bool(np.array_equal(standard,fast))
del standard,fast
scale=max(2.,max(ref.pixel_size_um)*4,(bounds[1]-bounds[0])/3000,(bounds[3]-bounds[2])/3000)
whole=Mosaic(bounds,scale);small=Mosaic(bounds,scale)
def roi_add():
    f=frame;h,w=f.rgb.shape[:2];matrix=np.asarray(transform.matrix)
    offset=np.array([f.position.x-small.x0,f.position.y-small.y0])-matrix@[(w-1)/2,(h-1)/2]
    affine=np.column_stack([matrix/small.scale,offset/small.scale])
    corners=np.array([[0,0],[w-1,0],[w-1,h-1],[0,h-1]])@affine[:,:2].T+affine[:,2]
    left,top=np.maximum(0,np.floor(corners.min(axis=0)-2).astype(int))
    right,bottom=np.minimum([small.weight.shape[1],small.weight.shape[0]],np.ceil(corners.max(axis=0)+3).astype(int))
    if right<=left or bottom<=top:return
    local=affine.copy();local[:,2]-=[left,top]
    feather=np.maximum(np.outer(np.hanning(h),np.hanning(w)),.02).astype(np.float32)
    shape=(int(right-left),int(bottom-top))
    small.sum[top:bottom,left:right]+=cv2.warpAffine(f.rgb.astype(np.float32)*feather[:,:,None],local,shape,flags=cv2.INTER_LINEAR)
    small.weight[top:bottom,left:right]+=cv2.warpAffine(feather,local,shape,flags=cv2.INTER_LINEAR)
bench('mosaic_full_target',lambda:whole.add(frame,transform))
bench('mosaic_local_target',roi_add)
results['mosaic_sum_max_abs_difference']=float(np.max(np.abs(whole.sum-small.sum)))
results['mosaic_weight_max_abs_difference']=float(np.max(np.abs(whole.weight-small.weight)))
results['mosaic_disagree_values']=int(np.count_nonzero(whole.sum!=small.sum))
model=SubstrateReference.from_frame(ref);ref_rgb=np.median(ref.rgb.reshape(-1,3),axis=0)
options=BoundaryOptions(bounds,cell_um=plan['support_evidence']['support_cell_um'])
def one_resize():
    # The same operations as classify_boundary + classify, with one shared
    # floating-point downsample. This is a benchmark, not a callable backend.
    rgb=_working_rgb(frame,model.max_dimension)
    features,valid=_features(rgb,model.white_level);delta=features-model.centre
    distance=np.sqrt(np.maximum(0.,np.einsum('...i,ij,...j->...',delta,np.linalg.inv(model.covariance),delta)))
    labels=np.zeros(valid.shape,np.uint8)
    labels[valid&(distance<=3.5)]=SUBSTRATE;labels[valid&(distance>=6.)]=NON_SUBSTRATE
    labels[(rgb<ref_rgb*options.dark_holder_ratio).all(axis=2)]=NON_SUBSTRATE
    return labels
normal=bench('boundary_current',lambda:classify_boundary(frame,model,ref_rgb,options))
shared=bench('boundary_shared_downsample',one_resize)
results['boundary_exact_equal']=bool(np.array_equal(normal,shared))
results['note']='One saved image at two OpenCV/BLAS threads. No production files changed and no hardware calls.'
atomic_json(out/'optimization-equivalence.json',results);print(json.dumps(results,indent=2))
