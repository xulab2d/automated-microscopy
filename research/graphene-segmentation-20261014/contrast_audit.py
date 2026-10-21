"""Independent per-field normalization and contrast-distribution audit."""
import os
for k in ('OMP_NUM_THREADS','OPENBLAS_NUM_THREADS','MKL_NUM_THREADS','NUMEXPR_NUM_THREADS'):os.environ[k]='1'
import gc,importlib.util,json,sys,time
from pathlib import Path
import numpy as np
import cv2
root=Path(__file__).resolve().parents[2];production=root/'research/software-next-20261009';sys.path.insert(0,str(production))
from flakefinder.material_input import prepare_material_input
from flakefinder.storage import resolve_output_path
spec=importlib.util.spec_from_file_location('watcher',production/'scripts/watch_graphene_scan.py');w=importlib.util.module_from_spec(spec);spec.loader.exec_module(w)
cv2.setNumThreads(1);w.background_priority()
project=Path(json.loads((root/'configs/storage.json').read_text())['project_root']);run_id='scan-20261013-221727-131d9ced';run=project/'data'/run_id
output=resolve_output_path(project/'analysis/graphene'/run_id/'chip001/model-comparison');output.mkdir(parents=True,exist_ok=True)
selected={f'chip001/tile_{n:06d}' for n in [100,300,400,500,600,950]}
points=[];records=[]
for entry in w.committed_fields(run):
    if entry['field'] not in selected:continue
    start=time.monotonic();w.verify_source(entry)
    arrays,provenance=prepare_material_input(chip_folder=entry['chip'],tile_id=entry['tile'],context=w.input_context(entry,run_id),maximum_pixels=6_000_000)
    valid=arrays['valid_mask']&~arrays['saturated_mask'];sample=arrays['contrast_rgb'][valid];sample=sample[::max(1,len(sample)//10000)].copy()
    points.append(sample)
    rec={'field':entry['field'],'valid_pixels':int(valid.sum()),'substrate_pixels':provenance['substrate_pixels'],
         'noise_covariance':arrays['noise_covariance'].tolist(),'contrast_quantiles_rgb':np.quantile(sample,[.01,.1,.5,.9,.99],axis=0).tolist(),
         'normalization':provenance['normalization'],'provenance':provenance,'seconds':time.monotonic()-start}
    records.append(rec);print(json.dumps({k:rec[k] for k in ('field','valid_pixels','substrate_pixels','seconds')}),flush=True)
    del arrays,provenance,sample;gc.collect()
w.save(output/'contrast-audit.json',{'records':records,'sampling':'At most approximately10000 deterministic spatial-stride valid raw-contrast pixels per selected field; no model color selection.',
     'qualification':'Observed pixel distribution, not identified graphene or independent labeled validation.'})
import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt
x=np.concatenate(points);model=w.read(output/'conditional90.json');theory=np.asarray(model['contrast_rgb']);external=w.read(output/'external90.json');means=np.asarray([c['mean_rgb'] for c in external['components']])
fig,axes=plt.subplots(1,2,figsize=(12,5),constrained_layout=True)
for ax,i,j in [(axes[0],0,1),(axes[1],1,2)]:
    ax.hexbin(x[:,i],x[:,j],gridsize=100,extent=(-.4,.6,-.4,.6),bins='log',mincnt=1,cmap='Greys')
    for k in range(1,5):ax.scatter(theory[:,k,i],theory[:,k,j],s=13,color='#c85819',alpha=.45)
    ax.plot(means[:,i],means[:,j],'o-',color='#246ccc',label='External90nm populations (different camera)')
    ax.scatter([],[],color='#c85819',label='Conditional90nm optics, 1–4 structural spacings')
    ax.axhline(0,color='#999',lw=.5);ax.axvline(0,color='#999',lw=.5)
    ax.set(xlabel='RGB'[i]+' contrast',ylabel='RGB'[j]+' contrast',xlim=(-.4,.6),ylim=(-.4,.6))
axes[0].legend(fontsize=8,loc='upper left');fig.suptitle('Saved chip: raw substrate-relative contrast vs uncalibrated graphene priors\nSix spatially separated fields; darker-than-substrate contrast is positive',fontsize=12)
fig.savefig(output/'contrast-priors.png',dpi=160);plt.close(fig)
print(output/'contrast-priors.png',flush=True)
