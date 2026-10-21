"""Compare sparse scoring with dense equations and a saved raw-image control."""
from pathlib import Path
import ctypes,json
import cv2,numpy as np,torch
from probe_cpu import load_model
from sparse_inference import postprocess,predict_candidates,process_memory_bytes

ROOT=Path(__file__).resolve().parent

torch.set_num_threads(3);torch.manual_seed(31)
raw=torch.randn(1,100,19,23);classes=torch.randn(1,100,2)
dense=torch.nn.functional.interpolate(raw,size=(96,128),mode='bilinear',align_corners=False)[0,:,:91,:119]
binary=dense>0
expected=classes.softmax(-1)[0,:,0]*(dense.sigmoid()*binary).flatten(1).sum(1)/(binary.flatten(1).sum(1)+1e-6)
actual,diagnostics=postprocess(raw,classes,(96,128),(91,119),.3)
assert {r['query'] for r in actual}==set(torch.nonzero(expected>=.3).flatten().tolist())
for row in actual:
    np.testing.assert_array_equal(row['logits']>0,binary[row['query']].numpy())
    np.testing.assert_allclose(row['score'],float(expected[row['query']]),rtol=2e-6,atol=1e-7)
del raw,classes,dense,binary,expected,actual
name='0023919e-4780-4255-9f00-8cc84fe1f21d'
bgr=cv2.imread(str(ROOT.parent/'benchmarks/test_images'/(name+'.jpg')))
scale=1536/max(bgr.shape[:2]);small=cv2.resize(bgr,None,fx=scale,fy=scale,interpolation=cv2.INTER_AREA)
model=load_model();actual,diagnostics,seconds=predict_candidates(model,small,.5)
actual=[r for r in actual if np.count_nonzero(r['logits']>0)/scale**2>=667]
old=np.load(ROOT/'benchmark'/(name+'.npz'))
assert len(actual)==len(old['scores'])
for j,row in enumerate(actual):
    np.testing.assert_array_equal(row['logits']>0,old['masks'][j])
    np.testing.assert_allclose(row['score'],old['scores'][j],rtol=2e-5,atol=2e-6)
result={'synthetic_dense_equations':'passed; identical retained mask pixels',
        'public_control':name,'retained_queries':len(actual),'public_control_masks':'exact match to saved dense output',
        'max_score_difference':max(abs(r['score']-float(old['scores'][j])) for j,r in enumerate(actual)),
        'seconds':seconds,'memory':process_memory_bytes()}
(ROOT/'sparse-verification.json').write_text(json.dumps(result,indent=2));print(json.dumps(result),flush=True)
