"""Bounded chip-level substrate consistency QC from saved independent support.

No feature-color segmentation, image correction, or hardware access occurs here.
"""
import hashlib
import json

import cv2
import numpy as np
from scipy.ndimage import distance_transform_edt

from .imaging import load_frame
from .model import AcquisitionError
from .terrace_domain import digest


MINIMUM_PIXELS=128
MINIMUM_FIELDS=5
SCALE_FLOOR=np.log([1.05,1.05,1.15])
GROSS_THRESHOLD=6.


def _matches_sha(path,expected):
    try:return bool(expected and digest(path)==expected)
    except OSError:return False


def recorded_profile(metadata,pixel_size_um):
    """Only recorded frame settings; sample/context names are not measurements."""
    resources=metadata.get('camera_resources',{})
    profile={'exposure_ms':resources.get('ff_exposure_ms'),
        'white_balance':resources.get('ff_white_balance'),
        'objective':metadata.get('objective'),'objective_position':metadata.get('objective_position'),
        'illumination_percent':metadata.get('illumination_percent'),
        'white_level':metadata.get('white_level'),'pixel_type':metadata.get('pixel_type'),
        'source':metadata.get('source'),'camera_job_template':metadata.get('camera_job_template'),
        'pixel_size_um':list(pixel_size_um)}
    encoded=json.dumps(profile,sort_keys=True,allow_nan=False,separators=(',',':'))
    missing=[k for k in ('exposure_ms','white_balance','objective','illumination_percent','white_level') if profile[k] is None]
    return hashlib.sha256(encoded.encode()).hexdigest(),profile,missing


def _summarize(rgb,white_level):
    rgb=np.asarray(rgb,np.float64)
    usable=np.all((rgb>0)&(rgb<float(white_level)),axis=1)
    values=rgb[usable]
    result={'sampled_pixels':len(rgb),'usable_pixels':len(values),'clipped_or_zero_pixels':int((~usable).sum())}
    if len(values)<MINIMUM_PIXELS:
        return {**result,'status':'insufficient_substrate_samples'}
    transformed=np.column_stack([np.log(values[:,0]/values[:,1]),np.log(values[:,2]/values[:,1]),
        np.log(values[:,1]/float(white_level))])
    return {**result,'status':'measured','median_raw_rgb':np.median(values,axis=0).tolist(),
        'log_color_brightness':np.median(transformed,axis=0).tolist(),
        'within_field_mad':(1.4826*np.median(abs(transformed-np.median(transformed,axis=0)),axis=0)).tolist()}


def _reference_statistics(records):
    values=np.asarray([r['measurement']['log_color_brightness'] for r in records])
    centre=np.median(values,axis=0)
    observed=1.4826*np.median(abs(values-centre),axis=0)
    return centre,np.maximum(observed,SCALE_FLOOR),observed


def _spread_order(records):
    """Deterministic maximin positions, beginning nearest the chip centroid."""
    if not records:return []
    xy=np.asarray([[r['position']['x'],r['position']['y']] for r in records])
    first=int(np.argmin(np.sum((xy-np.median(xy,axis=0))**2,axis=1)))
    order=[];distance=np.full(len(records),np.inf);chosen=first
    for _ in records:
        order.append(records[chosen]);distance=np.minimum(distance,np.sum((xy-xy[chosen])**2,axis=1))
        distance[chosen]=-1.;chosen=int(np.argmax(distance))
    return order


def build_substrate_reference(domain,maximum_fields=9,maximum_attempts=12,maximum_pixels=512):
    """Read at most12 raw fields, retain at most9 distributed supported samples."""
    if not 5<=maximum_fields<=12 or not maximum_fields<=maximum_attempts<=16 or not 128<=maximum_pixels<=2048:
        raise ValueError('Chip substrate sample budget is outside supported bounds.')
    folder=domain.folder;cohorts={};profiles={};unusable=[]
    for entry in domain.qc['fields']:
        if (entry.get('substrate_fraction') or 0)<.35:continue
        tile=entry['tile_id'];record=domain.manifest['completed'][tile]
        path=folder/'tiles'/(tile+'.json');expected=record.get('hashes',{}).get('tiles/'+path.name)
        if not _matches_sha(path,expected):
            unusable.append({'tile_id':tile,'reason':'source_sidecar_checksum_missing_or_mismatched'});continue
        try:
            saved=json.loads(path.read_text(encoding='utf-8'))
            profile_id,profile,missing=recorded_profile(saved['metadata'],saved['pixel_size_um'])
        except (OSError,KeyError,TypeError,ValueError) as error:
            unusable.append({'tile_id':tile,'reason':'unreadable_recorded_profile','error':str(error)});continue
        if missing:
            unusable.append({'tile_id':tile,'reason':'incomplete_recorded_optical_profile','missing':missing});continue
        profiles[profile_id]=profile
        cohorts.setdefault(profile_id,[]).append({**entry,'source_sidecar_sha256':expected})
    reference={'schema':1,'method':'bounded_distributed_saved_substrate_samples','status':'insufficient_reference_fields',
        'maximum_raw_fields_read':maximum_attempts,'maximum_reference_fields':maximum_fields,'maximum_pixels_per_field':maximum_pixels,
        'sample_chip_edge_margin_um':12.,'raw_fields_decoded':0,
        'minimum_usable_pixels':MINIMUM_PIXELS,'minimum_reference_fields':MINIMUM_FIELDS,
        'feature_support':'Existing checksummed substrate labels only, geometrically inside chip and eroded1um; never chosen by feature color.',
        'coordinate_definition':['log(R/G)','log(B/G)','log(G/white_level)'],
        'robust_scale_floor_log':SCALE_FLOOR.tolist(),'gross_mismatch_threshold':GROSS_THRESHOLD,
        'threshold_qualification':'Conservative engineering gross-mismatch screen, not a calibrated substrate or material classifier. Six robust deviations with floors of5% color-ratio and15% brightness variation.',
        'profile_field_counts':{k:len(v) for k,v in cohorts.items()},'records':[],'excluded_samples':unusable,
        'unmeasured_profile_settings':['sensor black offset','gamma/color processing not present in saved readback','spectral response'],
        'scope':'Does not alter raw pixels, source masks, parent identities, or fitted background. Existing substrate labels themselves are not independent chemical verification.'}
    if not cohorts:return reference
    profile_id=max(sorted(cohorts),key=lambda key:len(cohorts[key]));reference.update(profile_id=profile_id,recorded_profile=profiles[profile_id])
    for entry in _spread_order(cohorts[profile_id])[:maximum_attempts]:
        if len(reference['records'])>=maximum_fields:break
        tile=entry['tile_id'];record=domain.manifest['completed'][tile];hashes=record.get('hashes',{})
        image_path=folder/'tiles'/(tile+'.tiff');label_path=folder/'boundary-labels'/(tile+'.npz')
        image_sha=hashes.get('tiles/'+image_path.name);label_sha=hashes.get('boundary-labels/'+label_path.name)
        if not _matches_sha(image_path,image_sha) or not _matches_sha(label_path,label_sha):
            reference['excluded_samples'].append({'tile_id':tile,'reason':'source_TIFF_or_support_checksum_mismatch'});continue
        try:
            frame=load_frame(image_path);reference['raw_fields_decoded']+=1
            with np.load(label_path,allow_pickle=False) as saved:labels=saved['labels']
            if labels.ndim!=2:raise ValueError('Substrate labels must be a two-dimensional array.')
        except (AcquisitionError,OSError,KeyError,ValueError) as error:
            reference['excluded_samples'].append({'tile_id':tile,'reason':'unreadable_raw_or_support','error':str(error)});continue
        h,w=frame.rgb.shape[:2];lh,lw=labels.shape
        eroded=distance_transform_edt(np.pad(labels==1,1),sampling=(frame.pixel_size_um[1]*h/lh,frame.pixel_size_um[0]*w/lw))[1:-1,1:-1]>1.
        ys,xs=np.nonzero(eroded);selection=np.linspace(0,max(len(ys)-1,0),min(len(ys),maximum_pixels),dtype=int)
        pixels=np.column_stack([np.minimum(w-1,np.floor((xs[selection]+.5)*w/lw)).astype(int),
            np.minimum(h-1,np.floor((ys[selection]+.5)*h/lh)).astype(int)])
        matrix=np.asarray(domain.manifest['transform']['matrix'])
        stage=(pixels-[(w-1)/2,(h-1)/2])@matrix.T+[frame.position.x,frame.position.y]
        inside=np.asarray([cv2.pointPolygonTest(np.float32(domain.polygon_um),tuple(map(float,point)),True)>12. for point in stage],bool)
        pixels=pixels[inside];measurement=_summarize(frame.rgb[pixels[:,1],pixels[:,0]],frame.metadata['white_level'])
        sample={'tile_id':tile,'position_um':[frame.position.x,frame.position.y],'profile_id':profile_id,
            'source_image':'tiles/'+image_path.name,'source_image_sha256':image_sha,
            'source_sidecar_sha256':entry['source_sidecar_sha256'],'substrate_label_sha256':label_sha,
            'sample_pixels_xy':pixels.tolist(),'measurement':measurement}
        if measurement['status']!='measured':
            reference['excluded_samples'].append(sample);continue
        reference['records'].append(sample)
    records=reference['records']
    if len(records)<MINIMUM_FIELDS:return reference
    centre,scale,observed=_reference_statistics(records)
    xy=np.float32([r['position_um'] for r in records]);span_area=cv2.contourArea(cv2.convexHull(xy))
    chip_area=abs(cv2.contourArea(np.float32(domain.polygon_um)))
    fraction=float(span_area/max(chip_area,1.))
    reference.update(status='ready' if fraction>=.1 else 'limited_spatial_coverage',
        reference_log_color_brightness=centre.tolist(),robust_scale_log=scale.tolist(),observed_between_field_mad=observed.tolist(),
        sampled_position_hull_fraction=fraction,reference_id=hashlib.sha256(json.dumps(records,sort_keys=True).encode()).hexdigest())
    reference['gross_reference_outliers']=[]
    for record in records:
        peers=[other for other in records if other['tile_id']!=record['tile_id']]
        if len(peers)<MINIMUM_FIELDS:continue
        c,s,_=_reference_statistics(peers)
        score=float(np.max(abs(np.asarray(record['measurement']['log_color_brightness'])-c)/s))
        record['leave_one_field_out_score']=score
        if score>GROSS_THRESHOLD:reference['gross_reference_outliers'].append({'tile_id':record['tile_id'],'score':score})
    return reference


def check_substrate_consistency(reference,frame,support,tile_id,maximum_pixels=512):
    """QC only. A reference field is excluded from its own comparison."""
    profile_id,profile,missing=recorded_profile(frame.metadata,frame.pixel_size_um)
    result={'reference_id':reference.get('reference_id'),'profile_id':profile_id,'recorded_profile':profile,
        'status':'unavailable','feature_masks_changed':False,'threshold_qualification':reference.get('threshold_qualification')}
    if missing:return {**result,'status':'incomplete_recorded_optical_profile','missing_fields':missing}
    if profile_id!=reference.get('profile_id'):return {**result,'status':'reference_unavailable_for_optical_profile'}
    if reference.get('status') not in ('ready','limited_spatial_coverage'):
        return {**result,'status':'insufficient_chip_reference','reference_status':reference.get('status')}
    # Grid sampling remains independent of raw feature color, with a hard bound.
    step=max(1,int(np.sqrt(support.size/(maximum_pixels*4))))
    ys,xs=np.nonzero(support[::step,::step]);selection=np.linspace(0,max(len(xs)-1,0),min(maximum_pixels,len(xs)),dtype=int)
    xs=xs[selection]*step;ys=ys[selection]*step
    measured=_summarize(frame.rgb[ys,xs],frame.metadata['white_level'])
    result.update(measurement=measured,sample_pixels_xy=np.column_stack([xs,ys]).tolist())
    if measured['status']!='measured':return {**result,'status':'insufficient_substrate_samples'}
    records=[r for r in reference['records'] if r['tile_id']!=tile_id]
    result.update(reference_sample_ids=[r['tile_id'] for r in records],leave_one_field_out=len(records)!=len(reference['records']))
    if len(records)<MINIMUM_FIELDS:return {**result,'status':'insufficient_independent_reference_fields'}
    centre,scale,observed=_reference_statistics(records);delta=np.asarray(measured['log_color_brightness'])-centre
    component_scores=abs(delta)/scale;score=float(max(component_scores))
    result.update(status='gross_substrate_mismatch' if score>GROSS_THRESHOLD else 'consistent_with_chip_reference',
        score=score,component_scores=component_scores.tolist(),threshold=GROSS_THRESHOLD,
        reference_log_color_brightness=centre.tolist(),robust_scale_log=scale.tolist(),
        color_and_brightness_ratio_to_reference=np.exp(delta).tolist(),reference_spatial_status=reference['status'])
    return result
