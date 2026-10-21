"""Offline DINOv2 crop embeddings and unlabeled cluster review; no hardware.

Run in the optional learned-model environment. Images never leave the computer
for inference. Embeddings are exploratory appearance descriptors, not material,
thickness, cleanliness or crystallinity ground truth.
"""
from pathlib import Path
import argparse, hashlib, html, json, os, sys, time
os.environ.setdefault('OMP_NUM_THREADS','2')
os.environ.setdefault('MKL_NUM_THREADS','2')
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
import numpy as np
from PIL import Image, ImageOps
from flakefinder.model import atomic_json
from flakefinder.storage import resolve_output_path
from flakefinder.contrast_plot import ContrastDistribution


def digest(path):
    h=hashlib.sha256()
    with Path(path).open('rb') as stream:
        for block in iter(lambda:stream.read(1024*1024),b''):h.update(block)
    return h.hexdigest()


def crop_region(image,bbox,shape):
    """Keep the full object and context, with no centre-crop truncation."""
    h,w=shape[:2];sx=image.width/w;sy=image.height/h
    x,y,bw,bh=map(float,bbox)
    if not all(np.isfinite([x,y,bw,bh])) or bw<=0 or bh<=0:raise ValueError('Invalid object bounds')
    pad=max(8.,.12*max(bw,bh))
    bounds=(max(0,int((x-pad)*sx)),max(0,int((y-pad)*sy)),
            min(image.width,int(np.ceil((x+bw+pad)*sx))),min(image.height,int(np.ceil((y+bh+pad)*sy))))
    if bounds[2]<=bounds[0] or bounds[3]<=bounds[1]:raise ValueError('Object outside preview')
    crop=image.crop(bounds)
    return ImageOps.pad(crop,(224,224),method=Image.Resampling.BICUBIC,color=(128,128,128)),bounds


def review(root,rows,vectors,campaign,clusters):
    from sklearn.cluster import MiniBatchKMeans
    from sklearn.decomposition import PCA
    if not len(rows):return {'objects':0,'clusters':0}
    vectors=vectors/np.maximum(np.linalg.norm(vectors,axis=1,keepdims=True),1e-12)
    count=min(clusters,max(1,len(rows)//12))
    model=MiniBatchKMeans(n_clusters=count,random_state=21,n_init=3,batch_size=512)
    labels=model.fit_predict(vectors)
    projection=PCA(n_components=min(2,len(rows),vectors.shape[1]),random_state=21).fit_transform(vectors)
    groups=[];thumbs=root/'cluster-previews';thumbs.mkdir(exist_ok=True)
    for cluster in range(count):
        ids=np.flatnonzero(labels==cluster)
        distances=np.linalg.norm(vectors[ids]-model.cluster_centers_[cluster],axis=1)
        order=np.argsort(distances)
        # Show both typical and distant members, so a mixed cluster is visible.
        selected=list(ids[order[:12]])+list(ids[order[-4:]]) if len(ids)>12 else list(ids[order])
        selected=list(dict.fromkeys(selected))
        examples=[]
        for index in selected:
            row=rows[index];path=campaign/row['preview']
            with Image.open(path) as im:crop,_=crop_region(im.convert('RGB'),row['bbox_px'],row['shape'])
            filename=f'c{cluster:03d}-{index:06d}.jpg';crop.save(thumbs/filename,quality=88)
            examples.append({'index':int(index),'image':'cluster-previews/'+filename,
                             'id':row['id'],'field':row['field'],'area_um2':row['area_um2']})
        groups.append({'cluster':cluster,'count':len(ids),'semantic_label':None,'examples':examples,
                       'median_distance':float(np.median(distances))})
    assignments=[{**row,'cluster':int(label),'projection':p.tolist()} for row,label,p in zip(rows,labels,projection)]
    atomic_json(root/'clusters.json',{'schema':1,'method':'DINOv2-small CLS, unit length, MiniBatchKMeans',
        'objects':len(rows),'groups':groups,'assignments':assignments,'labels_are_unvalidated':True})
    cards=[]
    for group in groups:
        images=''.join(f'<figure><img loading="lazy" src="{html.escape(e["image"])}"><figcaption>{html.escape(e["field"])} · {e["area_um2"]:.1f} µm²</figcaption></figure>' for e in group['examples'])
        cards.append(f'<section><h2>Cluster {group["cluster"]} · {group["count"]} regions</h2>'
            f'<label>Review label <select data-cluster="{group["cluster"]}"><option>unlabeled</option>'
            '<option>mixed</option><option>substrate</option><option>off-chip edge</option><option>dust</option>'
            '<option>residue</option><option>flake-like</option><option>folded or cracked</option></select></label>'
            f'<div class="grid">{images}</div></section>')
    page='''<!doctype html><meta charset="utf-8"><title>Unlabeled appearance clusters</title>
<style>body{background:#111820;color:#dde5eb;font:16px system-ui;margin:30px}header{position:sticky;top:0;background:#111820;padding:12px;z-index:2}h1{margin:0}button,select{font:inherit;padding:6px}section{border-top:1px solid #40505c;padding:16px 0}.grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(160px,1fr));gap:10px}figure{margin:0}img{width:100%}figcaption{font-size:12px;color:#afc1cc}</style>
<header><h1>Appearance clusters — review required</h1><p>Typical and distant members shown. Labels are suggestions for review, not automatically propagated scientific truth. No thickness or crystal-quality inference.</p><button id="save">Download cluster labels</button></header>'''+''.join(cards)+'''
<script>document.getElementById('save').onclick=()=>{const labels=[...document.querySelectorAll('select')].map(x=>({cluster:+x.dataset.cluster,label:x.value,scope:'cluster-level provisional annotation; inspect members before training'}));const b=new Blob([JSON.stringify({schema:1,created_at:new Date().toISOString(),labels},null,2)],{type:'application/json'});const a=document.createElement('a');a.href=URL.createObjectURL(b);a.download='appearance-cluster-labels.json';a.click();setTimeout(()=>URL.revokeObjectURL(a.href),1000)};</script>'''
    (root/'review.html').write_text(page,encoding='utf-8')
    return {'objects':len(rows),'clusters':count}


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--campaign',type=Path,required=True)
    parser.add_argument('--output',type=Path,required=True)
    parser.add_argument('--model',type=Path,required=True)
    parser.add_argument('--min-area',type=float,default=25.)
    parser.add_argument('--batch-size',type=int,default=16)
    parser.add_argument('--threads',type=int,default=2)
    parser.add_argument('--clusters',type=int,default=32)
    parser.add_argument('--max-fields',type=int)
    args=parser.parse_args()
    if not (args.min_area>=0 and args.batch_size>0 and args.threads>0 and args.clusters>0):parser.error('Invalid positive settings')
    if args.max_fields is not None and args.max_fields<1:parser.error('--max-fields must be positive')
    root=resolve_output_path(args.output);root.mkdir(parents=True,exist_ok=True);(root/'fields').mkdir(exist_ok=True)
    campaign=args.campaign.resolve();model_path=args.model.resolve()
    model_receipt=json.loads((model_path/'download-provenance.json').read_text())
    for name in ('model.safetensors','config.json','preprocessor_config.json'):
        if digest(model_path/name)!=model_receipt['files'][name]['sha256']:
            raise ValueError('DINOv2 model/processor differs from the recorded download: '+name)
    import torch
    from transformers import AutoModel,AutoImageProcessor
    torch.set_num_threads(args.threads)
    processor=AutoImageProcessor.from_pretrained(model_path,local_files_only=True,use_fast=False)
    model=AutoModel.from_pretrained(model_path,local_files_only=True,use_safetensors=True).eval()
    sources,warnings,statuses=ContrastDistribution(campaign).sources()
    if args.max_fields:sources=sources[:args.max_fields]
    settings={'version':1,'model':model_receipt,'min_area_um2':args.min_area,
              'processor':'official fixed normalization; square pad full context224; no crop-specific color adjustment',
              'source':'stored camera preview; display gamma is not quantitative optical contrast',
              'script_sha256':digest(__file__)}
    source_copy=root/('source-'+settings['script_sha256']+'.py')
    if source_copy.exists():
        if digest(source_copy)!=settings['script_sha256']:
            raise ValueError('Recorded source snapshot has changed')
    else:
        source_bytes=Path(__file__).read_bytes()
        if hashlib.sha256(source_bytes).hexdigest()!=settings['script_sha256']:
            raise ValueError('Source changed during run initialization')
        with source_copy.open('xb') as stream:stream.write(source_bytes)
    atomic_json(root/'configuration.json',settings)
    all_rows=[];all_vectors=[];errors=[];new=0;started=time.monotonic();cancelled=False
    for source in sources:
        if (root/'STOP').exists():break
        key=source['key'];field_id=key.replace('/','__')
        jsonpath=root/'fields'/(field_id+'.json');vectorpath=jsonpath.with_suffix('.npz')
        preview=campaign/source['chip']/'tiles'/(source['tile']+'.jpg')
        try:
            stamp={'source_sha256':digest(source['path']),'preview_sha256':digest(preview),'settings':settings}
            identity=hashlib.sha256(json.dumps(stamp,sort_keys=True).encode()).hexdigest()
            old=json.loads(jsonpath.read_text()) if jsonpath.exists() else {}
            if old.get('identity')==identity and vectorpath.is_file() and digest(vectorpath)==old.get('vectors_sha256'):
                rows=old['rows'];vectors=np.load(vectorpath,allow_pickle=False)['embeddings']
            else:
                field=json.loads(source['path'].read_text());shape=field.get('tile',{}).get('shape') or source['shape']
                observations=[r for r in field.get('rows',[]) if float(r.get('area_um2',0))>=args.min_area and r.get('bbox_px')]
                rows=[];chunks=[]
                with Image.open(preview) as im:
                    im=im.convert('RGB')
                    for start in range(0,len(observations),args.batch_size):
                        if (root/'STOP').exists():
                            cancelled=True
                            break
                        batch=observations[start:start+args.batch_size]
                        images=[crop_region(im,row['bbox_px'],shape)[0] for row in batch]
                        inputs=processor(images=images,return_tensors='pt',do_resize=False,do_center_crop=False)
                        with torch.inference_mode():features=model(**inputs).last_hidden_state[:,0,:].numpy()
                        chunks.append(features.astype(np.float32))
                        rows.extend({'id':r.get('id') or key+':'+str(r['instance_label']),'field':key,
                            'preview':preview.relative_to(campaign).as_posix(),'bbox_px':r['bbox_px'],
                            'shape':shape,'area_um2':r['area_um2'],'source_instance':r.get('instance_label'),
                            'old_selected':bool(r.get('selected_for_review'))} for r in batch)
                if cancelled:break  # Never commit a partial field as complete.
                vectors=np.concatenate(chunks) if chunks else np.empty((0,model.config.hidden_size),np.float32)
                part=vectorpath.with_suffix('.tmp.npz');np.savez_compressed(part,embeddings=vectors);part.replace(vectorpath)
                atomic_json(jsonpath,{'schema':1,'identity':identity,'rows':rows,'provenance':stamp,'vectors_sha256':digest(vectorpath)})
                new+=1
            all_rows.extend(rows);all_vectors.append(vectors)
        except Exception as exc:
            errors.append({'field':key,'error':f'{type(exc).__name__}: {exc}'})
        status={'state':'running','fields_done':len(all_vectors),'fields_total':len(sources),'new_fields':new,
                'objects':len(all_rows),'elapsed_seconds':time.monotonic()-started,'errors':errors,
                'source_field_statuses':statuses,'warnings':warnings,'hardware_access':False}
        atomic_json(root/'progress.json',status)
        print(json.dumps({k:status[k] for k in ('fields_done','fields_total','objects','elapsed_seconds')}),flush=True)
        if len(errors)>=3:
            atomic_json(root/'progress.json',{**status,'state':'failed','reason':'Three failed embedding fields; inspect errors before resuming.'})
            raise RuntimeError('Three failed embedding fields; stop and inspect progress.json')
    stopped=(root/'STOP').exists()
    if stopped or cancelled:
        atomic_json(root/'progress.json',{'state':'stopped','fields_done':len(all_vectors),'fields_total':len(sources),
            'objects':len(all_rows),'errors':errors,'elapsed_seconds':time.monotonic()-started,'hardware_access':False})
        return
    try:
        result=review(root,all_rows,np.concatenate(all_vectors) if all_vectors else np.empty((0,384)),campaign,args.clusters)
    except Exception as exc:
        atomic_json(root/'progress.json',{'state':'embedding_complete_clustering_failed','fields_done':len(all_vectors),
            'fields_total':len(sources),'objects':len(all_rows),'error':f'{type(exc).__name__}: {exc}',
            'source_field_statuses':statuses,'errors':errors,'hardware_access':False})
        raise
    atomic_json(root/'progress.json',{'state':'stopped' if stopped else 'complete_with_errors' if errors else 'complete',
        'fields_done':len(all_vectors),'fields_total':len(sources),'source_field_statuses':statuses,
        'errors':errors,'warnings':warnings,'elapsed_seconds':time.monotonic()-started,'hardware_access':False,**result})


if __name__=='__main__':main()
