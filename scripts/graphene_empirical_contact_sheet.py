"""Display-only comparison of versioned masks on existing saved previews."""
from pathlib import Path
import argparse,json
from PIL import Image, ImageDraw, ImageFont

ROOT=Path(__file__).resolve().parents[1]
evidence=ROOT/'research/graphene-segmentation-20261014'
comparison=json.loads((evidence/'empirical-pilot-v2-comparison.json').read_text())
comparison['observations'] += [{**r,'after_selected':True} for r in json.loads((evidence/'empirical-pilot-promotions.json').read_text())]
parser=argparse.ArgumentParser(description=__doc__)
parser.add_argument('--analysis',type=Path)
parser.add_argument('--refinement',type=Path)
parser.add_argument('--output',type=Path)
args=parser.parse_args()
before=Path(comparison['before']);after=args.analysis or Path(comparison['after'])
refined_root=args.refinement or after.parent/'operator-empirical-pilot-v2-boundaries'
refined_index=json.loads((refined_root/json.loads((refined_root/'latest.json').read_text())['index_path']).read_text())['regions']
ledgers=[json.loads((p/'ledger.json').read_text()) for p in (before,after)]
examples=['chip001/tile_000215/36','chip001/tile_000639/121','chip001/tile_000547/176','chip001/tile_000660/176','chip001/tile_000269/52']
font=ImageFont.truetype('C:/Windows/Fonts/segoeui.ttf',18)
small=ImageFont.truetype('C:/Windows/Fonts/segoeui.ttf',14)
canvas=Image.new('RGB',(1000,len(examples)*320+60),'#10171e');draw=ImageDraw.Draw(canvas)
for x,title in zip((16,344,672),('Saved image','Previous mask','Current review outline')):
    draw.text((x,12),title,font=font,fill='#e6eef5')
for row,identifier in enumerate(examples):
    match=next(x for x in comparison['observations'] if x['original_id']==identifier)
    tile=identifier.split('/')[1];regions=[];documents=[]
    for base,ledger,key in zip((before,after),ledgers,('original_id','new_id')):
        d=json.loads((base/ledger[tile]['field_json']).read_text());documents.append(d)
        label=int(match[key].split('/')[-1]);regions.append(next(r for r in d['regions'] if r['instance_label']==label))
    source=Image.open(Path(documents[1]['prepared_input']['raw_tiff']).with_suffix('.jpg')).convert('RGB')
    points=[p for region in regions for p in region['polygon_source_px']]
    xs,ys=zip(*points);cx=(min(xs)+max(xs))/2;cy=(min(ys)+max(ys))/2
    side=max(max(xs)-min(xs),max(ys)-min(ys),55)+40
    left=max(0,min(source.width-side,cx-side/2));top=max(0,min(source.height-side,cy-side/2))
    box=(int(left),int(top),int(left+side),int(top+side));crop=source.crop(box).resize((260,260))
    y=60+row*320
    for column in range(3):
        x=16+column*328;panel=crop.copy();pd=ImageDraw.Draw(panel)
        if column:
            region=regions[column-1];color='#ffbf6b' if column==1 else ('#70dcc3' if match['after_selected'] else '#c2c8ce')
            polygons=[region['polygon_source_px'],*region.get('holes_source_px',[])]
            if column==2:
                rr=refined_index[match['new_id']]
                refinement=json.loads((refined_root/rr['shard_path']).read_text())['regions'][match['new_id']]
                polygons=refinement.get('polygons_px') or polygons
            for polygon in polygons:
                p=[((a-box[0])*260/(box[2]-box[0]),(b-box[1])*260/(box[3]-box[1])) for a,b in polygon]
                if p:pd.line(p+[p[0]],fill=color,width=2)
        canvas.paste(panel,(x,y))
    label=identifier+' → '+match['new_id'].split('/')[-1]+' · '+('Candidate' if match['after_selected'] else 'Uncertain')
    draw.text((16,y+266),label,font=small,fill='#e6eef5')
    draw.text((16,y+288),'Boundary: '+refinement['refinement_status'],font=small,fill='#9aafbd')
target=args.output or evidence/'empirical-pilot-review-comparison.png';canvas.save(target);print(target)
