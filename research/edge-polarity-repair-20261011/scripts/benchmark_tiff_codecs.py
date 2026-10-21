"""Read one saved raw frame; measure in-memory lossless codecs. No hardware."""
import argparse
import json
import time
from pathlib import Path
import cv2
import numpy as np

p=argparse.ArgumentParser(description=__doc__)
p.add_argument('image'); p.add_argument('--output',required=True)
args=p.parse_args()
rgb=cv2.imread(args.image,cv2.IMREAD_UNCHANGED)
if rgb is None: raise ValueError('Could not read saved raw TIFF')
records=[]
for name in ('LZW','DEFLATE','ZSTD','NONE'):
    value=getattr(cv2,'IMWRITE_TIFF_COMPRESSION_'+name,None)
    if value is None: continue
    record={'codec':name,'flag':value,'samples':[]}
    try:
        for _ in range(3):
            start=time.perf_counter()
            ok,payload=cv2.imencode('.tiff',rgb,[cv2.IMWRITE_TIFF_COMPRESSION,value])
            encoded=time.perf_counter()-start
            if not ok: raise ValueError('TIFF encoding failed')
            start=time.perf_counter(); restored=cv2.imdecode(payload,cv2.IMREAD_UNCHANGED)
            decoded=time.perf_counter()-start
            if restored.dtype!=rgb.dtype or restored.shape!=rgb.shape or not np.array_equal(rgb,restored):
                raise ValueError('Pixel preservation failed')
            record['samples'].append({'encode_s':encoded,'decode_s':decoded,'bytes':len(payload)})
        record['lossless_verified']=True
    except (ValueError,cv2.error) as error:
        record['error']=str(error)
    records.append(record)
result={'image':str(Path(args.image).resolve()),'shape':list(rgb.shape),'dtype':str(rgb.dtype),
        'opencv':cv2.__version__,'records':records,'hardware_used':False,
        'qualification':'In-memory saved-frame codec benchmark; does not include disk, Drive upload or camera time.'}
Path(args.output).write_text(json.dumps(result,indent=2))
print(json.dumps(result,indent=2))
