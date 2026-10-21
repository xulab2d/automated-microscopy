"""Read-only actual-region audit contact, including formerly uncertain regions."""
from pathlib import Path
import json,sys
import numpy as np
from PIL import Image
import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt
root=Path(__file__).resolve().parents[2];project=Path(json.loads((root/'configs/storage.json').read_text())['project_root'])
run='scan-20261013-221727-131d9ced';folder=project/'analysis/graphene'/run/'chip001/useful-refinement-benchmark-v2'
index=json.loads((folder/'index-000010.json').read_text());chosen=sorted(index['regions'],key=lambda k:-index['regions'][k]['rank'])[:8]
chosen+=['chip001/tile_000639/121','chip001/tile_000660/167']
fig,axes=plt.subplots(5,2,figsize=(12,17),constrained_layout=True);audit=[]
for ax,key in zip(axes.flat,chosen):
    compact=index['regions'][key];row=json.loads((folder/compact['shard_path']).read_text())['regions'][key]
    tile=key.split('/')[1];image=np.asarray(Image.open(project/'data'/run/'chip001/tiles'/f'{tile}.jpg'))
    points=np.vstack(row['original_polygons_px']);low=np.maximum(points.min(0)-60,0);high=np.minimum(points.max(0)+60,[2464,2056])
    sx=image.shape[1]/2464;sy=image.shape[0]/2056;x0,y0=low;x1,y1=high
    ax.imshow(image[int(y0*sy):int(y1*sy),int(x0*sx):int(x1*sx)],extent=(x0,x1,y1,y0))
    for polygon in row['polygons_px']:
        p=np.asarray(polygon)
        if len(p):ax.plot(*np.vstack([p,p[:1]]).T,color='yellow',linewidth=.6)
    ax.axis('off');ax.set_title(f"{key}\nrank {row['rank']:.2f}, original selected={row['original_selected_for_review']}, area {row['metrics']['geometric_area_um2']:.0f}um2\n"+', '.join(row['artifact_flags']),fontsize=9)
    audit.append({k:row[k] for k in ('id','rank','original_selected_for_review','original_filter_reasons','filter_reasons','size_assessment','artifact_flags','likely_artifact')})
fig.savefig(Path(__file__).with_name('useful-region-samples.png'),dpi=130)
Path(__file__).with_name('useful-region-samples.json').write_text(json.dumps(audit,indent=2))
