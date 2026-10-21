"""Scientific display of candidate masks; JPEGs used for visual background only."""
import json
from pathlib import Path
import numpy as np
from PIL import Image
import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt
root=Path(__file__).resolve().parents[2];project=Path(json.loads((root/'configs/storage.json').read_text())['project_root']);rid='scan-20261013-221727-131d9ced'
base=project/'analysis/graphene'/rid/'chip001';tile='tile_000500';records=[]
for version in ['pilot-conditional-v2','pilot-external-v1']:
    folder=base/version;ledger=json.loads((folder/'ledger.json').read_text());p=folder/ledger[tile]['field_json'];d=json.loads(p.read_text())
    with np.load(p.with_name('masks.npz')) as a:labels=a['labels'];mask=np.isin(labels,[r['instance_label'] for r in d['regions'] if r['selected_for_review']])
    records.append((d,mask))
image=np.asarray(Image.open(project/'data'/rid/'chip001/tiles'/f'{tile}.jpg'))
chosen=sorted([r for r in records[0][0]['regions'] if r['selected_for_review']],key=lambda r:-r['area_um2'])[:4]
fig,axes=plt.subplots(len(chosen),3,figsize=(12,12),constrained_layout=True)
h,w=records[0][0]['reduction']['source_shape_hw'];sy,sx=image.shape[0]/h,image.shape[1]/w
for row,r in enumerate(chosen):
    points=np.asarray(r['polygon_source_px']);low=np.maximum(np.floor(points.min(0)-70),0).astype(int);high=np.minimum(np.ceil(points.max(0)+70),[w,h]).astype(int);x0,y0=low;x1,y1=high
    for col,ax in enumerate(axes[row]):
        ax.imshow(image[int(y0*sy):int(y1*sy),int(x0*sx):int(x1*sx)],extent=(x0,x1,y1,y0));ax.set(xlim=(x0,x1),ylim=(y1,y0));ax.set_xticks([]);ax.set_yticks([])
        if col:
            d,mask=records[col-1];factor=d['reduction']['factor'];crop=mask[y0//factor:(y1+factor-1)//factor,x0//factor:(x1+factor-1)//factor]
            if crop.any() and not crop.all():
                yy,xx=np.mgrid[y0//factor:(y1+factor-1)//factor,x0//factor:(x1+factor-1)//factor]
                ax.contour(xx*factor+(factor-1)/2,yy*factor+(factor-1)/2,crop,levels=[.5],colors=['#00f6ed'],linewidths=.7)
        title=['Saved preview','Conditional90 mask','External90 mask'][col]
        ax.set_title(title+(f" · #{r['instance_label']} · {r['area_um2']:.1f} µm²" if col==0 else ''),fontsize=10)
fig.suptitle('Tile500: strongest optical-compatible regions are fragmented / narrow\nOutlines are conditional proposals, not confirmed graphene; inspect full field and unmatched regions too',fontsize=13)
fig.savefig(base/'model-comparison/candidate-comparison.png',dpi=150)
print(base/'model-comparison/candidate-comparison.png')
