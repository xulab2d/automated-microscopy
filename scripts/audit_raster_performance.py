"""Offline microbenchmark of saved frames; never instantiates microscope APIs."""
import os
for name in ('OMP_NUM_THREADS','OPENBLAS_NUM_THREADS','MKL_NUM_THREADS','NUMEXPR_NUM_THREADS'):
    os.environ[name]='2'
import json,time,sys
from collections import defaultdict
from pathlib import Path
import cv2,numpy as np
from PIL import Image
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from flakefinder.imaging import load_frame,save_frame,preview
from flakefinder.model import CameraTransform,atomic_json
from flakefinder.layout import SubstrateReference
from flakefinder.chip_discovery import BoundaryOptions,EvidenceMap,classify_boundary,file_hash
from flakefinder.scan import Mosaic

cv2.setNumThreads(2)
root=Path('data/whole-chip-standardized-01')
out=Path('data/raster-performance-audit-01');out.mkdir(exist_ok=True)
config=json.loads((root/'configuration.json').read_text())
plan=json.loads((root/'chip001/coverage-plan.json').read_text())
reference=load_frame(root/'reference/substrate.tiff')
candidates=list((root/'chip001/tiles').glob('*.tiff'))
if not candidates:candidates=list((root/'discovery/chip-001').rglob('*.tiff'))
source=candidates[-1];frame=load_frame(source)
transform=CameraTransform(**config['camera_transform'])
results={}
def bench(name,fn,n=2):
    ts=[];v=None
    for _ in range(n):
        t=time.perf_counter();v=fn();ts.append(time.perf_counter()-t)
    results[name]={'seconds':ts,'median_s':float(np.median(ts))};return v
bench('save_frame_complete',lambda:save_frame(out,'saved',frame))
bgr=np.ascontiguousarray(frame.rgb[:,:,::-1])
bench('bgr_contiguous_copy',lambda:np.ascontiguousarray(frame.rgb[:,:,::-1]))
bench('raw_tiff_default_LZW',lambda:cv2.imwrite(str(out/'default.tiff'),bgr))
bench('raw_tiff_uncompressed',lambda:cv2.imwrite(str(out/'uncompressed.tiff'),bgr,[cv2.IMWRITE_TIFF_COMPRESSION,1]))
bench('raw_tiff_deflate',lambda:cv2.imwrite(str(out/'deflate.tiff'),bgr,[cv2.IMWRITE_TIFF_COMPRESSION,8]))
p=bench('preview_full',lambda:preview(frame.rgb,frame.metadata.get('white_level')))
bench('jpeg_full',lambda:Image.fromarray(p).save(out/'preview.jpg',quality=90))
bench('raw_hash',lambda:file_hash(out/'saved.tiff'))
model=bench('substrate_reference_once',lambda:SubstrateReference.from_frame(reference),1)
bounds=plan['scan_plan']['bounds_um'];cell=plan['support_evidence']['support_cell_um']
evidence=EvidenceMap(bounds,cell);opts=BoundaryOptions(bounds,cell_um=cell)
rgb=np.median(reference.rgb.reshape(-1,3),axis=0)
labels=bench('classify_boundary',lambda:classify_boundary(frame,model,rgb,opts))
bench('labels_compress',lambda:np.savez_compressed(out/'labels.npz',labels=labels))
bench('evidence_add',lambda:evidence.add(labels,[frame.position.x,frame.position.y],frame.rgb.shape,transform))
scale=max(2.,max(reference.pixel_size_um)*4,(bounds[1]-bounds[0])/3000,(bounds[3]-bounds[2])/3000)
mosaic=Mosaic(bounds,scale)
bench('mosaic_add',lambda:mosaic.add(frame,transform))
bench('mosaic_save_every10',lambda:mosaic.save(out),1)
timings=defaultdict(list);metadata=[]
for path in root.rglob('*.tiff'):
    try:
        d=json.loads(path.with_suffix('.json').read_text());t=d.get('metadata',{}).get('acquisition_timing_s',{})
        if t:
            for k,v in t.items():timings[k].append(v)
            metadata.append({'file':str(path),'acquired_at':d['acquired_at'],'timings':t})
    except (OSError,ValueError,KeyError):pass
start=json.loads((root/'batch.json').read_text())['started_at'];events=[]
with Path('data/motion.jsonl').open() as handle:
    for line in handle:
        try:
            e=json.loads(line)
            if e['time']>=start:events.append(e)
        except (ValueError,KeyError):pass
moves=[];request=None
for e in events:
    if e['event']=='request':request=e
    elif e['event']=='settled' and request:
        moves.append({'seconds':e['time']-request['time'],'changed_axes':[a for a in 'xyz' if abs(request['position'][a]-request['target'][a])>(.235 if a=='z' else .3)]});request=None
report={'source':str(source),'source_shape':frame.rgb.shape,'opencv_threads':cv2.getNumThreads(),
    'microbenchmarks':results,'file_sizes':{p.name:p.stat().st_size for p in out.glob('*.tiff')},
    'acquisition_timings':{k:{'n':len(v),'median_s':float(np.median(v)),'p90_s':float(np.percentile(v,90))} for k,v in timings.items()},
    'motion_requests_since_campaign_start':moves,'acquisition_metadata':metadata,
    'qualification':'Offline saved-frame timings with two OpenCV/BLAS threads while acquisition runs; no hardware calls, no production changes.'}
atomic_json(out/'report.json',report)
print(json.dumps({k:v for k,v in report.items() if k not in ('acquisition_metadata','motion_requests_since_campaign_start')},indent=2))
print('Motion summary',len(moves),float(np.median([v['seconds'] for v in moves])) if moves else None)
