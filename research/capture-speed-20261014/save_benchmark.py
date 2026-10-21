"""Offline same-frame local/Drive persistence comparison. Never imports hardware."""
import os
os.environ.update(OMP_NUM_THREADS='1',OPENBLAS_NUM_THREADS='1',MKL_NUM_THREADS='1')
from pathlib import Path
import ctypes
import hashlib
import json
import sys
import tempfile
import time
import uuid

ROOT=Path(__file__).resolve().parents[2]
RELEASE=ROOT/'research/software-next-20261009'
sys.path.insert(0,str(RELEASE))
import cv2
import numpy as np
from PIL import Image
from flakefinder import imaging
from flakefinder.model import atomic_json
from flakefinder.storage import project_root,managed_output_path


def digest(path):
    h=hashlib.sha256()
    with Path(path).open('rb') as stream:
        for block in iter(lambda:stream.read(1024*1024),b''):h.update(block)
    return h.hexdigest()


def run():
    cv2.setNumThreads(1)
    priority={'threads':1}
    if os.name=='nt':
        from ctypes import wintypes as w
        k=ctypes.WinDLL('kernel32',use_last_error=True)
        k.GetCurrentProcess.restype=w.HANDLE
        handle=k.GetCurrentProcess()
        k.SetPriorityClass.argtypes=[w.HANDLE,w.DWORD]
        priority['below_normal']=bool(k.SetPriorityClass(handle,0x4000))
        mask=ctypes.c_size_t();system=ctypes.c_size_t()
        k.GetProcessAffinityMask.argtypes=[w.HANDLE,ctypes.POINTER(ctypes.c_size_t),ctypes.POINTER(ctypes.c_size_t)]
        if k.GetProcessAffinityMask(handle,ctypes.byref(mask),ctypes.byref(system)):
            chosen=4 if mask.value&4 else mask.value&-mask.value
            k.SetProcessAffinityMask.argtypes=[w.HANDLE,ctypes.c_size_t]
            priority.update(cpu_mask=chosen,affinity_set=bool(k.SetProcessAffinityMask(handle,chosen)))
    project=project_root()
    source=project/'data/capture-benchmark-20261014-211039-257869e5/frames/frame_004.tiff'
    identifier='save-benchmark-'+time.strftime('%Y%m%d-%H%M%S')+'-'+uuid.uuid4().hex[:8]
    shared=project/'data'/identifier
    local=Path(tempfile.gettempdir())/identifier
    for folder in (shared,local):
        assert managed_output_path(folder)==folder.resolve()
        folder.mkdir()
    frame=imaging.load_frame(source)
    raw_hash=hashlib.sha256(frame.rgb.tobytes()).hexdigest()
    plan={'schema':1,'id':identifier,'no_hardware':True,'source':str(source),
          'source_sha256':digest(source),'metadata_sha256':digest(source.with_suffix('.json')),
          'pixel_sha256':raw_hash,'shape':list(frame.rgb.shape),'dtype':str(frame.rgb.dtype),
          'directories':{'C':str(local),'G':str(shared)},'priority':priority,
          'code_sha256':{str(p):digest(p) for p in (Path(__file__),RELEASE/'flakefinder/imaging.py',RELEASE/'flakefinder/model.py')},
          'load_confound':'Two-worker segmentation remains active. No PAUSE requested. CPU affinity prefers CPU2, independent of segmentation CPU0/1.',
          'durability_limit':'fsync is the OS/provider contract, not proof of completed remote Drive upload.',
          'policy':'Same in-memory source frame/metadata. Production save_frame path unchanged. No cleanup, no science overwrite, no acquisition.'}
    atomic_json(shared/'plan.json',plan)
    original_write=cv2.imwrite;original_preview=Image.Image.save;original_json=imaging.atomic_json
    phases={};compression=None
    def raw_write(path,array,*args,**kwargs):
        start=time.perf_counter()
        try:
            if compression is not None and str(path).endswith('.tiff'):
                return original_write(path,array,[cv2.IMWRITE_TIFF_COMPRESSION,compression])
            return original_write(path,array,*args,**kwargs)
        finally:phases['raw_encode_write_s']=time.perf_counter()-start
    def preview_write(image,*args,**kwargs):
        start=time.perf_counter()
        try:return original_preview(image,*args,**kwargs)
        finally:phases['preview_jpeg_write_s']=time.perf_counter()-start
    def json_write(*args,**kwargs):
        start=time.perf_counter()
        try:return original_json(*args,**kwargs)
        finally:phases['metadata_json_s']=time.perf_counter()-start
    cv2.imwrite=raw_write;Image.Image.save=preview_write;imaging.atomic_json=json_write
    rows=[]
    try:
        trials=[(pair,drive,None) for pair in range(3) for drive in ('C','G')]
        # One supplementary pair changes only TIFF compression in this process.
        # It does not edit the production writer or any camera/acquisition option.
        trials.extend([(3,'C',1),(3,'G',1)])
        for index,(pair,drive,compression) in enumerate(trials):
            if (shared/'STOP').exists() or (local/'STOP').exists():raise RuntimeError('Offline benchmark STOP')
            phases.clear();folder=local if drive=='C' else shared;name=f'trial_{index:02d}'
            start=time.perf_counter();imaging.save_frame(folder,name,frame)
            phases['save_frame_s']=time.perf_counter()-start
            raw=folder/(name+'.tiff');tick=time.perf_counter()
            for p in (raw,raw.with_suffix('.json')):
                with p.open('rb+') as stream:os.fsync(stream.fileno())
            phases['fsync_s']=time.perf_counter()-tick;tick=time.perf_counter()
            saved=imaging.load_frame(raw)
            phases['readback_decode_s']=time.perf_counter()-tick;tick=time.perf_counter()
            equal=np.array_equal(saved.rgb,frame.rgb)
            if not equal or saved.metadata!=frame.metadata or saved.position!=frame.position:raise RuntimeError('Roundtrip differs')
            actual_hash=hashlib.sha256(saved.rgb.tobytes()).hexdigest()
            if actual_hash!=raw_hash:raise RuntimeError('Pixel hash differs')
            phases['pixel_compare_hash_s']=time.perf_counter()-tick;tick=time.perf_counter()
            file_sha=digest(raw);phases['file_hash_s']=time.perf_counter()-tick
            phases['total_verified_s']=time.perf_counter()-start
            row={'trial':index,'pair':pair,'drive':drive,'compression':'production_default' if compression is None else 'uncompressed_lossless',
                 'timings':dict(phases),'raw_bytes':raw.stat().st_size,'raw_sha256':file_sha,'pixel_equal':equal,'pixel_sha256':actual_hash}
            rows.append(row);atomic_json(shared/'results.json',{'state':'running','plan':plan,'rows':rows})
            print(json.dumps(row),flush=True)
            del saved
    finally:
        cv2.imwrite=original_write;Image.Image.save=original_preview;imaging.atomic_json=original_json
    assert digest(source)==plan['source_sha256'] and digest(source.with_suffix('.json'))==plan['metadata_sha256']
    result={'state':'complete','plan':plan,'rows':rows,'source_unchanged':True}
    atomic_json(shared/'results.json',result)
    atomic_json(Path(__file__).parent/'save-benchmark-latest.json',{'output':str(shared),'local_scratch':str(local),'results_sha256':digest(shared/'results.json')})
    print('COMPLETE '+str(shared),flush=True)


if __name__=='__main__':run()
