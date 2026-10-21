"""Compare original and proposed contours for actual camera-cut regions."""
from pathlib import Path
import json
import numpy as np
from PIL import Image
import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt
root=Path(__file__).resolve().parents[2];project=Path(json.loads((root/'configs/storage.json').read_text())['project_root'])
run='scan-20261013-221727-131d9ced';folder=project/'analysis/graphene'/run/'chip001/useful-refinement-benchmark-v2'
index=json.loads((folder/'index-000010.json').read_text());ids=['chip001/tile_000329/5','chip001/tile_000368/1','chip001/tile_000943/6']
fig,axes=plt.subplots(3,2,figsize=(12,14),constrained_layout=True)
for axrow,key in zip(axes,ids):
    compact=index['regions'][key];row=json.loads((folder/compact['shard_path']).read_text())['regions'][key]
    tile=key.split('/')[1];image=np.asarray(Image.open(project/'data'/run/'chip001/tiles'/f'{tile}.jpg'))
    points=np.vstack(row['original_polygons_px']);low=np.maximum(points.min(0)-30,0);high=np.minimum(points.max(0)+30,[2464,2056]);x0,y0=low;x1,y1=high
    sx=image.shape[1]/2464;sy=image.shape[0]/2056
    for ax,name in zip(axrow,['original_polygons_px','refined_polygons_px']):
        ax.imshow(image[int(y0*sy):int(y1*sy),int(x0*sx):int(x1*sx)],extent=(x0,x1,y1,y0))
        for polygon in row[name]:
            p=np.asarray(polygon)
            if len(p):ax.plot(*np.vstack([p,p[:1]]).T,color='yellow',linewidth=.6)
        ax.axis('off');ax.set_title(key+'\n'+name,fontsize=10)
fig.savefig(Path(__file__).with_name('camera-cut-refinement-comparison.png'),dpi=130)
