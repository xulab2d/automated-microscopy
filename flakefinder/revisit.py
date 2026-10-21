"""Audited candidate revisits with independent image registration and re-analysis."""
from pathlib import Path
from dataclasses import asdict
import hashlib,json,time
import cv2
import numpy as np
from .model import CameraTransform,Position,SafetyError,AcquisitionError,atomic_json
from .imaging import load_frame,save_frame
from .autofocus import autofocus

def register_candidate(source,target,source_transform,target_transform,centroid):
    """Locate a survey candidate in a revisit using specimen keypoints, across magnifications.

    Registration verifies the field and candidate coordinate, not the presence
    or material identity of a flake at that coordinate.
    The affine fit must agree with independently measured camera transforms.
    """
    def features(rgb):
        g=rgb[:,:,1].astype(np.float32);lo,hi=np.percentile(g,[1,99.5])
        g=np.uint8(np.clip((g-lo)/max(hi-lo,1)*255,0,255))
        return cv2.SIFT_create(nfeatures=6000,contrastThreshold=.01).detectAndCompute(g,None)
    a,da=features(source.rgb);b,db=features(target.rgb)
    if da is None or db is None:raise AcquisitionError('No features for candidate identity verification.')
    matches=[p[0] for p in cv2.BFMatcher().knnMatch(da,db,k=2) if len(p)==2 and p[0].distance<.70*p[1].distance]
    # Require unique target keypoints; duplicated matches must not inflate consensus.
    matches=list({m.trainIdx:m for m in sorted(matches,key=lambda m:-m.distance)}.values())
    if len(matches)<8:raise AcquisitionError('Fewer than eight independent revisit feature matches.')
    src=np.float32([a[m.queryIdx].pt for m in matches]);dst=np.float32([b[m.trainIdx].pt for m in matches])
    affine,inliers=cv2.estimateAffine2D(src,dst,method=cv2.RANSAC,ransacReprojThreshold=2.5,maxIters=5000,confidence=.995)
    if affine is None:raise AcquisitionError('Revisit registration has no consistent geometric fit.')
    good=inliers.ravel().astype(bool)
    if good.sum()<8 or good.mean()<.5:raise AcquisitionError('Revisit feature agreement is insufficient.')
    expected=np.linalg.inv(target_transform.matrix)@np.asarray(source_transform.matrix)
    error=np.linalg.norm(affine[:,:2]-expected)/np.linalg.norm(expected)
    if error>.08:raise AcquisitionError('Revisit image scale/orientation disagrees with objective calibration.')
    if np.ptp(dst[good],axis=0).min()<30:raise AcquisitionError('Revisit matches are too tightly clustered.')
    residual=np.linalg.norm(src[good]@affine[:,:2].T+affine[:,2]-dst[good],axis=1)
    point=affine[:,:2]@np.array(centroid)+affine[:,2]
    h,w=target.rgb.shape[:2]
    if not (8<point[0]<w-8 and 8<point[1]<h-8):raise AcquisitionError('Matched candidate lies outside the revisit image.')
    return {'candidate_px':point.tolist(),'inliers':int(good.sum()),'inlier_fraction':float(good.mean()),
            'median_error_px':float(np.median(residual)),'relative_transform_error':float(error),'affine':affine.tolist()}

def build_queue(analysis_folder,selection='priority',max_candidates=None):
    folder=Path(analysis_folder).resolve();payload=json.loads((folder/'flakes.json').read_text(encoding='utf-8'))
    source=Path(payload.get('source_folder',folder)).resolve()
    review={}
    if (folder/'reviews.json').exists():
        saved=json.loads((folder/'reviews.json').read_text(encoding='utf-8'))
        if saved.get('dataset_id')==payload.get('dataset_id'):review=saved.get('annotations',{})
    rows=[r for r in payload['flakes'] if r.get('disposition')!='rejected' and review.get(r['id'],{}).get('decision')!='reject']
    # Once material selection exists, automatic queues use its contiguous
    # support gate. Explicit human keeps can still request unresolved objects.
    rows=[r for r in rows if 'material_selection' not in r
          or r['material_selection'].get('catalog_selected',False)
          or review.get(r['id'],{}).get('decision')=='keep']
    if selection=='kept':rows=[r for r in rows if review.get(r['id'],{}).get('decision')=='keep']
    elif selection=='priority':rows=[r for r in rows if r.get('priority') in ('thin_screen','review_required','low_contrast')]
    elif selection!='all':raise ValueError('Selection must be priority, kept, or all.')
    if payload.get('validation_status')=='failed_user_review':
        rows=[r for r in rows if review.get(r['id'],{}).get('decision')=='keep']
    rows.sort(key=lambda r:(-r.get('area_um2',0),-r.get('quality_score',0)))
    # Round-robin tiles makes a small validation batch sample different fields.
    buckets={}
    for row in rows:buckets.setdefault(row.get('tile_key',row['tile_id']),[]).append(row)
    rows=[]
    while any(buckets.values()):
        for bucket in buckets.values():
            if bucket:rows.append(bucket.pop(0))
    if max_candidates is not None:
        if max_candidates<1:raise ValueError('Candidate limit must be positive.')
        rows=rows[:max_candidates]
    if not rows:raise AcquisitionError('No candidates match the revisit selection.')
    for row in rows:
        path=(source/row['source_image']).resolve()
        if not path.is_relative_to(source):raise SafetyError('Candidate image is outside its acquisition folder.')
        if not path.exists():raise AcquisitionError('Candidate raw image is missing.')
    return {'dataset_id':payload.get('dataset_id'),'source_folder':str(source),'selection':selection,'candidates':rows}

def run_revisits(scope,analysis_folder,profile,output,selection='priority',max_candidates=None,resume=False,callback=None,analysis_options=None):
    if not scope.armed:raise SafetyError('Arm the configured travel envelope before revisiting.')
    queue=build_queue(analysis_folder,selection,max_candidates)
    transform=CameraTransform(**profile['camera_transform'])
    if not transform.calibrated:raise SafetyError('Revisit objective requires a measured camera transform.')
    controls=scope.hardware_controls();position=profile['objective']['position']
    def check_objective():
        if controls.objectives()['position']!=position:raise SafetyError(f'Engage objective position {position} ({profile["objective"]["name"]}) before running this profile.')
    check_objective()  # No motion or optical changes until the engaged lens is verified.
    source=Path(queue['source_folder']);prepared=[]
    for row in queue['candidates']:
        path=(source/row['source_image']).resolve();manifest=json.loads((path.parent.parent/'manifest.json').read_text())
        sample=manifest.get('context',{}).get('sample_id')
        if not sample or sample!=profile['context'].get('sample_id'):raise SafetyError('Candidate sample ID differs from the revisit profile.')
        frame=load_frame(path)
        if frame.metadata.get('source')!=profile['source']:raise SafetyError('Revisit profile and candidate acquisition sources differ.')
        scope.limits.check(Position(*row['stage_xy_um'],scope.position().z))
        prepared.append((row,path,CameraTransform(**manifest['transform'])))
    fingerprint=hashlib.sha256(json.dumps({'queue':queue,'profile':profile,'analysis_options':analysis_options},sort_keys=True).encode()).hexdigest()
    from .storage import managed_output_path
    output=managed_output_path(output);checkpoint=output/'revisit.json'
    if output.exists() and any(output.iterdir()) and not resume:raise ValueError('Choose an empty revisit output or explicit resume.')
    output.mkdir(parents=True,exist_ok=True)
    state={'state':'running','fingerprint':fingerprint,'profile':profile,'queue':queue,'completed':{},'started_at':time.time()}
    if checkpoint.exists():
        state=json.loads(checkpoint.read_text())
        if state['fingerprint']!=fingerprint:raise SafetyError('Revisit resume differs from the original queue or optical profile.')
    scope.job_template=profile['job_template'];scope.camera_resources=dict(profile['camera_resources']);scope.expected_scale=transform.pixel_size_um
    scope.expected_objective_position=position
    controls.set_lamp_percent(profile['illumination_percent'])
    transform.validate_frame(scope.capture())
    state['state']='running';atomic_json(checkpoint,state)
    try:
        for index,(row,path,source_transform) in enumerate(prepared):
            scope.check_cancel();check_objective()
            name='candidate_'+hashlib.sha256(row['id'].encode()).hexdigest()[:14];folder=output/name
            if row['id'] in state['completed']:
                prior=state['completed'][row['id']];raw=folder/'tiles/tile_000000.tiff'
                if not raw.exists() or hashlib.sha256(raw.read_bytes()).hexdigest()!=prior['sha256']:raise AcquisitionError('Completed revisit image is missing or modified.')
                continue
            scope.travel(*row['stage_xy_um'])
            focus=autofocus(scope,**profile['focus'],output=folder/'focus',callback=(lambda f,m:callback('focus',f,m)) if callback else None)
            if not focus.success:raise AcquisitionError('Revisit autofocus failed: '+focus.reason)
            check_objective();frame=scope.capture();transform.validate_frame(frame)
            if frame.metadata.get('camera_resources')!=profile['camera_resources'] or abs(controls.lamp_percent()-profile['illumination_percent'])>1:
                raise SafetyError('Optical settings changed during the revisit.')
            save_frame(folder/'tiles','tile_000000',frame)
            record={'parent_candidate_id':row['id'],'position':asdict(frame.position),'objective_position':position,'focus':asdict(focus)}
            try:
                record['registration']=register_candidate(load_frame(path),frame,source_transform,transform,row['centroid_px'])
                record['identity_verified']=True  # legacy field: image registration only
                record['field_registration_verified']=True
                record['flake_identity_verified']=False
            except AcquisitionError as exc:
                record.update(identity_verified=False,field_registration_verified=False,flake_identity_verified=False,registration_error=str(exc))
            atomic_json(folder/'identity.json',record)
            # Unverified images remain on disk but cannot enter the confirmed revisit analysis.
            manifest={'state':'complete','transform':asdict(transform),'context':profile['context'],
                      'plan':{'chip_id':name,'polygon_um':None},'completed':{'tile_000000':record} if record['identity_verified'] else {},
                      'revisit_parent_id':row['id'],'revisit_source_chip_id':row.get('chip_id'),'optical_profile':profile}
            atomic_json(folder/'manifest.json',manifest)
            record['sha256']=hashlib.sha256((folder/'tiles/tile_000000.tiff').read_bytes()).hexdigest()
            state['completed'][row['id']]=record;atomic_json(checkpoint,state)
            if callback:callback('revisit',frame,{'index':index+1,'total':len(prepared),'verified':record['identity_verified']})
        verified=sum(r['identity_verified'] for r in state['completed'].values())
        if verified:
            from .analysis import analyze_scan
            summary=analyze_scan(output,analysis_options,check_cancel=scope.check_cancel)
            state['analysis_folder']=str(output/'analysis');state['candidate_count']=summary['candidate_count']
        state['verified_count']=verified;state['state']='complete'
    except BaseException as exc:
        state['state']='failed';state['error']=str(exc);scope.stop();raise
    finally:atomic_json(checkpoint,state)
    return state
