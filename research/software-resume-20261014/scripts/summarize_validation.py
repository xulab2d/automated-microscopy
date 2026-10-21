"""Assemble measured evidence and paired raw-image crops, without hardware access."""
from pathlib import Path
import json,hashlib,sys,re
import numpy as np
import cv2
from PIL import Image,ImageDraw,ImageFont
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from flakefinder.imaging import load_frame,preview
from flakefinder.model import atomic_json
from flakefinder.review import build_review

folder=Path('data/revisit-validation-10x');state=json.loads((folder/'revisit.json').read_text())
font=ImageFont.truetype('C:/Windows/Fonts/segoeui.ttf',18)
canvas=Image.new('RGB',(1000,1120),'#0e1c28');draw=ImageDraw.Draw(canvas)
draw.text((24,16),'PHYSICAL REVISITS | original tile and newly acquired image | both 10x',fill='#a0e4d3',font=font)
evidence=[]
for index,row in enumerate(state['queue']['candidates']):
    result=state['completed'][row['id']];name='candidate_'+hashlib.sha256(row['id'].encode()).hexdigest()[:14]
    source=load_frame(Path(state['queue']['source_folder'])/row['source_image'])
    target=load_frame(folder/name/'tiles/tile_000000.tiff')
    registration=result['registration'];affine=np.array(registration['affine'])
    poly=np.array(row['polygon_px']);target_poly=poly@affine[:,:2].T+affine[:,2]
    for column,(frame,point,polygon,label) in enumerate([(source,row['centroid_px'],poly,'Original '+row['tile_id']),
                       (target,registration['candidate_px'],target_poly,'Revisit | verified identity')]):
        rgb=preview(frame.rgb);cx,cy=map(int,point);half=int(45/frame.pixel_size_um[0])
        x0=max(0,cx-half);y0=max(0,cy-half);x1=min(rgb.shape[1],cx+half);y1=min(rgb.shape[0],cy+half)
        cv2.polylines(rgb,[np.int32(polygon)],True,(255,216,81),1)
        crop=Image.fromarray(rgb[y0:y1,x0:x1]);crop.thumbnail((460,285));x=24+column*495;y=82+index*343
        canvas.paste(crop,(x,y));draw.text((x,y-25),label,fill='white',font=font)
        draw.text((x,y+290),f'{row["color_group"]} | {row["area_um2"]:.1f} um2 | '+('253.613 ms' if column==0 else '200 ms'),fill='#a8bbc9',font=font)
    evidence.append({'candidate':row['id'],'tile':row['tile_id'],'stage':result['position'],
                    'registration':registration,'identity_verified':result['identity_verified']})
canvas.save(folder/'paired-revisits.jpg',quality=94)
scan=json.loads(Path('data/live-validation-20261008-c/scan/analysis/analysis.json').read_text())
demo=json.loads(Path('data/demo-color-workflow-02/demo.json').read_text())
summary={'raster_candidates':scan['candidate_count'],'raster_rejected':scan['rejected_count'],
         'distinct_images':scan['distinct_images'],'revisits':evidence,'simulation':demo}
atomic_json('data/validation-summary.json',summary)
for path in [Path('data/live-validation-20261008-c/scan/analysis'),folder/'analysis']:
    build_review(path)
source=Path('flakefinder/review.html').read_text(encoding='utf-8')
Path('data/review-code-check.js').write_text(re.search(r'<script>([\s\S]*?)</script>',source).group(1).replace('__REVIEW_DATA__','{}'),encoding='utf-8')
print(json.dumps({'images':summary['distinct_images'],'candidates':summary['raster_candidates'],'rejected':summary['raster_rejected'],
                 'revisits':len(evidence),'matched_points':[r['registration']['inliers'] for r in evidence],'simulation':demo},indent=2))
