"""Versioned, raw-evidence refinement of explicit saved candidate IDs only."""
from pathlib import Path
import argparse,hashlib,json,os,sys,gc
ROOT=Path(__file__).resolve().parents[2];RELEASE=ROOT/'research/software-next-20261009'
sys.path[:0]=[str(RELEASE),str(RELEASE/'scripts')]
import analyze_graphene_run as driver
import numpy as np
import cv2
from flakefinder import graphene_refinement as refinement


def run(args):
    source=args.analysis.resolve();project=source.parents[4];run_id=source.parents[1].name;chip_name=source.parent.name
    if not args.version.replace('-','').replace('_','').isalnum():raise ValueError('Simple version name required.')
    out=source.parent/args.version
    if out==source:raise ValueError('Use a distinct refinement version.')
    out.mkdir(exist_ok=False);driver.cpu_policy()
    def check():
        if (out/'STOP').exists():raise driver.watcher.AnalysisStop('Independent refinement STOP')
    plan=driver.read(source/'plan.json',check);ledger=driver.read(source/'ledger.json',check)
    chip=project/'data'/run_id/chip_name
    if driver.digest(chip/'manifest.json',check)!=plan['pin']['manifest_sha256']:raise ValueError('Source manifest changed.')
    if driver.digest(chip.parent/'scan-request.json',check)!=plan['pin']['identity']['scan_request_sha256']:raise ValueError('Source request changed.')
    snapshot=driver.read(args.candidates,check)
    ids=args.ids.split(',') if args.ids else [row['id'] for row in snapshot['rows']]
    ids=sorted(set(ids))
    proof={'kind':'candidate_raw_boundary_refinement_v1','source_analysis':str(source),'candidate_ids':ids,
        'source_plan_sha256':driver.digest(source/'plan.json',check),'source_ledger_sha256':driver.digest(source/'ledger.json',check),
        'candidate_snapshot_sha256':driver.digest(args.candidates,check),
        'code_sha256':{str(p):driver.digest(p,check) for p in (Path(__file__),Path(refinement.__file__),Path(driver.__file__))},
        'options':refinement.asdict(refinement.RefinementOptions()),'original_outputs_modified':False,
        'selection_scope':'Explicit original proposals; optional IDs can include uncertain regions. No material-colour rejection.'}
    driver.watcher.save(out/'plan.json',proof);results=[];catalog={};cached=None
    for candidate_id in ids:
        check();parts=candidate_id.split('/')
        if len(parts)!=3 or parts[0]!=chip_name:raise ValueError('Candidate belongs to another chip.')
        _,tile,label=parts;label=int(label)
        if cached!=tile:
            entry=ledger[tile];field_path=source/entry['field_json']
            if driver.digest(field_path,check)!=entry['hashes'][entry['field_json']]:raise ValueError('Source field record changed.')
            field=driver.read(field_path,check);maskpath=field_path.with_name('masks.npz');relative=maskpath.relative_to(source).as_posix()
            if driver.digest(maskpath,check)!=entry['hashes'][relative]:raise ValueError('Source masks changed.')
            with np.load(maskpath,allow_pickle=False) as masks:
                labels=masks['labels'];valid=masks['valid_mask'];support=masks['substrate_mask']
            rawpath=chip/'tiles'/f'{tile}.tiff'
            if driver.digest(rawpath,check)!=field['source_hashes'][f'tiles/{tile}.tiff']:raise ValueError('Source raw changed.')
            raw=driver.retry_read(lambda:cv2.imread(str(rawpath),cv2.IMREAD_UNCHANGED),check)
            if raw is None:raise ValueError('Raw TIFF decode failed.')
            raw=raw[:,:,::-1];factor=field['reduction']['factor'];h,w=labels.shape
            image=raw[:h*factor,:w*factor].reshape(h,factor,w,factor,3).mean(axis=(1,3),dtype=np.float32)
            del raw;pixel=np.array(field['reduction']['boundary_resolution_um']);cached=tile;gc.collect()
        region=next(r for r in field['regions'] if r['instance_label']==label)
        yy,xx=np.nonzero(labels==label)
        if not len(xx):raise ValueError('Candidate label absent from exact original masks.')
        pad=int(np.ceil(12/min(pixel)));x0=max(0,xx.min()-pad);x1=min(labels.shape[1],xx.max()+pad+1)
        y0=max(0,yy.min()-pad);y1=min(labels.shape[0],yy.max()+pad+1);section=np.s_[y0:y1,x0:x1]
        refined=refinement.refine_candidate(image[section],labels[section]==label,valid[section],pixel,substrate_mask=support[section])
        folder=out/'candidates'/f'{tile}-{label}';folder.mkdir(parents=True)
        np.savez_compressed(folder/'masks.npz',**{key:refined[key] for key in ('original_mask','refined_mask','uncertain_mask')},valid_mask=valid[section])
        geometry=[]
        for component in refined['geometry']:
            def source_points(points):return ((np.asarray(points,float)+[x0,y0])*factor+(factor-1)/2).tolist()
            geometry.append({'polygon_source_px':source_points(component['polygon_px']),
                'holes_source_px':[source_points(p) for p in component['holes_px']]})
        payload={key:value for key,value in refined.items() if not isinstance(value,np.ndarray)}
        payload.update(id=candidate_id,original_region=region,source_hashes=field['source_hashes'],
            source_field_sha256=entry['hashes'][entry['field_json']],source_mask_sha256=entry['hashes'][relative],
            geometry_source=geometry,analysis_crop_xywh=[int(x0),int(y0),int(x1-x0),int(y1-y0)],
            source_pixel_affine=[[factor,0,float(x0*factor+(factor-1)/2)],[0,factor,float(y0*factor+(factor-1)/2)]],
            pixel_size_um=pixel.tolist(),masks_sha256=driver.digest(folder/'masks.npz',check),
            original_disposition='shortlist' if region['selected_for_review'] else 'uncertain',
            artifact_disposition=payload.get('artifact_disposition','review_only'))
        driver.watcher.save(folder/'refinement.json',payload)
        catalog[candidate_id]={'polygons_px':[p for component in geometry for p in [component['polygon_source_px'],*component['holes_source_px']]],
            'original_polygons_px':[region['polygon_source_px'],*region['holes_source_px']],
            'refinement_status':refined['status'],'artifact_flags':refined['flags'],
            'likely_artifact':refined.get('likely_artifact',False),'metrics':refined.get('metrics'),
            'source_field_sha256':entry['hashes'][entry['field_json']],
            'refinement_path':(folder/'refinement.json').relative_to(out).as_posix(),
            'refinement_sha256':driver.digest(folder/'refinement.json',check)}
        import matplotlib
        matplotlib.use('Agg')
        import matplotlib.pyplot as plt
        display=image[section]/4095;display=np.clip(display,0,1)**(1/2.2)
        fig,axes=plt.subplots(1,3,figsize=(10,3.5))
        for ax,title in zip(axes,('Raw display','Original mask','Measured refinement')):
            ax.imshow(display);ax.set_title(title);ax.axis('off')
        for ax,key,color in ((axes[1],'original_mask','cyan'),(axes[2],'refined_mask','yellow')):
            mask=refined[key]
            if mask.any() and not mask.all():ax.contour(mask,levels=[.5],colors=[color],linewidths=.6)
        fig.suptitle(candidate_id+'\n'+(', '.join(refined['flags']) or 'No artifact flag; optical identity unconfirmed'),fontsize=9)
        fig.tight_layout();fig.savefig(folder/'comparison.png',dpi=130);plt.close(fig)
        results.append({'id':candidate_id,'status':refined['status'],'flags':refined['flags'],
            'artifact_disposition':payload['artifact_disposition'],'metrics':refined.get('metrics'),
            'refinement_json':(folder/'refinement.json').relative_to(out).as_posix(),
            'refinement_sha256':driver.digest(folder/'refinement.json',check),
            'comparison':(folder/'comparison.png').relative_to(out).as_posix()})
        driver.watcher.save(out/'summary.json',{'state':'running','candidates':results})
        print(json.dumps(results[-1]),flush=True)
    driver.watcher.save(out/'summary.json',{'state':'complete','candidates':results,'originals_preserved':True})
    driver.watcher.save(out/'catalog.json',{'schema':1,'run_id':run_id,'analysis_plan_sha256':proof['source_plan_sha256'],
        'plan_sha256':driver.digest(out/'plan.json',check),'regions':catalog,
        'qualification':'Measured candidate-local contour proposals and heuristic artifact warnings. All original labels and uncertain candidates retained; likely_artifact suppression must be reversible.'})
    return out


if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('--analysis',type=Path,required=True);p.add_argument('--version',required=True)
    p.add_argument('--candidates',type=Path,default=Path(__file__).with_name('gui-api-verification.json'));p.add_argument('--ids')
    print(run(p.parse_args()))
