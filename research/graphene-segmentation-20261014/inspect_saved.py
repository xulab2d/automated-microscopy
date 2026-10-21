"""Representative saved previews, display only; no JPEG photometry."""
import json
from pathlib import Path
from PIL import Image,ImageDraw
root=Path(__file__).resolve().parents[2]
project=Path(json.loads((root/'configs/storage.json').read_text())['project_root'])
run=project/'data/scan-20261013-221727-131d9ced'
out=run/'analysis-graphene-20261014-inspection'
out.mkdir(exist_ok=True)
ids=[0,50,100,150,200,250,300,350,400,450,500,550,600,650,700,750,800,850,900,950,998]
canvas=Image.new('RGB',(1200,7*285),'#182027');draw=ImageDraw.Draw(canvas)
for i,n in enumerate(ids):
    tile=f'tile_{n:06d}';image=Image.open(run/'chip001/tiles'/f'{tile}.jpg');image.thumbnail((390,250))
    x=(i%3)*400;y=(i//3)*285
    canvas.paste(image,(x,y+25));draw.text((x+8,y+5),tile,fill='white')
canvas.save(out/'sample-previews.jpg',quality=90)
selection={'source_run':run.name,'chip':'chip001','method':'Every50th committed tile plus final tile; JPEG display only, no color inference.',
           'tiles':[f'tile_{n:06d}' for n in ids]}
(out/'selection.json').write_text(json.dumps(selection,indent=2))
print(out/'sample-previews.jpg')
