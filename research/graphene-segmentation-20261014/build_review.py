"""Portable review of pinned offline segmentation, no microscope access."""
import argparse,hashlib,html,json,shutil
from pathlib import Path
from PIL import Image

def read(p):return json.loads(p.read_text(encoding='utf-8-sig'))
def digest(p):return hashlib.sha256(p.read_bytes()).hexdigest()
def js(v):return json.dumps(v,separators=(',',':'),allow_nan=False).replace('<','\\u003c')

def build(folder):
    folder=folder.resolve();plan=read(folder/'plan.json');pin=plan['pin'];identity=pin['identity']
    project=folder.parents[4];run=project/'data'/identity['run_id'];chip=run/pin['chip']
    if digest(run/'scan-request.json')!=identity['scan_request_sha256'] or digest(chip/'manifest.json')!=pin['manifest_sha256']:
        raise ValueError('Source identity changed')
    out=folder/'review';out.mkdir(exist_ok=True);(out/'fields').mkdir(exist_ok=True);(out/'images').mkdir(exist_ok=True);(out/'crops').mkdir(exist_ok=True)
    ledger=read(folder/'ledger.json');fields=[];candidates=[]
    for tile,item in sorted(ledger.items()):
        payload_path=folder/item['field_json']
        if digest(payload_path)!=item['hashes'][item['field_json']]:raise ValueError('Changed field record')
        d=read(payload_path);regions=d.get('regions') or []
        fields.append({'tile':tile,'state':item['state'],'reason':item.get('reason'),'regions':len(regions),
                       'compatible':sum(r['selected_for_review'] for r in regions),'area25':sum(r['area_um2']>=25 for r in regions)})
        preview=chip/'tiles'/f'{tile}.jpg';target=out/'images'/f'{tile}.jpg'
        if preview.is_file() and not target.exists():shutil.copyfile(preview,target)
        if not regions:continue
        rows=[]
        for r in regions:
            row={k:r.get(k) for k in ('instance_label','parent_instance_label','area_um2','contrast_rgb','selected_for_review','filter_reasons','class_name','polygon_source_px','holes_source_px','measurement','usable_rectangle','source_shape_hw','stage_xy_um')}
            row['source_tiff_sha256']=d['source_hashes'][f'tiles/{tile}.tiff'];rows.append(row)
            if r['selected_for_review'] and r['area_um2']>=4:candidates.append({'tile':tile,**row})
        payload={'tile':tile,'rows':rows,'shape':d['reduction']['source_shape_hw'],'resolution_um':d['reduction']['boundary_resolution_um'],
                 'source_hashes':d['source_hashes'],'report':d['report'],'field_json_sha256':digest(payload_path)}
        (out/'fields'/f'{tile}.js').write_text('showField('+js(payload)+');',encoding='utf-8')
    candidates=sorted(candidates,key=lambda r:-r['area_um2'])[:60]
    for r in candidates:
        source=out/'images'/f"{r['tile']}.jpg"
        if not source.exists():continue
        with Image.open(source) as im:
            h,w=r['source_shape_hw'];sx=im.width/w;sy=im.height/h
            points=r['polygon_source_px'];x=[p[0] for p in points];y=[p[1] for p in points]
            pad=90;left=max(0,min(x)-pad);top=max(0,min(y)-pad);right=min(w,max(x)+pad);bottom=min(h,max(y)+pad)
            crop=im.crop((int(left*sx),int(top*sy),int(right*sx),int(bottom*sy)));crop.thumbnail((300,230))
            name=f"{r['tile']}-{r['instance_label']}.jpg";crop.save(out/'crops'/name,quality=92);r['crop']='crops/'+name
    data={'run':identity['run_id'],'chip':pin['chip'],'version':folder.name,'fields':fields,
          'candidates':[{k:r[k] for k in ('tile','instance_label','area_um2','contrast_rgb','crop') if k in r} for r in candidates],
          'identity':identity,'model_sha256':plan['model_sha256'],'analysis_config':pin['config']}
    (out/'review-data.js').write_text('const DATA='+js(data)+';',encoding='utf-8')
    template=Path(__file__).with_name('review-template.html').read_text(encoding='utf-8')
    (out/'index.html').write_text(template,encoding='utf-8')
    (out/'review-provenance.json').write_text(json.dumps({'analysis':str(folder),'plan_sha256':digest(folder/'plan.json'),'ledger_sha256':digest(folder/'ledger.json'),
        'source_images':'Unmodified acquisition JPEG copies for display only; quantitative contrast derived from hash-verified raw TIFF.',
        'accuracy':'No human ground truth or validated chemical/layer labels. Per-field regions may duplicate overlapping physical flakes.',
        'fields':len(fields),'crop_selection':'Largest optical-compatible regions >=4um2; candidate-biased navigation, not validation set.'},indent=2),encoding='utf-8')
    print(out/'index.html')

if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('analysis',type=Path);build(p.parse_args().analysis)
