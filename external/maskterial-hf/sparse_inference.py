"""Memory-bounded MaskTerial inference with the original instance score.

Class probability upper-bounds class_probability * mean(foreground_probability).
Queries below the requested threshold therefore cannot produce a retained mask.
Remaining query logits are upsampled one at a time, avoiding 100 full-size logits,
probabilities and intermediate score products in memory at once.
"""
import ctypes,os,time
import numpy as np
import torch
from torch import nn


def process_memory_bytes():
    if os.name!='nt':return None
    class Counters(ctypes.Structure):
        _fields_=[('cb',ctypes.c_ulong),('PageFaultCount',ctypes.c_ulong)]+[(name,ctypes.c_size_t) for name in
                    ['PeakWorkingSetSize','WorkingSetSize','QuotaPeakPagedPoolUsage','QuotaPagedPoolUsage',
                     'QuotaPeakNonPagedPoolUsage','QuotaNonPagedPoolUsage','PagefileUsage','PeakPagefileUsage','PrivateUsage']]
    counts=Counters();counts.cb=ctypes.sizeof(counts)
    current=ctypes.windll.kernel32.GetCurrentProcess;current.restype=ctypes.c_void_p
    query=ctypes.windll.psapi.GetProcessMemoryInfo
    query.argtypes=[ctypes.c_void_p,ctypes.c_void_p,ctypes.c_ulong];query.restype=ctypes.c_int
    if not query(current(),ctypes.byref(counts),counts.cb):raise ctypes.WinError()
    return {name:getattr(counts,name) for name in ['PeakWorkingSetSize','WorkingSetSize','PeakPagefileUsage','PrivateUsage']}


def postprocess(mask_logits, class_logits, padded_hw, image_hw, threshold):
    height,width=image_hw
    classes=class_logits.softmax(-1)[0,:,0]
    candidates=[];evaluated=[]
    for query in torch.nonzero(classes>=threshold).flatten().tolist():
        logit=nn.functional.interpolate(mask_logits[:,query:query+1],size=padded_hw,
                                       mode='bilinear',align_corners=False)[0,0,:height,:width]
        mask=logit>0
        score=classes[query]*(logit.sigmoid()*mask).sum()/(mask.sum()+1e-6)
        evaluated.append({'query':query,'score':float(score)})
        if score>=threshold:
            candidates.append({'query':query,'score':float(score),'logits':logit.numpy().copy()})
    candidates.sort(key=lambda row:-row['score'])
    return candidates,{'class_upper_bounds':classes.numpy().tolist(),'evaluated':evaluated,
                       'rejected_by_class_bound':int((classes<threshold).sum())}


def predict_candidates(model,bgr,threshold):
    height,width=bgr.shape[:2]
    data=torch.from_numpy(np.ascontiguousarray(bgr.transpose(2,0,1))).float()
    data=(data-torch.tensor([107.,148.,85.])[:,None,None])/torch.tensor([33.,34.,40.])[:,None,None]
    data=nn.functional.pad(data,(0,(-width)%32,0,(-height)%32))
    start=time.perf_counter()
    with torch.inference_mode():
        out=model(pixel_values=data[None])
        candidates,diagnostics=postprocess(out.masks_queries_logits,out.class_queries_logits,
                                          data.shape[-2:],(height,width),threshold)
    return candidates,diagnostics,time.perf_counter()-start
