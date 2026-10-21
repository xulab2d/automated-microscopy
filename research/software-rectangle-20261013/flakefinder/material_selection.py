"""Profile-bound RGB reference selection on immutable, previously saved masks.

Reference resemblance is distinct from chemical identity and thickness. The
model supports multiple positive optical branches and explicit substrate/debris
distributions. It never changes segmentation or substitutes a mean mixed color
for the pixel-supported area of an object.
"""
from pathlib import Path
import hashlib
import json
import shutil

import cv2
import numpy as np
from scipy.ndimage import find_objects

from .colors import ColorGroups
from .imaging import background_surface,correct_flat_field,load_frame
from .model import AcquisitionError,atomic_json


CONTEXT_KEYS=('sample_id','material','substrate','oxide_nm','oxide_process','objective','camera',
              'exposure_ms','illumination','illumination_percent','white_balance','white_level','color_profile_kind')


def optical_context(manifest):
    return {'context':{key:manifest.get('context',{}).get(key) for key in CONTEXT_KEYS},
            'acquisition_settings':manifest.get('acquisition_settings')}


def normalized_tile(item,edge_margin_um=12):
    # Identical fitted-substrate normalization to prepare_contrast, without
    # repeating segmentation, noise estimation or proposal generation.
    from .analysis import tile_region
    frame=load_frame(item['path']);rgb=correct_flat_field(frame.rgb)
    valid=tile_region(item,frame)
    if valid is None:valid=np.ones(rgb.shape[:2],bool)
    radius=int(np.ceil(edge_margin_um/min(frame.pixel_size_um)))
    if radius:valid=cv2.erode(np.uint8(valid),np.ones((2*radius+1,2*radius+1),np.uint8)).astype(bool)
    background=background_surface(rgb,valid)
    return frame,(background-rgb)/np.maximum(background,1),valid


def _core(mask,pixel_size_um,erosion_um):
    padded=cv2.copyMakeBorder(np.uint8(mask),1,1,1,1,cv2.BORDER_CONSTANT,value=0)
    distance=cv2.distanceTransform(padded,cv2.DIST_L2,5)[1:-1,1:-1]
    core=distance>erosion_um/min(pixel_size_um)
    return core if core.sum()>=12 else np.asarray(mask,bool)


def _robust_component(values,name,role):
    values=np.asarray(values,float);centre=np.median(values,axis=0)
    # Trim isolated damaged/saturated-edge outliers; preserve the full measured
    # covariance and a numerical 0.5% contrast floor across all three channels.
    scale=np.maximum(1.4826*np.median(abs(values-centre),axis=0),.005)
    core=values[np.sum(((values-centre)/scale)**2,axis=1)<=np.percentile(np.sum(((values-centre)/scale)**2,axis=1),95)]
    covariance=np.cov(core.T)+np.eye(3)*.005**2
    return {'name':name,'role':role,'mean':np.median(core,axis=0).tolist(),
            'covariance':covariance.tolist(),'sample_pixels':len(values),'fit_pixels':len(core)}


def fit_reference_profile(source_analysis,bulk_analysis,annotations,negative_ids):
    """Fit the compact local reference catalog from reviewed optical examples."""
    from .analysis import tile_inputs
    source=Path(source_analysis).resolve();summary=json.loads((source/'analysis.json').read_text(encoding='utf-8'))
    items={item['key']:item for item in tile_inputs(summary['source_folder'])}
    rows=json.loads((source/'flakes.json').read_text(encoding='utf-8'))['flakes']
    bulk=Path(bulk_analysis).resolve();bulk_rows=json.loads((bulk/'flakes.json').read_text(encoding='utf-8'))['flakes']
    annotation_path=Path(annotations).resolve();manual=json.loads(annotation_path.read_text(encoding='utf-8'))['annotations']
    chosen=[]
    for row in bulk_rows:
        if row.get('disposition')=='candidate':chosen.append(('bright_bulk_reference','positive',row,bulk))
    for ann in manual:
        if ann['id'] in ('P1','P2'):
            key=next(key for key,item in items.items() if item['path'].resolve()==Path(ann['source_image']).resolve())
            chosen.append(('cyan_'+ann['id'],'positive',{'tile_key':key,'polygon_px':ann['polygon_px'],'id':ann['id']},None))
    for row in rows:
        if row['id'] in negative_ids:chosen.append(('reviewed_dust_haze','negative',row,source))
    samples={};references=[];substrate=[];contexts=[];cache={}
    for name,role,row,folder in chosen:
        key=row['tile_key'];item=items[key]
        if key not in cache:
            if len(cache)>=2:cache.pop(next(iter(cache)))
            cache[key]=normalized_tile(item,summary['options'].get('edge_margin_um',12))
        frame,contrast,valid=cache[key];contexts.append(optical_context(item['manifest']))
        validate_frame_profile(frame,{'optical_context':optical_context(item['manifest'])})
        if folder is None:
            mask=np.zeros(valid.shape,np.uint8);cv2.fillPoly(mask,[np.int32(row['polygon_px'])],1);mask=mask.astype(bool)
        else:
            tile=next(t for t in json.loads((folder/'analysis.json').read_text(encoding='utf-8'))['tiles'] if t['key']==key)
            labels=cv2.imread(str(folder/tile['instance_mask']),cv2.IMREAD_UNCHANGED)
            mask=labels==row['instance_label']
        core=_core(mask,frame.pixel_size_um,.5)&valid
        values=contrast[core];samples.setdefault(name,[]).append(values[::max(1,len(values)//4000)])
        inner=cv2.dilate(np.uint8(mask),cv2.getStructuringElement(cv2.MORPH_ELLIPSE,(25,25)))>0
        outer=cv2.dilate(np.uint8(mask),cv2.getStructuringElement(cv2.MORPH_ELLIPSE,(65,65)))>0
        annulus=outer&~inner&valid;bg_values=contrast[annulus]
        if len(bg_values):
            centre=np.median(bg_values,axis=0);norm=np.linalg.norm(bg_values-centre,axis=1)
            quiet=bg_values[norm<=np.percentile(norm,50)];substrate.append(quiet[::max(1,len(quiet)//1000)])
        references.append({'id':row['id'],'tile_key':key,'branch':name,'role':role,
            'source_image':str(item['path']),'source_sha256':hashlib.sha256(item['path'].read_bytes()).hexdigest(),
            'interior_sample_pixels':len(values),'interior_median_rgb':np.median(values,axis=0).tolist(),
            'interior_covariance':np.cov(values.T).tolist(),
            'annulus_median_rgb':np.median(bg_values,axis=0).tolist() if len(bg_values) else None,
            'qualification':'User-selected optical bulk population' if name=='bright_bulk_reference' else
                            'Independent morphology-selected platelet; material/layer unconfirmed' if role=='positive' else
                            'Reviewed false thin proposal containing dust/haze; constituents not individually labeled'})
        if folder is None:references[-1]['polygon_px']=row['polygon_px']
        else:
            references[-1].update(mask_source_analysis=str(folder),instance_label=row['instance_label'],
                mask_file=tile['instance_mask'],mask_sha256=hashlib.sha256((folder/tile['instance_mask']).read_bytes()).hexdigest())
    if len({json.dumps(c,sort_keys=True) for c in contexts})!=1:raise AcquisitionError('Reference optical contexts differ.')
    components=[]
    for name,values in samples.items():
        joined=np.concatenate(values)
        if name=='reviewed_dust_haze':
            # A mixture preserves the distinct haze and dark-granule populations.
            groups=ColorGroups.fit(joined,max_components=3)
            for i,g in enumerate(groups.groups):components.append({'name':f'dust_haze_{i+1}','role':'negative',
                'mean':g['mean'],'covariance':g['covariance'],'sample_pixels':len(joined)})
        else:components.append(_robust_component(joined,name,'positive'))
    components.append(_robust_component(np.concatenate(substrate),'substrate','background'))
    profile={'schema':1,'kind':'local_multibranch_optical_reference','optical_context':contexts[0],
        'normalization':'Stored camera RGB; same robust fitted quadratic substrate as saved analysis; no display gamma. Camera processing and radiometric linearity require separate verification.',
        'components':components,'references':references,
        'parameters':{'interior_erosion_um':.5,'max_mahalanobis_squared':11.345,
            'likelihood_ratio':3.,'minimum_interior_occupancy':.6,'maximum_positive_occupancy_for_exclusion':.1,
            'minimum_debris_occupancy_for_exclusion':.6,'catalog_supported_area_um2':100.,'source_min_area_um2':.25},
        'parameter_interpretation':'99% Gaussian reference ellipsoid in 3D; 3:1 likelihood ratio and occupancy thresholds are explicit engineering selection settings, not calibrated probabilities.',
        'qualification':'Optical reference resemblance, not chemical identity or layer calibration. Positive references are bulk/cyan branches; unmatched faint colors remain unknown. Debris colors may overlap unknown thin material.',
        'training_notes':'Compact reviewed local examples only. Reference covariance is descriptive; source pixels are spatially correlated.'}
    profile['model_id']='rgb-'+hashlib.sha256(json.dumps(profile,sort_keys=True).encode()).hexdigest()[:16]
    return profile


def classify_pixels(values,profile):
    values=np.asarray(values,np.float32).reshape(-1,3)
    log_scores=[];distances=[]
    for component in profile['components']:
        covariance=np.asarray(component['covariance']);delta=values-component['mean']
        distance=np.einsum('ni,ij,nj->n',delta,np.linalg.inv(covariance),delta,optimize=False)
        log_scores.append(-.5*(distance+np.linalg.slogdet(covariance)[1]));distances.append(distance)
    log_scores=np.asarray(log_scores).T;distances=np.asarray(distances).T
    roles=np.array([c['role'] for c in profile['components']]);positive=roles=='positive';negative=roles=='negative'
    p=np.where(positive,log_scores,-np.inf).argmax(axis=1);n=np.where(negative,log_scores,-np.inf).argmax(axis=1)
    ar=np.arange(len(values));margin=np.log(profile['parameters']['likelihood_ratio']);limit=profile['parameters']['max_mahalanobis_squared']
    supported=(distances[ar,p]<=limit)&(log_scores[ar,p]>=np.max(log_scores[:,~positive],axis=1)+margin)
    debris=(distances[ar,n]<=limit)&(log_scores[ar,n]>=np.max(log_scores[:,~negative],axis=1)+margin)
    return supported,debris,p,distances[ar,p]


def validate_frame_profile(frame,profile):
    expected=profile['optical_context'].get('acquisition_settings') or {}
    for key in ('camera_job_template','camera_resources','objective_position','illumination_percent'):
        if key in expected and frame.metadata.get(key)!=expected[key]:
            raise AcquisitionError(f'Material profile frame metadata mismatch: {key}.')
    white=profile['optical_context']['context'].get('white_level')
    if white is not None and frame.metadata.get('white_level')!=white:
        raise AcquisitionError('Material profile frame white level mismatch.')


def classify_frame_regions(frame,contrast,labels,rows,profile,valid=None):
    """Return selections keyed by row id, reusing already normalized pixels.

    The caller must additionally compare optical_context(manifest) with the
    profile context. Raw-frame acquisition metadata are checked here. Rows and
    masks are never mutated; all region ids must address the supplied label map.
    """
    validate_frame_profile(frame,profile)
    if valid is None:valid=np.ones(labels.shape,bool)
    if contrast.shape[:2]!=labels.shape or valid.shape!=labels.shape:
        raise ValueError('Material selection image/mask dimensions differ.')
    parameters=profile['parameters'];selections={}
    observed=(labels>0)&valid;support=np.zeros(labels.shape,bool);debris=np.zeros(labels.shape,bool);branch=np.zeros(labels.shape,np.uint8)
    support[observed],debris[observed],branch[observed],_=classify_pixels(contrast[observed],profile)
    boxes=find_objects(labels);pixel_area=float(np.prod(frame.pixel_size_um))
    for row in rows:
        bounds=boxes[row['instance_label']-1]
        if bounds is None:continue
        mask=labels[bounds]==row['instance_label'];core=_core(mask,frame.pixel_size_um,parameters['interior_erosion_um'])
        positive=mask&support[bounds];negative=mask&debris[bounds]
        occupancy=float(np.count_nonzero(support[bounds]&core)/core.sum())
        debris_occupancy=float(np.count_nonzero(debris[bounds]&core)/core.sum())
        _,_,stats,_=cv2.connectedComponentsWithStats(np.uint8(positive),8)
        largest=float(stats[1:,cv2.CC_STAT_AREA].max()*pixel_area) if len(stats)>1 else 0.
        supported_area=float(positive.sum()*pixel_area)
        if occupancy>=parameters['minimum_interior_occupancy'] and supported_area>=parameters['source_min_area_um2']:
            status='material_like';reason='Most interior pixels match the positive optical reference branches.'
        elif occupancy<=parameters['maximum_positive_occupancy_for_exclusion'] and debris_occupancy>=parameters['minimum_debris_occupancy_for_exclusion']:
            status='excluded';reason='Excluded from the reference-selected view: interior resembles reviewed dust/haze colors with negligible positive-reference support. Chemical identity remains unconfirmed.'
        else:status='uncertain';reason='Mixed or out-of-reference colors; faint material is not required to match bulk references.'
        counts=np.bincount(branch[bounds][positive],minlength=len(profile['components']))
        class_name=profile['components'][int(counts.argmax())]['name'] if positive.any() else 'unmatched'
        median=np.median(contrast[bounds][core],axis=0)
        faint_direction=bool(median[0]>0 and median[1]>0)
        selections[row['id']]={'status':status,'class_name':class_name,'reason':reason,'model_id':profile['model_id'],
            'supported_area_um2':supported_area,'largest_supported_area_um2':largest,'occupancy':occupancy,
            'debris_occupancy':debris_occupancy,'supported_fraction_of_mask':float(positive.sum()/mask.sum()),
            'catalog_selected':bool(status=='material_like' and largest>=parameters['catalog_supported_area_um2']),
            'layer_number':None,'chemical_identity_confirmed':False,'mask_modified':False,
            'interior_median_contrast_rgb':median.tolist(),
            'faint_direction_compatible':faint_direction,
            'faint_interpretation':'Positive red/green direction alone is not evidence of thin MoTe2; blue sign and numerical layer contrast are not constrained.',
            'reference_branch_areas_um2':{c['name']:float(counts[i]*pixel_area) for i,c in enumerate(profile['components']) if counts[i]},
            'screening_role':'known_optical_branch' if status=='material_like' else 'reviewed_debris_like' if status=='excluded' else 'faint_unknown' if faint_direction else 'unknown_color_review'}
    return selections


def classify_saved_analysis(source,output,profile):
    """Write selection-annotated JSON only; keep all original instance masks.

    No segmentation or JPEG regeneration is performed. The caller may reuse the
    original review assets and refresh review data after this function returns.
    """
    from .analysis import tile_inputs
    source=Path(source).resolve();output=Path(output).resolve()
    if source==output:raise ValueError('Material selection must use a separate output folder.')
    if isinstance(profile,(str,Path)):profile=json.loads(Path(profile).read_text(encoding='utf-8'))
    analysis=json.loads((source/'analysis.json').read_text(encoding='utf-8'));payload=json.loads((source/'flakes.json').read_text(encoding='utf-8'))
    observations=json.loads((source/'observations.json').read_text(encoding='utf-8'));items={i['key']:i for i in tile_inputs(analysis['source_folder'])}
    rows_by_tile={};selections={};parameters=profile['parameters']
    for row in observations['observations']:rows_by_tile.setdefault(row['tile_key'],[]).append(row)
    output.mkdir(parents=True,exist_ok=True)
    for tile in analysis['tiles']:
        item=items[tile['key']]
        if optical_context(item['manifest'])!=profile['optical_context']:raise AcquisitionError('Material reference profile does not match this optical/sample context.')
        frame,contrast,valid=normalized_tile(item,analysis['options'].get('edge_margin_um',12))
        labels=cv2.imread(str(source/tile['instance_mask']),cv2.IMREAD_UNCHANGED)
        selections.update(classify_frame_regions(frame,contrast,labels,rows_by_tile.get(tile['key'],[]),profile,valid))
        for key in ('instance_mask','color_mask','contrast_rg_file','global_color_model_file'):
            if tile.get(key):
                path=source/tile[key]
                if path.is_file():shutil.copy2(path,output/path.name);tile[key]=path.name
    for row in observations['observations']:row['material_selection']=selections.get(row['id'],{'status':'uncertain','reason':'No matching source mask.','model_id':profile['model_id']})
    for row in payload['flakes']:row['material_selection']=selections.get(row['id'],{'status':'uncertain','reason':'No matching source mask.','model_id':profile['model_id']})
    catalog=[r for r in payload['flakes'] if r['disposition']=='candidate']
    analysis['material_selection']={'model_id':profile['model_id'],'profile':'material-profile.json','source_analysis':str(source),
        'catalog_selected_count':sum(r['material_selection'].get('catalog_selected',False) for r in catalog),
        'catalog_status_counts':{status:sum(r['material_selection']['status']==status for r in catalog) for status in ('material_like','uncertain','excluded')},
        'mask_policy':'Original boundaries and exact instance masks unchanged; supported area is an additional color-evidence measurement.',
        'qualification':profile['qualification']}
    atomic_json(output/'material-profile.json',profile);atomic_json(output/'analysis.json',analysis)
    atomic_json(output/'flakes.json',payload);atomic_json(output/'observations.json',observations)
    return analysis['material_selection']
