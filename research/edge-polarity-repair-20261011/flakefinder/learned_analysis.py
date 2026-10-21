"""Measured physical candidates from MaskTerial masks, without shape-rule vetoes.

The optional heavy model runs in its isolated environment. This adapter works
with saved predictions and imports neither torch nor transformers. A model
confidence is an instance proposal score, never a material or layer probability.
"""
from dataclasses import asdict
from pathlib import Path
import hashlib
import json
import subprocess
import time

import cv2
import numpy as np

from .colors import mote2_thin_screen, regional_contrast
from .imaging import load_frame
from .model import AcquisitionError, atomic_json
from .scan import deduplicate, export_csv

WORKSPACE=Path(__file__).resolve().parents[1]
LEARNED_PYTHON=WORKSPACE/'.venv-maskterial/Scripts/python.exe'
LEARNED_SCRIPT=WORKSPACE/'scripts/infer_learned_masks.py'
DEFAULT_WEIGHTS=WORKSPACE/'external/maskterial-hf/synthetic/model_final.pth'


def learned_runtime_available():
    return all(path.is_file() for path in (LEARNED_PYTHON,LEARNED_SCRIPT,DEFAULT_WEIGHTS))


def file_sha256(path):
    digest=hashlib.sha256()
    with Path(path).open('rb') as stream:
        for block in iter(lambda:stream.read(1024*1024),b''):digest.update(block)
    return digest.hexdigest()


def _run_inference(source,output,settings,callback,check_cancel):
    if not learned_runtime_available():
        raise AcquisitionError('Learned runtime is not installed. Install the documented optional MaskTerial runtime, supply saved predictions, or explicitly select backend=classical.')
    command=[str(LEARNED_PYTHON),str(LEARNED_SCRIPT),'--input',str(source),'--output',str(output),
             '--display-transform',str(settings.get('display_transform','gamma')),
             '--gamma',str(settings.get('gamma',.6)),'--max-side',str(settings.get('max_side',1536)),
             '--score-threshold',str(settings.get('score_threshold',.3)),
             '--threads',str(settings.get('threads',3)),'--resume']
    # The measured microscope stream is 12 bit. Other instruments should supply
    # their ADC white level explicitly or set None to use camera metadata.
    white=settings.get('white_level',4095)
    if white is not None:command.extend(['--white-level',str(white)])
    if settings.get('weights'):command.extend(['--weights',str(Path(settings['weights']).resolve())])
    output.mkdir(parents=True,exist_ok=True)
    (output/'STOP').unlink(missing_ok=True)
    with (output/'inference.log').open('a',encoding='utf-8') as log:
        process=subprocess.Popen(command,cwd=WORKSPACE,stdout=log,stderr=subprocess.STDOUT,
                                 creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0))
        try:
            last_progress=0
            while process.poll() is None:
                if check_cancel:check_cancel()
                if callback and time.monotonic()-last_progress>2:
                    callback('Learned instance segmentation',{'source':str(source)})
                    last_progress=time.monotonic()
                time.sleep(.2)
        except BaseException:
            # Let the worker checkpoint at a field boundary. A Windows venv
            # launcher can own a child interpreter; killing only the launcher
            # can otherwise leave a large orphaned model process behind.
            (output/'STOP').write_text('Analysis cancelled\n',encoding='utf-8')
            try:process.wait(timeout=40)
            except subprocess.TimeoutExpired:
                if __import__('os').name=='nt':
                    subprocess.run(['taskkill','/PID',str(process.pid),'/T','/F'],capture_output=True,
                                   creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0),check=False)
                else:process.kill()
                process.wait(timeout=5)
            raise
        if process.returncode:
            raise AcquisitionError(f'Learned inference failed; see {output / "inference.log"}. No fallback detector was substituted.')
    return output/'predictions.json'


def prediction_index(tiles,out,settings,callback=None,check_cancel=None):
    supplied=settings.get('predictions')
    paths=[Path(p).resolve() for p in (supplied if isinstance(supplied,list) else [supplied])] if supplied else []
    if not paths:
        for index,source in enumerate(sorted({item['path'].parent for item in tiles})):
            paths.append(_run_inference(source,out/'learned-predictions'/f'part-{index:03d}',settings,callback,check_cancel))
    records={};runs=[]
    for path in paths:
        data=json.loads(path.read_text(encoding='utf-8-sig'))
        if data.get('schema')!=1 or data.get('backend')!='maskterial_hf_cpu':raise AcquisitionError(f'Unsupported learned prediction format: {path}')
        if data.get('state')!='complete':raise AcquisitionError(f'Learned inference is not complete: {path}')
        runs.append({key:data.get(key) for key in ('backend','fingerprint','model','parameters','postprocessor','execution_changes','validation_status')})
        runs[-1]['predictions_file']=str(path)
        for record in data['images']:
            source=Path(record['source_image']).resolve()
            if source in records:raise AcquisitionError(f'Duplicate learned prediction for {source}')
            records[source]=(record,path.parent,len(runs)-1)
    missing=[str(item['path']) for item in tiles if item['path'].resolve() not in records]
    if missing:raise AcquisitionError(f'Learned predictions omit {len(missing)} completed tile(s): {missing[0]}')
    return records,runs


def _kernel(radius):
    return cv2.getStructuringElement(cv2.MORPH_ELLIPSE,(2*radius+1,2*radius+1))


def rows_from_masks(frame,masks,scores,query_indices,options,transform=None,valid=None):
    """Measure connected learned instances in original source coordinates.

    Minimum area and useful width are physical user requirements. All other
    descriptors, optical contrast, and focus limits remain available for review;
    they do not silently veto a learned prediction. Exact pixels determine area.
    """
    masks=np.asarray(masks,bool);scores=np.asarray(scores,float)
    height,width=frame.rgb.shape[:2]
    if masks.ndim!=3 or masks.shape[1:]!=(height,width) or len(masks)!=len(scores) or len(scores)!=len(query_indices):
        raise AcquisitionError('Learned masks do not match the original source image or scores.')
    if not np.isfinite(scores).all():raise AcquisitionError('Nonfinite learned confidence.')
    if valid is None:valid=np.ones((height,width),bool)
    valid=np.asarray(valid,bool)
    union=np.any(masks,axis=0) if len(masks) else np.zeros((height,width),bool)
    pxarea=float(np.prod(frame.pixel_size_um));scale=min(frame.pixel_size_um)
    rows=[]
    for mask_index,(mask,score,query) in enumerate(zip(masks,scores,query_indices)):
        number,components,stats,centres=cv2.connectedComponentsWithStats(np.uint8(mask),8)
        for component in range(1,number):
            x,y,w,h,count=map(int,stats[component]);area=count*pxarea
            # Keep sub-threshold masks in the original NPZ, not as gallery junk.
            if area<options.min_area_um2:continue
            radius=max(4,int(np.ceil(25/scale)))
            x0=max(0,x-radius);y0=max(0,y-radius);x1=min(width,x+w+radius);y1=min(height,y+h+radius)
            local=np.uint8(components[y0:y1,x0:x1]==component)
            contours,hierarchy=cv2.findContours(local,cv2.RETR_CCOMP,cv2.CHAIN_APPROX_SIMPLE)
            outer_ids=[i for i,hier in enumerate(hierarchy[0]) if hier[3]<0]
            outer=max(outer_ids,key=lambda i:cv2.contourArea(contours[i]));contour=contours[outer]
            offset=np.array([x0,y0]);points=contour[:,0,:]+offset
            polygon=(cv2.approxPolyDP(contour,.5,True)[:,0,:]+offset).tolist()
            holes=[(cv2.approxPolyDP(c,.5,True)[:,0,:]+offset).tolist() for i,c in enumerate(contours) if hierarchy[0,i,3]==outer]
            physical=np.float32(points)*np.float32(frame.pixel_size_um)
            useful_width=float(min(cv2.minAreaRect(physical)[1]))
            border=bool(x==0 or y==0 or x+w==width or y+h==height)
            inside=cv2.erode(local,_kernel(max(1,int(round(.5/scale)))))>0
            if np.count_nonzero(inside)<6:inside=local>0
            exclusion=union[y0:y1,x0:x1]
            local_valid=valid[y0:y1,x0:x1]
            inner=cv2.dilate(local,_kernel(max(1,int(round(2/scale)))))>0
            substrate=None;annulus_range=None
            for distance in (8,15,25):
                outer_ring=cv2.dilate(local,_kernel(max(1,int(round(distance/scale)))))>0
                substrate=outer_ring&~inner&~exclusion&local_valid
                if np.count_nonzero(substrate)>=30:
                    annulus_range=[2,distance];break
            measurement=regional_contrast(frame.rgb[y0:y1,x0:x1],inside,substrate,frame.pixel_size_um)
            if measurement:
                measurement['background_annulus_um']=annulus_range
                measurement['background_pixels']=int(np.count_nonzero(substrate))
                measurement['object_interior_pixels']=int(np.count_nonzero(inside))
            contrast=measurement['contrast_rgb'] if measurement else [0.,0.,0.]
            values=frame.rgb[y0:y1,x0:x1][inside].astype(float)
            rgb_median=np.median(values,axis=0)
            uniformity=float(np.linalg.norm(1.4826*np.median(np.abs(values-rgb_median),axis=0)/np.maximum(rgb_median,1)))
            contour_area=max(cv2.contourArea(contour),1)
            solidity=float(contour_area/max(cv2.contourArea(cv2.convexHull(contour)),1))
            reasons=[]
            if area>options.max_area_um2:reasons.append('above_maximum_area')
            if useful_width<options.min_width_um:reasons.append('below_useful_width')
            if not np.any(local.astype(bool)&local_valid):reasons.append('outside_chip_region')
            centre=centres[component].tolist()
            row={'centroid_px':centre,'polygon_px':polygon,'holes_px':holes,'bbox_px':[x,y,w,h],
                 'area_um2':float(area),'usable_width_um':useful_width,'useful_area_um2':float(area),
                 'contrast_rgb':contrast,'regional_measurement':measurement,'contrast_measurement_available':measurement is not None,
                 'contrast_norm':float(np.linalg.norm(contrast)) if measurement else None,
                 'uniformity':uniformity,'solidity':solidity,'touches_border':border,
                 'color_group':'learned-instance','color_posterior':None,'quality_score':float(score),
                 'quality_score_meaning':'Learned instance score; not a material or thickness probability.',
                 'learned_score':float(score),'learned_mask_index':mask_index,'learned_query_index':int(query),
                 'learned_component_index':component,'mask_area_pixels':count,
                 'extraction_method':'MaskTerial learned instance mask; connected component; no shape-rule veto',
                 'terraces':[],'filter_reasons':reasons,'disposition':'rejected' if reasons else 'candidate',
                 'label':'unclassified','layer_number':None,'distance':None,'stage_xy_um':None,'stage_polygon_um':None}
            if transform:
                row['stage_xy_um']=transform.stage_xy(centre,frame).tolist()
                row['stage_polygon_um']=[transform.stage_xy(p,frame).tolist() for p in polygon]
            rows.append(row)
    return rows


def analyze_learned_scan(folder,out,options,settings,callback=None,model=None,check_cancel=None):
    from .analysis import tile_inputs,tile_region
    from .review import build_review
    tiles=tile_inputs(folder)
    records,runs=prediction_index(tiles,out,settings,callback,check_cancel)
    signature=hashlib.sha256(json.dumps({'algorithm':'learned-mask-measurements-v1','runs':runs,'options':asdict(options)},sort_keys=True).encode())
    rows=[];tile_data=[];cloud=[]
    for index,item in enumerate(tiles):
        if check_cancel:check_cancel()
        prediction,root,run=records[item['path'].resolve()]
        if file_sha256(item['path'])!=prediction['source_sha256']:raise AcquisitionError(f'Raw source changed after learned inference: {item["path"]}')
        metadata=item['path'].with_suffix('.json')
        if prediction.get('metadata_sha256')!=(file_sha256(metadata) if metadata.exists() else None):raise AcquisitionError(f'Camera metadata changed after learned inference: {metadata}')
        mask_path=(root/prediction['masks_file']).resolve()
        if not mask_path.is_relative_to(root):raise AcquisitionError('Learned mask path escapes prediction directory.')
        if file_sha256(mask_path)!=prediction['masks_sha256']:raise AcquisitionError(f'Learned mask file changed: {mask_path}')
        frame=load_frame(item['path']);item['transform'].validate_frame(frame)
        if list(frame.rgb.shape[:2])!=prediction['original_shape_hw']:raise AcquisitionError('Learned source dimensions differ from recorded camera image.')
        with np.load(mask_path,allow_pickle=False) as saved:
            found=rows_from_masks(frame,saved['masks'],saved['scores'],saved['query_indices'],options,item['transform'],tile_region(item,frame))
        digest=hashlib.sha256(frame.rgb.tobytes()).hexdigest();signature.update((item['key']+digest).encode())
        h,w=frame.rgb.shape[:2];relative=item['path'].relative_to(folder).as_posix()
        record={'key':item['key'],'tile_id':item['tile_id'],'chip_id':item['chip_id'],'source':relative,
                'position':asdict(frame.position),'shape':[h,w],'pixel_size_um':list(frame.pixel_size_um),'sha256':digest,
                'footprint_um':[item['transform'].stage_xy(p,frame).tolist() for p in [[0,0],[w-1,0],[w-1,h-1],[0,h-1]]],
                'acquired_at':frame.acquired_at,'focus_status':prediction.get('focus_status'),
                'focus_limited':prediction.get('focus_limited',True),'learned_queries':len(prediction['candidates']),
                'regions':len(found),'candidates':sum(r['disposition']=='candidate' for r in found),
                'inference_seconds':prediction.get('inference_seconds'),'learned_run':run}
        material=str(item['manifest'].get('context',{}).get('material','')).lower().replace('₂','2')
        for row in found:
            identity=f'{item["key"]}|{runs[run]["fingerprint"]}|{row["learned_query_index"]}|{row["learned_component_index"]}'
            row.update(id='f-'+hashlib.sha256(identity.encode()).hexdigest()[:14],tile_id=item['tile_id'],
                       tile_key=item['key'],chip_id=item['chip_id'],source_image=relative,source_sha256=digest,
                       learned_masks_file=str(mask_path),learned_masks_sha256=prediction['masks_sha256'],learned_run=run,
                       focus_limited=record['focus_limited'],focus_status=record['focus_status'])
            if row['contrast_measurement_available']:
                cloud.append({'rgb':row['contrast_rgb'],'tile':item['key'],'population':'learned-instance'})
                if model:row['label'],row['distance']=model.classify(row['contrast_rgb'],item['manifest'].get('context',{}))
            if material=='mote2':
                row['thin_screen']=mote2_thin_screen(row['contrast_rgb']) if row['contrast_measurement_available'] else {'score':0.,'status':'unavailable','layer_number':None}
                row['thin_screen_area_um2']=row['area_um2'] if row['thin_screen']['score'] else 0.
            if item['manifest'].get('revisit_parent_id'):
                row['parent_candidate_id']=item['manifest']['revisit_parent_id']
                point=item['manifest']['completed'][item['tile_id']].get('registration',{}).get('candidate_px')
                if point is not None:
                    contained=cv2.pointPolygonTest(np.float32(row['polygon_px']),tuple(point),False)>=0
                    in_hole=any(cv2.pointPolygonTest(np.float32(hole),tuple(point),False)>=0 for hole in row['holes_px'])
                    row['contains_revisited_candidate']=bool(contained and not in_hole)
        rows.extend(found);tile_data.append(record)
        if callback:callback('Measuring learned masks',{'index':index+1,'total':len(tiles)})
    accepted=deduplicate([r for r in rows if r['disposition']=='candidate'])
    rejected=[r for r in rows if r['disposition']=='rejected']
    for row in accepted:
        row['priority']='thin_screen' if row.get('thin_screen',{}).get('status')=='thin_screen' else 'review_required'
    for row in rejected:row['priority']='rejected'
    accepted.sort(key=lambda row:(-row.get('thin_screen',{}).get('score',0),-row['learned_score'],-row['area_um2']))
    all_rows=accepted+rejected
    groups={'schema':1,'method':'learned instance masks; local substrate contrast','groups':[]}
    summary={'schema':2,'backend':'maskterial_hf_cpu','dataset_id':signature.hexdigest()[:20],'source_folder':str(folder),
             'created_at':time.time(),'options':asdict(options),'learned_runs':runs,'tiles':tile_data,
             'color_model':groups,'color_cloud':cloud,'raw_regions':len(rows),'candidate_count':len(accepted),
             'rejected_count':len(rejected),'deduplicated_count':len(rows)-len(all_rows),
             'distinct_images':len({r['sha256'] for r in tile_data}),'focus_limited_tiles':sum(t['focus_limited'] for t in tile_data),
             'thin_screen_count':sum(r['priority']=='thin_screen' for r in accepted),
             'validation_status':'unvalidated_target_domain',
             'warning':'Learned instance proposals require review on this microscope and material. Optical screening does not establish MoTe2 identity or layer number. Defocused fields can conceal thin flakes; no detections is not evidence of absence.'}
    atomic_json(out/'analysis.json',summary)
    atomic_json(out/'flakes.json',{'flakes':all_rows,'analysis_schema':2,'dataset_id':summary['dataset_id'],'source_folder':str(folder)})
    atomic_json(out/'color-model.json',groups);export_csv(out/'flakes.csv',all_rows);build_review(out)
    return summary
