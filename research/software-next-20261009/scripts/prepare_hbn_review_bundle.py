"""Build the source-pinned hBN review bundle for the operator-labelled run."""
import json,hashlib
from pathlib import Path
project=Path('G:/Shared drives/Xu Lab - Microscopy and Instruments/Automated Microscopy')
run='scan-20261015-223549-98fae566'
root=project/'analysis/hbn'/run/'chip001'
models=root/'models'
model=json.loads((models/'hbn90-conditional-v2.json').read_text())
model['geometry_method']='observed_rgb_v1'
model['alternative_models']=json.loads((models/'transparent-film-alternatives-v1.json').read_text())['models']
model['operator_annotations']=[]
examples=[(1200,1189,1618.5,1904.5,'thin_positive'),(1700,307,364.5,1192.5,'thin_positive'),(1700,307,304.5,1212.5,'thin_positive'),(1700,83,714.5,564.5,'thin_positive'),(1200,925,1970.5,1784.5,'bulk'),(1200,769,384.5,1556.5,'bulk'),(1200,956,2096.5,2008.5,'bulk'),(1700,546,1850.5,1722.5,'bulk'),(1700,550,2054.5,1744.5,'bulk')]
for n,old,x,y,label in examples:
 tile=f'tile_{n:06d}'
 field=json.loads(next((root/'hbn-pilot-v2/fields').glob(tile+'-*/field.json')).read_text())
 model['operator_annotations'].append(dict(tile=tile,point_px=[x,y],label=label,source=f'Operator visual judgement, 2026-10-16; old hbn-pilot-v2 {tile}/{old}. Point chosen within corresponding image region; sub-5nm belief is uncalibrated.',source_raw_sha256=field['source_hashes'][f'tiles/{tile}.tiff']))
model['review_qualification']='Conditional model comparisons; source-pinned operator examples are not chemical or thickness calibration. Geometry measured independently of optical models.'
p=models/'hbn90-image-thin-v3.json'
if p.exists():raise FileExistsError(p)
p.write_text(json.dumps(model,indent=2))
print(p, hashlib.sha256(p.read_bytes()).hexdigest())
