"""Export immutable refinement evidence to the GUI annotation schema."""
from pathlib import Path
import argparse,hashlib,json


def digest(path):return hashlib.sha256(path.read_bytes()).hexdigest()
def read(path):return json.loads(path.read_text(encoding='utf-8-sig'))


def export(folder):
    original=read(folder/'catalog.json');summary=read(folder/'summary.json');plan=read(folder/'plan.json')
    if summary['state']!='complete' or original['plan_sha256']!=digest(folder/'plan.json'):
        raise ValueError('Incomplete or changed refinement evidence.')
    regions={};fields={}
    for key,row in original['regions'].items():
        path=folder/row['refinement_path']
        if digest(path)!=row['refinement_sha256']:raise ValueError('Refinement record changed.')
        result=read(path);accepted=row['refinement_status']=='edge_supported_proposal'
        fields[key.rsplit('/',1)[0]]=row['source_field_sha256']
        regions[key]={**row,'refined_polygons_px':row['polygons_px'],
            'polygons_px':row['polygons_px'] if accepted else row['original_polygons_px'],
            'refinement_status':'accepted' if accepted else row['refinement_status'],
            'qualification':'Measured local RGB boundary proposal; no manual boundary ground truth.' if accepted else
                'Original outline retained because the proposed contour needs review; raw refinement is preserved separately.'}
    payload={'schema':1,'run_id':original['run_id'],'plan_sha256':original['analysis_plan_sha256'],
        'refinement_plan_sha256':original['plan_sha256'],'source_fields':fields,'regions':regions,
        'exporter_sha256':digest(Path(__file__)),'source_catalog_sha256':digest(folder/'catalog.json'),
        'qualification':original['qualification']}
    target=folder/'gui-catalog.json'
    with target.open('x',encoding='utf-8') as stream:json.dump(payload,stream,indent=2,allow_nan=False)
    return target


if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('folder',type=Path);print(export(p.parse_args().folder))
