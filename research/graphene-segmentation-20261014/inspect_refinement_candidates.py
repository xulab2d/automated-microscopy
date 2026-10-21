"""Small read-only candidate contact sheet for offline refinement design."""
import json
from pathlib import Path
import numpy as np
from PIL import Image,ImageDraw
ROOT=Path(__file__).resolve().parents[2]
PROJECT=Path(json.loads((ROOT/'configs/storage.json').read_text())['project_root'])
rows=json.loads((Path(__file__).with_name('gui-api-verification.json')).read_text())['rows']
sheet=Image.new('RGB',(7*250,6*220),'white');draw=ImageDraw.Draw(sheet)
for i,r in enumerate(rows):
    tile=r['field'].split('/')[-1];p=PROJECT/'data/scan-20261013-221727-131d9ced/chip001/tiles'/f'{tile}.jpg'
    with Image.open(p) as image:
        h,w=r['source_shape_hw'];points=np.array(r['polygons_px'][0]);lo=np.maximum(points.min(0)-70,0);hi=np.minimum(points.max(0)+70,[w,h])
        crop=image.crop((int(lo[0]*image.width/w),int(lo[1]*image.height/h),int(hi[0]*image.width/w),int(hi[1]*image.height/h)))
        crop.thumbnail((245,190));x=(i%7)*250;y=(i//7)*220;sheet.paste(crop,(x,y+25))
        draw.text((x+3,y+3),f"{tile[5:]} #{r['id'].split('/')[-1]} {r['area_um2']:.0f}um2",fill='black')
sheet.save(Path(__file__).with_name('refinement-candidates.png'))
