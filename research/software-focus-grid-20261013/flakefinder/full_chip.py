"""Full-enclosure coverage with substrate anchors and bounded plane predictions."""
from __future__ import annotations
from dataclasses import asdict,dataclass
from pathlib import Path
import hashlib,json,math,shutil,time,sys

import cv2,numpy as np

from .autofocus import autofocus,autofocus_range,focus_metrics
from .calibration import FocusMap
from .chip_discovery import BoundaryOptions,EvidenceMap,classify_boundary,file_hash,full_enclosure_scan_plan,validate_search_envelope
from .focus_surface import prepare_focus_surface
from .focus_recovery import recover_focus
from .imaging import load_frame,save_frame
from .layout import SubstrateReference,SUBSTRATE
from .vendor_lifecycle import checkpoint_observation,acknowledge_raw
from .model import Frame,Position,AcquisitionError,FocusSupportError,SafetyError,Cancelled,atomic_json
from .motion import move_z_bounded
from .scan import ScanPlan,Mosaic,tile_centres,export_csv


@dataclass
class FullChipOptions:
    overlap:float=.15
    focus_roi_um:float=160.
    minimum_substrate_fraction:float=.90
    anchor_count:int=4
    focus_half_range_um:float=9.
    focus_step_um:float=3.
    focus_fine_step_um:float=.5
    focus_max_residual_um:float=1.
    max_local_focus_offset_um:float=600.
    focus_search_bounds_um:list[float]|None=None
    max_tiles:int=10000
    focus_max_extrapolation_um:float=1500.
    focus_max_extrapolation_fraction:float=.25
    focus_max_prediction_sigma_um:float=2.
    focus_measurement_floor_um:float=.5
    adaptive_focus_recovery:bool=True
    focus_anchor_mode:str='chip_edges'
    edge_focus_roi_um:float=64.
    max_local_focus_attempts:int=2

    def __post_init__(self):
        values=[self.overlap,self.focus_roi_um,self.minimum_substrate_fraction,self.focus_half_range_um,
                self.focus_step_um,self.focus_fine_step_um,self.focus_max_residual_um,self.max_local_focus_offset_um,
                self.focus_max_extrapolation_um,self.focus_max_extrapolation_fraction,self.focus_max_prediction_sigma_um,self.focus_measurement_floor_um,self.edge_focus_roi_um]
        if not np.isfinite(values).all():raise ValueError('Full-chip parameters must be finite.')
        if not .05<=self.overlap<=.5 or self.focus_roi_um<=0 or not .8<=self.minimum_substrate_fraction<=1 or not 4<=self.anchor_count<=16:
            raise ValueError('Invalid coverage or substrate-support parameters.')
        if not 0<self.focus_fine_step_um<=self.focus_step_um or self.focus_half_range_um<=0 or self.focus_max_residual_um<=0 or self.max_local_focus_offset_um<0 or self.max_tiles<1:
            raise ValueError('Invalid focus or coverage budget.')
        if self.focus_max_extrapolation_um<0 or not 0<=self.focus_max_extrapolation_fraction<=1 or self.focus_max_prediction_sigma_um<=0 or self.focus_measurement_floor_um<=0:
            raise ValueError('Invalid bounded-plane focus parameters.')
        if self.focus_anchor_mode not in ('chip_edges','substrate') or self.edge_focus_roi_um<=0:
            raise ValueError('Invalid focus anchor mode or edge crop size.')
        if type(self.max_local_focus_attempts) is not int or not 0<=self.max_local_focus_attempts<=100:
            raise ValueError('Exceptional local-focus attempt budget must be an integer from 0 to 100.')


def _camera_context(frame):
    return {key:frame.metadata.get(key) for key in ('camera_job_template','camera_resources','objective_position','illumination_percent')}


def substrate_support_points(discovery,evidence,options):
    """Points supported by an ROI-sized mostly-substrate observed neighborhood."""
    flags=evidence['flags'];cell=float(evidence['cell_um']);bounds=evidence['bounds_um']
    confident=((flags&EvidenceMap.SUBSTRATE)>0)&((flags&EvidenceMap.HOLDER)==0)
    region=np.zeros(flags.shape,np.uint8)
    outline=(np.asarray(discovery['enclosure']['outline_um'])-[bounds[0],bounds[2]])/cell-.5
    cv2.fillPoly(region,[np.rint(outline).astype(np.int32)],1)
    confident &= region>0
    # This stage-aligned square contains the camera ROI under any rotation.
    width=max(3,int(math.ceil((options.focus_roi_um*math.sqrt(2)+2*cell)/cell)))
    if width%2==0:width+=1
    fraction=cv2.boxFilter(confident.astype(np.float32),-1,(width,width),normalize=True,borderType=cv2.BORDER_CONSTANT)
    yy,xx=np.nonzero((fraction>=options.minimum_substrate_fraction)&(region>0))
    if not len(xx):raise AcquisitionError('Boundary evidence contains no adequately supported autofocus ROI; acquire more interior substrate evidence.')
    # Bound plan size without removing the extreme samples used for hull anchors.
    points=np.column_stack([xx,yy])*cell+[bounds[0]+cell/2,bounds[2]+cell/2]
    stride=max(1,int(math.ceil(len(points)/12000)))
    selected=set(range(0,len(points),stride))
    for angle in np.linspace(0,2*np.pi,32,endpoint=False):selected.add(int(np.argmax(points@[math.cos(angle),math.sin(angle)])))
    return points[sorted(selected)],{'roi_support_window_um':width*cell,'minimum_substrate_fraction':options.minimum_substrate_fraction,
                                    'support_cell_um':cell,'source':'registered observed substrate; conflicts and unobserved cells excluded'}


def _anchor_points(points,count):
    # Keep measured interior points too: a triangular hull can still provide
    # four independent measurements for fitting and checking the plane.
    hull=np.unique(np.asarray(points,float),axis=0)
    selected=[hull[int(np.argmax(np.linalg.norm(hull-hull.mean(axis=0),axis=1)))]]
    separation=max(float(np.linalg.norm(np.ptp(hull,axis=0)))*.12,1e-6)
    while len(selected)<min(count,len(hull)):
        distances=np.linalg.norm(hull[:,None,:]-np.asarray(selected)[None,:,:],axis=2).min(axis=1)
        if len(selected)==2:
            direction=selected[1]-selected[0]
            # The third anchor must span the chip's short dimension as well.
            score=abs((hull[:,0]-selected[0][0])*direction[1]-(hull[:,1]-selected[0][1])*direction[0])
            score[distances<separation]=-1
        else:score=distances
        index=int(np.argmax(score))
        if distances[index]<separation:break
        selected.append(hull[index])
    result=np.asarray(selected)
    if len(result)<max(4,count) or np.linalg.matrix_rank(result-result.mean(axis=0))<2:
        raise AcquisitionError('At least four separated supported focus anchors are required before acquisition; more substrate evidence is required.')
    return result


def observed_edge_anchors(discovery,folder,polygon,count=4):
    """Choose separated observed edge-image means, never inferred corners."""
    candidates=[];seen=set();seed=np.asarray(discovery['seed_xy_um'],float)
    for record in discovery.get('records',[]):
        edge=record.get('edge')
        reanalysis=None
        if not edge:
            # A rectangle replay measures new raw points in an existing image;
            # preserve the original rejection while reusing its exact source.
            derived=discovery.get('rectangle_reanalysis',{}).get(record.get('image'),{})
            if derived.get('edge') and derived.get('validation',{}).get('accepted') is True:
                model=derived.get('rectangle_model_sha256')
                if (not model or model!=discovery.get('rectangle_model_sha256')
                        or derived.get('source_hashes')!=record.get('hashes')
                        or derived['edge'].get('image',record.get('image'))!=record.get('image')):
                    raise SafetyError('Rectangle edge anchor differs from its recorded model or source provenance.')
                edge=derived['edge'];reanalysis=derived
        if not edge:continue
        image=record.get('image') or edge.get('image')
        if not image or image in seen:continue
        points=np.asarray(edge.get('points_um',[]),float)
        if points.ndim!=2 or points.shape[1]!=2 or len(points)<3 or not np.isfinite(points).all():continue
        normal=np.asarray(edge.get('normal',[]),float)
        if normal.shape!=(2,) or not np.isfinite(normal).all() or float(normal@seed)>=float(edge['offset_um']):continue
        centre=points.mean(axis=0)
        if cv2.pointPolygonTest(np.asarray(polygon,np.float32),tuple(centre),False)<0:continue
        # Existing coherent-edge extraction supplies side-purity/line evidence.
        # Verify its recorded sources instead of treating the fitted polygon as
        # a photographed edge or adding another image-acquisition phase.
        hashes=record.get('hashes',{})
        if image not in hashes:raise SafetyError('Measured edge anchor lacks its source image hash.')
        for relative,digest in hashes.items():
            if file_hash(Path(folder)/relative)!=digest:raise SafetyError('Measured edge anchor source changed: '+relative)
        candidates.append({'xy_um':centre.tolist(),'source_image':image,'source_hashes':hashes,
            'observed_edge_point_count':len(points),'normal':normal.tolist(),'offset_um':float(edge['offset_um']),
            'source':'arithmetic mean of observed coherent-edge points in one acquired image'})
        if reanalysis is not None:
            candidates[-1].update(source='arithmetic mean of raw RGB edge points from rectangle reanalysis of the same acquired image',
                rectangle_model_sha256=reanalysis['rectangle_model_sha256'],
                original_capture_position_um=record.get('position_um'),
                reanalysis_validation=reanalysis['validation'])
        seen.add(image)
    if len(candidates)<3:return None,{'available_edge_images':len(candidates),'reason':'Fewer than three observed coherent edge images.'}
    values=np.asarray([r['xy_um'] for r in candidates])
    try:selected=_anchor_points(values,count)
    except AcquisitionError:return None,{'available_edge_images':len(candidates),'reason':'Observed edge-image centres are collinear or insufficiently separated.'}
    chosen=[candidates[int(np.argmin(np.linalg.norm(values-p,axis=1)))] for p in selected]
    return np.asarray([r['xy_um'] for r in chosen]),{'available_edge_images':len(candidates),'anchors':chosen,
        'source':'verified saved edge-image samples; no inferred corners','local_focus_verified':False}


def build_full_chip_plan(discovery_folder,reference_frame,transform,options=None,chip_id=None,estimated_outline=None):
    options=options or FullChipOptions()
    if not isinstance(options,FullChipOptions):options=FullChipOptions(**options)
    folder=Path(discovery_folder).resolve();source=folder/'discovery.json';discovery=json.loads(source.read_text())
    if not np.allclose(discovery['transform']['matrix'],transform.matrix) or discovery['reference']['context']!=SubstrateReference.from_frame(reference_frame).context:
        raise SafetyError('Boundary/reference optical profile or transform differs.')
    scan_options=dict(overlap=options.overlap,mode='survey',chip_id=chip_id,focus_strategy='surface',max_tiles=options.max_tiles)
    if estimated_outline is None:
        scan=full_enclosure_scan_plan(discovery,**scan_options)
        boundary_estimate={'status':'observed_enclosure','uncertainty_um':discovery['enclosure']['edge_sampling_uncertainty_um']}
        support_discovery=discovery
    else:
        polygon=np.asarray(estimated_outline['polygon_um'],float)
        uncertainty=float(estimated_outline['uncertainty_um'])
        if polygon.ndim!=2 or polygon.shape[1]!=2 or len(polygon)<3 or not np.isfinite(polygon).all() or not math.isfinite(uncertainty) or uncertainty<=0:
            raise ValueError('Estimated outline needs a finite polygon and positive uncertainty in micrometres.')
        if not estimated_outline.get('method'):raise ValueError('Estimated outline must identify its method.')
        polygon=cv2.convexHull(polygon.astype(np.float32))[:,0,:].astype(float)
        if cv2.contourArea(polygon.astype(np.float32))<=0:raise ValueError('Estimated outline must enclose an area.')
        # The input is an already expanded conservative polygon. Its uncertainty
        # is retained as a model limitation, never converted into measured edges.
        bounds=[float(polygon[:,0].min()),float(polygon[:,0].max()),float(polygon[:,1].min()),float(polygon[:,1].max())]
        if cv2.pointPolygonTest(polygon.astype(np.float32),tuple(discovery['seed_xy_um']),False)<=0:
            raise ValueError('Estimated outline must contain the verified substrate seed.')
        search=discovery['options']['search_bounds_um']
        if bounds[0]<search[0] or bounds[1]>search[1] or bounds[2]<search[2] or bounds[3]>search[3]:
            raise ValueError('Estimated outline exceeds the recorded search bounds.')
        scan=ScanPlan(bounds,polygon_um=polygon.tolist(),**scan_options)
        boundary_estimate={**estimated_outline,'status':'estimated','polygon_um':polygon.tolist(),
                           'validation':'Acquire the planned raster; record image evidence of missed boundaries without extra frontier acquisition.'}
        support_discovery={**discovery,'enclosure':{'outline_um':polygon.tolist()}}
    centres=tile_centres(scan,reference_frame,transform)
    with np.load(folder/'evidence.npz') as stored:evidence={key:stored[key].copy() for key in stored.files}
    support,support_info=substrate_support_points(support_discovery,evidence,options)
    anchors=None;edge_evidence=None;anchor_mode='substrate'
    if options.focus_anchor_mode=='chip_edges':
        anchors,edge_evidence=observed_edge_anchors(discovery,folder,scan.polygon_um,options.anchor_count)
        if anchors is not None:anchor_mode='chip_edges'
    if anchors is None:anchors=_anchor_points(support,options.anchor_count)
    plan={'schema':1,'kind':'full_estimated_enclosure_coverage' if estimated_outline is not None else 'full_measured_enclosure_coverage','chip_id':chip_id,'scan_plan':asdict(scan),
          'tile_centres_um':[list(p) for p in centres],'tile_count':len(centres),'options':asdict(options),
          'transform':asdict(transform),'camera_shape':list(reference_frame.rgb.shape),'acquisition_settings':_camera_context(reference_frame),
          'focus_reference_pixels_sha256':hashlib.sha256(np.ascontiguousarray(reference_frame.rgb).tobytes()).hexdigest(),
          'discovery_folder':str(folder),'discovery_sha256':file_hash(source),'evidence_sha256':file_hash(folder/'evidence.npz'),
          'support_points_um':support.tolist(),'support_evidence':support_info,'anchor_xy_um':anchors.tolist(),
          'focus_anchor_mode':anchor_mode,'edge_anchor_evidence':edge_evidence,
          'focus_anchor_roi_um':options.edge_focus_roi_um if anchor_mode=='chip_edges' else options.focus_roi_um,
          'boundary_estimate':boundary_estimate,'seed_xy_um':discovery['seed_xy_um'],
          'coverage_note':'Every planned field intersecting the conservative enclosure polygon is acquired. Planned-area coverage and evidence validating the estimated chip extent are reported separately.',
          'focus_policy':{'plane':'At least four measured points; every residual within its limit. Bounded extrapolation assumes a planar chip.',
                          'outside_hull':'Accept a bounded measured-plane prediction only within distance and conditional uncertainty limits; otherwise autofocus an image-supported ROI.',
                          'max_extrapolation_um':options.focus_max_extrapolation_um,'max_extrapolation_fraction_of_anchor_diameter':options.focus_max_extrapolation_fraction,
                          'max_conditional_prediction_sigma_um':options.focus_max_prediction_sigma_um,'measurement_floor_um':options.focus_measurement_floor_um,
                          'plane_prediction_is_independent_local_focus_verification':False,
                          'nearby_height_is_plane_extrapolation':False,'nearby_height_image_focus_verified':False}}
    plan['fingerprint']=hashlib.sha256(json.dumps(plan,sort_keys=True).encode()).hexdigest()
    return plan


def load_prior_focus_points(scan_folder,plan,context):
    """Only successful anchors from the same sample and optical profile."""
    if scan_folder is None:return [],None
    folder=Path(scan_folder).resolve();manifest_path=folder/'manifest.json'
    manifest=json.loads(manifest_path.read_text())
    if manifest.get('context',{}).get('sample_id')!=context.get('sample_id') or not context.get('sample_id'):
        raise SafetyError('Prior autofocus belongs to a different or unidentified sample.')
    if manifest.get('acquisition_settings')!=plan['acquisition_settings'] or json.dumps(manifest.get('transform'),sort_keys=True)!=json.dumps(plan['transform'],sort_keys=True):
        raise SafetyError('Prior autofocus optical profile or camera transform differs.')
    points=[];sources={'manifest':str(manifest_path),'manifest_sha256':file_hash(manifest_path)}
    report_path=folder/'focus-surface/focus-surface.json'
    if report_path.exists():
        report=json.loads(report_path.read_text());sources.update(focus_report=str(report_path),focus_report_sha256=file_hash(report_path))
        for anchor in report.get('anchors',[]):
            if anchor.get('status')=='verified':points.append(anchor['position_um'])
    for value in manifest.get('completed',{}).values():
        if (value.get('focus') or {}).get('success'):points.append([value['position'][axis] for axis in ('x','y','z')])
    polygon=np.asarray(plan['scan_plan']['polygon_um'],np.float32)
    points=[list(map(float,p)) for p in points if np.isfinite(p).all() and cv2.pointPolygonTest(polygon,tuple(map(float,p[:2])),False)>=0]
    return np.unique(points,axis=0).tolist() if points else [],sources


def load_seed_focus_points(seed_focus_results,plan,*,limits=None,position_tolerance_um=.5):
    """Validate explicitly paired fresh focus results and full camera images.

    Standalone FocusResult JSON has no XY; its explicitly paired image supplies
    XY and must match the successful result's Z. Recovery reports additionally
    provide an initial XY, which is checked when present. Source hashes are
    persisted in the scan fingerprint and optionally asserted by the caller.
    Entries default to purpose='verified'. Explicit purpose='search_seed'
    retains uncertain-XY/full-field focus only as search initialization; these
    points never enter the returned verified list or an acquisition plane.
    """
    from .model import CameraTransform
    if seed_focus_results is None:return [],None
    if not isinstance(seed_focus_results,(list,tuple)):raise ValueError('seed_focus_results must be a list of result/frame pairs.')
    if not np.isfinite(position_tolerance_um) or position_tolerance_um<=0:raise ValueError('Invalid seed focus position tolerance.')
    transform=CameraTransform(**plan['transform']);polygon=np.asarray(plan['scan_plan']['polygon_um'],np.float32)
    points=[];search_points=[];records=[]
    for entry in seed_focus_results:
        if not isinstance(entry,dict) or not entry.get('result') or not entry.get('frame'):
            raise ValueError('Each seed focus pair requires result and frame paths.')
        if entry.get('kind')=='verified_edge_roi':
            from .edge_focus_reuse import load_edge_focus_entry
            point,record=load_edge_focus_entry(entry,plan,limits=limits,position_tolerance_um=position_tolerance_um)
            if not any(np.linalg.norm(np.asarray(point)-p)<1e-6 for p in points):points.append(point)
            records.append(record);continue
        purpose=entry.get('purpose','verified')
        if purpose not in ('verified','search_seed'):raise ValueError('Seed focus purpose must be verified or search_seed.')
        result_path=Path(entry['result']).resolve();image_path=Path(entry['frame']).resolve();metadata_path=image_path.with_suffix('.json')
        hashes={key:file_hash(path) for key,path in (('result_sha256',result_path),('frame_sha256',image_path),('metadata_sha256',metadata_path))}
        for key,digest in hashes.items():
            if entry.get(key) is not None and entry[key]!=digest:raise SafetyError('Verified seed focus evidence changed: '+str(entry.get('result')))
        report=json.loads(result_path.read_text(encoding='utf-8-sig'))
        result=report if 'success' in report else report.get('result',{})
        if result.get('success') is not True or not np.isfinite(result.get('z_um',np.nan)):
            raise SafetyError('Seed focus result must report successful finite focus.')
        frame=load_frame(image_path);transform.validate_frame(frame)
        if list(frame.rgb.shape)!=plan['camera_shape'] or _camera_context(frame)!=plan['acquisition_settings']:
            raise SafetyError('Seed focus image camera geometry or optical profile differs from this chip.')
        point=[float(frame.position.x),float(frame.position.y),float(frame.position.z)]
        if abs(point[2]-float(result['z_um']))>position_tolerance_um:
            raise SafetyError('Seed focus image Z differs from its successful focus result.')
        reported_position=report.get('initial_position_um') or result.get('position_um')
        if reported_position is not None:
            xy=[reported_position[axis] for axis in ('x','y')] if isinstance(reported_position,dict) else reported_position[:2]
            if not np.isfinite(xy).all() or np.linalg.norm(np.asarray(xy)-point[:2])>position_tolerance_um:
                raise SafetyError('Seed focus image XY differs from the focus-result position.')
        if cv2.pointPolygonTest(polygon,tuple(point[:2]),False)<=0:
            raise SafetyError('Verified seed focus image must lie inside the estimated chip polygon.')
        if limits is not None:limits.check(frame.position)
        for key,path in (('result_sha256',result_path),('frame_sha256',image_path),('metadata_sha256',metadata_path)):
            if file_hash(path)!=hashes[key]:raise SafetyError('Seed focus evidence changed while being read.')
        destination=points if purpose=='verified' else search_points
        if not any(np.linalg.norm(np.asarray(point)-p)<1e-6 for p in destination):destination.append(point)
        records.append({'result':str(result_path),'frame':str(image_path),'metadata':str(metadata_path),**hashes,
            'position_um':point,'result_z_um':float(result['z_um']),'result_success':True,
            'purpose':purpose,'local_focus_verified':purpose=='verified',
            'result_xy_available':reported_position is not None,'xy_source':'explicitly_paired_full_camera_image',
            'position_tolerance_um':position_tolerance_um,'acquisition_settings':_camera_context(frame)})
    return points,{'schema':1,'records':records,'verified_points_um':points,'search_seed_points_um':search_points,
        'qualification':'Search-seed purpose is initialization only: it never supplies verified plane support. Verified-purpose evidence applies only at its measured location; other anchors require local verification.'}


def _roi_box(frame,roi_um,centre_px=None):
    h,w=frame.rgb.shape[:2]
    sizes=np.maximum(8,np.rint(np.array([roi_um/frame.pixel_size_um[0],roi_um/frame.pixel_size_um[1]])).astype(int))
    sizes=np.minimum(sizes,[w,h]);sizes-=np.mod(np.array([w,h])-sizes,2)
    centre=np.array([(w-1)/2,(h-1)/2]) if centre_px is None else np.asarray(centre_px)
    left,top=np.rint(centre-(sizes-1)/2).astype(int)
    if left<0 or top<0 or left+sizes[0]>w or top+sizes[1]>h:return None
    return [int(left),int(top),int(left+sizes[0]),int(top+sizes[1])]


def edge_focus_roi(frame,roi_um,transform,*,boundary_proposal=None):
    """Centre a fixed ROI on the dominant current grayscale transition.

    Discovery's colour threshold can move tens of micrometres under defocus.
    Fit orientation from low-frequency green gradients, then locate the peak
    of the tangentially averaged normal derivative in the current full field.
    """
    sh,sw=frame.rgb.shape[:2];scale=min(1.,512/max(sh,sw))
    g=cv2.resize(frame.rgb[:,:,1].astype(np.float32),(round(sw*scale),round(sh*scale)),interpolation=cv2.INTER_AREA)
    g=cv2.GaussianBlur(g,(0,0),3);h,w=g.shape
    low,high=np.percentile(g,[10,90]);contrast=float((high-low)/max(float(np.mean(g)),1))
    if contrast<.15:raise AcquisitionError('No substantial dark/bright chip-edge transition in the current full field.')
    gx=cv2.Sobel(g,cv2.CV_32F,1,0,ksize=3);gy=cv2.Sobel(g,cv2.CV_32F,0,1,ksize=3)
    tensor=np.array([[np.sum(gx*gx),np.sum(gx*gy)],[np.sum(gx*gy),np.sum(gy*gy)]],float)
    eigen,axes=np.linalg.eigh(tensor);normal=axes[:,-1]
    coherence=float((eigen[1]-eigen[0])/max(eigen.sum(),1e-12))
    line_hint=None;method='dominant_green_transition_gradient';hint_window=10.
    if boundary_proposal is not None:
        # A label-derived direction may guide focus even when blur prevents
        # the independent raw line validator from accepting a chip boundary.
        # Locate the transition again in current raw green intensities. This
        # hint is never promoted to a measured boundary or a verified focus.
        ns=np.asarray(boundary_proposal.get('normal',[]),float)
        offset=boundary_proposal.get('offset_um')
        if ns.shape!=(2,) or not np.isfinite(ns).all() or not isinstance(offset,(int,float)) or not np.isfinite(offset):
            raise AcquisitionError('Invalid current boundary proposal for focus selection.')
        linear=np.asarray(transform.matrix)@np.diag([sw/w,sh/h])
        normal=linear.T@ns;factor=float(np.linalg.norm(normal))
        if factor<1e-12:raise AcquisitionError('Degenerate boundary proposal for focus selection.')
        normal/=factor
        origin=np.array([frame.position.x,frame.position.y])+np.asarray(transform.matrix)@[(sw/w-sw)/2,(sh/h-sh)/2]
        line_hint={'offset_working_px':float((offset-origin@ns)/factor),
                   'qualification':'Unvalidated current label proposal, used only to select an autofocus ROI.'}
        hint_window=50./factor;method='label_guided_green_transition'
    elif coherence<.6:
        # Bright flakes can dominate the global tensor while a long cleave
        # edge is plainly visible. Require a long, consistently separating
        # line; short flake outlines cannot substitute for the chip boundary.
        scaled=np.uint8(np.clip((g-low)/max(high-low,1)*255,0,255))
        edges=cv2.Canny(scaled,20,50)
        lines=cv2.HoughLinesP(edges,1,np.pi/720,40,minLineLength=.45*min(h,w),maxLineGap=15)
        candidates=[]
        for line in ([] if lines is None else lines.reshape(-1,4)):
            a=np.asarray(line[:2],float);b=np.asarray(line[2:],float);tangent=b-a;length=float(np.linalg.norm(tangent))
            n=np.array([-tangent[1],tangent[0]])/length
            samples=a[None,:]+np.linspace(.05,.95,64)[:,None]*tangent
            plus=np.rint(samples+8*n).astype(int);minus=np.rint(samples-8*n).astype(int)
            valid=((plus[:,0]>=0)&(plus[:,0]<w)&(plus[:,1]>=0)&(plus[:,1]<h)&
                   (minus[:,0]>=0)&(minus[:,0]<w)&(minus[:,1]>=0)&(minus[:,1]<h))
            if valid.sum()<48:continue
            delta=g[plus[valid,1],plus[valid,0]]-g[minus[valid,1],minus[valid,0]]
            difference=float(np.median(delta))
            if abs(difference)<.15*max(float(np.mean(g)),1) or np.mean(np.sign(delta)==np.sign(difference))<.85:continue
            candidates.append((length,n,float(n@((a+b)/2))))
        if not candidates:raise AcquisitionError('Current full field lacks a coherent gradient or long separating chip-edge line.')
        length,normal,offset=max(candidates,key=lambda item:item[0]);line_hint={'length_working_px':length,'offset_working_px':offset}
        method='long_green_separator_line'
    yy,xx=np.mgrid[:h,:w];q=xx*normal[0]+yy*normal[1];q0=float(q.min())
    bins=np.floor(q-q0).astype(int)
    support=np.ones((h,w),bool)
    if boundary_proposal is not None:
        # Use the tangential footprint of the eventual crop. A rough edge's
        # location elsewhere in the field must not displace this local ROI.
        tangent=-xx*normal[1]+yy*normal[0]
        tangent_centre=-(w-1)/2*normal[1]+(h-1)/2*normal[0]
        support=abs(tangent-tangent_centre)<=roi_um/(2*factor)
    counts=np.bincount(bins[support].ravel(),minlength=int(bins.max())+1)
    sums=np.bincount(bins[support].ravel(),weights=g[support].ravel(),minlength=len(counts));profile=sums/np.maximum(counts,1)
    profile=cv2.GaussianBlur(profile[:,None],(0,0),3)[:,0]
    slope=-np.gradient(profile) if boundary_proposal is not None else np.abs(np.gradient(profile))
    valid=counts>=.5*counts.max();valid[:5]=False;valid[-5:]=False
    baseline_valid=valid.copy()
    if line_hint is not None:valid &= abs(q0+np.arange(len(profile))+.5-line_hint['offset_working_px'])<=hint_window
    if not valid.any():raise AcquisitionError('Chip-edge profile has insufficient full-field support.')
    peak=int(np.argmax(np.where(valid,slope,-1)))
    baseline=float(np.median(np.abs(slope[baseline_valid])));strength=float(slope[peak])
    if strength<2*max(baseline,1e-6):raise AcquisitionError('Only a broad intensity ramp is visible; no localized chip-edge transition.')
    guided_step=None
    if boundary_proposal is not None:
        span=max(3,int(roi_um/(2*factor)));near=max(1,span//2)
        # Bright debris can have a stronger downward slope than the chip
        # boundary. In this dark-holder mode the far side must reach the
        # field's dark plateau, not merely fall from debris to substrate.
        maxima=np.flatnonzero(valid & (slope>=np.roll(slope,1)) & (slope>=np.roll(slope,-1)))
        for candidate in sorted(maxima,key=lambda i:slope[i],reverse=True):
            if slope[candidate]<2*max(baseline,1e-6):break
            left=profile[max(0,candidate-span):max(0,candidate-near)]
            right=profile[candidate+near:min(len(profile),candidate+span)]
            if min(len(left),len(right))<2:continue
            step=float((np.median(left)-np.median(right))/max(float(np.mean(g)),1))
            if step>=.1 and np.median(right)<=low+.1*(high-low):
                peak=int(candidate);strength=float(slope[peak]);guided_step=step;break
        if guided_step is None:raise AcquisitionError('Guided focus crop lacks a substantial transition to the dark holder plateau.')
    centre=np.array([(w-1)/2,(h-1)/2]);centre+=normal*(q0+peak+.5-normal@centre)
    source=(centre+.5)*[sw/w,sh/h]-.5
    box=_roi_box(frame,roi_um,source)
    if box is None:raise AcquisitionError('Current chip-edge transition is too near the camera border for the focus ROI.')
    x0,y0,x1,y1=box;actual=[(x0+x1-1)/2,(y0+y1-1)/2]
    return {'box_px':box,'edge_centre_px':source.tolist(),'roi_centre_px':actual,
            'point_xy_um':transform.stage_xy(actual,frame).tolist(),
            'method':method,
            'long_line_evidence':line_hint,'relative_full_field_contrast':contrast,
            'orientation_coherence':coherence,'normal_working_pixels':normal.tolist(),
            'profile_peak_to_median_slope':strength/max(baseline,1e-6),
            'guided_profile_step_contrast':guided_step,
            'selection_position_um':asdict(frame.position),'local_focus_verified':False,
            'policy':'Fixed ROI after current full-field edge registration; search image is not focus verification.'}


class FocusROIAdapter:
    """Focus-only cropped frames; stage commands still reach the owned adapter."""
    def __init__(self,scope,roi_um,transform,box=None,reference_frame=None,*,edge=False,edge_proposals=None):
        self.scope=scope;self.roi_um=roi_um;self.transform=transform;self.box=box;self.reference_frame=reference_frame;self.edge=edge
        self.edge_proposals=list(edge_proposals or [])
        self.selection_frame=None;self.selection=None;self._retention_frames={}
    def __getattr__(self,name):return getattr(self.scope,name)
    def retention_full_frame(self,frame):
        identifier=frame.metadata.get('zen_vendor_result',{}).get('capture_id')
        return self._retention_frames.get(identifier,frame)
    def forget_retention_frame(self,frame):
        identifier=frame.metadata.get('zen_vendor_result',{}).get('capture_id')
        self._retention_frames.pop(identifier,None)
    def begin_focus_anchor(self):
        """Fix a textured, substrate-supported ROI for this anchor's Z sweep."""
        if self.edge:
            self.box=None;self.selection=None
            full=self.scope.capture();self.transform.validate_frame(full);self.selection_frame=full
            if self.reference_frame is not None and (full.rgb.shape!=self.reference_frame.rgb.shape or _camera_context(full)!=_camera_context(self.reference_frame)):
                raise SafetyError('Optical profile changed before chip-edge registration.')
            try:self.selection=edge_focus_roi(full,self.roi_um,self.transform)
            except AcquisitionError as primary_error:
                # The photographed side that supplied this anchor is a useful
                # direction/location proposal when debris dominates the global
                # tensor. Locate its transition again in the current raw field.
                if self.edge_proposals:
                    xy=np.array([full.position.x,full.position.y])
                    proposal=min(self.edge_proposals,key=lambda item:np.linalg.norm(np.asarray(item['xy_um'])-xy))
                    boundary={'normal':proposal['normal'],
                        'offset_um':proposal.get('offset_um',float(np.dot(proposal['normal'],proposal['xy_um'])))}
                    try:
                        self.selection=edge_focus_roi(full,self.roi_um,self.transform,boundary_proposal=boundary)
                        self.selection['measured_anchor_proposal']={**boundary,
                            'source_image':proposal.get('source_image'),'source_hashes':proposal.get('source_hashes')}
                    except AcquisitionError:
                        pass
                # Busy sharp fields can defeat the global gradient proposal.
                # Reuse the bounded same-frame low-pass selector only to place
                # the fixed ROI; autofocus still scores and confirms raw data.
                if self.selection is None:
                    from .autonomous_reference import select_edge_roi
                    try:self.selection=select_edge_roi(full,self.roi_um,self.transform)
                    except AcquisitionError as exc:
                        self.selection={'status':'failed','reason':str(exc),
                            'primary_selector_rejection':str(primary_error),
                            'selection_position_um':asdict(full.position)};raise
                self.selection['primary_selector_rejection']=str(primary_error)
            self.box=self.selection['box_px']
            return
        if self.reference_frame is None:return
        full=self.scope.capture();self.transform.validate_frame(full)
        if full.rgb.shape!=self.reference_frame.rgb.shape or _camera_context(full)!=_camera_context(self.reference_frame):
            raise SafetyError('Optical profile changed before focus-anchor selection.')
        reference=SubstrateReference.from_frame(self.reference_frame)
        self.selection_frame=full
        roi=supported_roi(full,reference,self.roi_um,.90,self.transform)
        self.selection=roi or {'status':'failed','reason':'No substrate-supported ROI'}
        if roi is None:raise AcquisitionError('No substrate-supported focus region in the anchor field.')
        self.box=roi['box_px']
    def save_focus_selection(self,folder):
        if self.selection_frame is not None:
            save_frame(folder,'full-field',self.selection_frame)
            atomic_json(Path(folder)/'selection.json',self.selection)
            checkpoint_observation(self,self.selection_frame,folder,'focus_roi_selection',self.selection,
                selected_raw=Path(folder)/'full-field.tiff')
    def capture(self):return self._focus_frame(self.scope.capture())
    def capture_with_processing(self,processor):
        def process(full):
            frame=self._focus_frame(full);return frame,processor(frame)
        if hasattr(self.scope,'capture_with_processing'):
            _,result=self.scope.capture_with_processing(process);return result
        return process(self.scope.capture())
    def _focus_frame(self,full):
        self.transform.validate_frame(full)
        if self.reference_frame is not None and (full.rgb.shape!=self.reference_frame.rgb.shape or _camera_context(full)!=_camera_context(self.reference_frame)):
            raise SafetyError('Camera/objective/illumination profile changed during substrate autofocus.')
        box=self.box or _roi_box(full,self.roi_um)
        if box is None:raise AcquisitionError('Focus ROI does not fit the camera field.')
        x0,y0,x1,y1=box
        if hasattr(self.scope,'vendor_frame_pending') and self.scope.vendor_frame_pending(full):
            self._retention_frames[full.metadata['zen_vendor_result']['capture_id']]=full
        centre=[(x0+x1-1)/2,(y0+y1-1)/2]
        xy=self.transform.stage_xy(centre,full)
        metadata={**full.metadata,'focus_only_roi_source_px':box,'physical_stage_position':asdict(full.position)}
        if self.edge:
            metadata['focus_mask_policy']='include_chip_edge'
            metadata['edge_focus_selection']=self.selection
        return Frame(full.rgb[y0:y1,x0:x1].copy(),Position(float(xy[0]),float(xy[1]),full.position.z),
                     full.pixel_size_um,full.sequence,full.acquired_at,metadata)


def supported_roi(frame,reference,roi_um,minimum_fraction,transform):
    """Choose a mostly-substrate in-field ROI, using texture only to rank it."""
    labels=reference.classify(frame);mh,mw=labels.shape;h,w=frame.rgb.shape[:2]
    width=max(3,int(math.ceil(roi_um/frame.pixel_size_um[0]*mw/w)))
    height=max(3,int(math.ceil(roi_um/frame.pixel_size_um[1]*mh/h)))
    if width>mw or height>mh:return None
    fractions=cv2.boxFilter(np.float32(labels==SUBSTRATE),-1,(width,height),normalize=True,borderType=cv2.BORDER_CONSTANT)
    yy,xx=np.nonzero(fractions>=minimum_fraction)
    if not len(xx):return None
    # Search every supported centre rather than nine arbitrary positions. A
    # narrow textured ROI gives a better spatial focus coordinate on tilted
    # chips. Broad illumination gradients and pixel noise are not useful texture.
    g=cv2.resize(frame.rgb[:,:,1].astype(np.float32),(mw,mh),interpolation=cv2.INTER_AREA)
    g/=max(float(np.median(g[labels==SUBSTRATE])),1.)
    smooth=cv2.GaussianBlur(g,(0,0),1.2)
    band=smooth-cv2.GaussianBlur(smooth,(0,0),8.)
    noise=float(np.median(abs(g-smooth)[labels==SUBSTRATE]))*.3
    signal=np.maximum(abs(band)-max(5*noise,.003),0.)**2
    # Large non-substrate components are holder regions; suppress their edges.
    # Small colored objects remain eligible to provide the focus signal.
    from .layout import NON_SUBSTRATE
    count,components,stats,_=cv2.connectedComponentsWithStats(np.uint8(labels==NON_SUBSTRATE),8)
    area_per_pixel=abs(float(np.linalg.det(transform.matrix)))*h*w/(mh*mw)
    large=np.flatnonzero(stats[:,cv2.CC_STAT_AREA]*area_per_pixel>5000.)
    large=large[large!=0]
    if len(large):
        excluded=cv2.dilate(np.uint8(np.isin(components,large)),np.ones((21,21),np.uint8))
        signal[excluded>0]=0
    scores=cv2.boxFilter(signal,-1,(width,height),normalize=True,borderType=cv2.BORDER_CONSTANT)
    order=np.argsort(scores[yy,xx])[::-1]
    for index in order:
        if scores[yy[index],xx[index]]<=1e-8:return None
        centre=(np.array([xx[index],yy[index]])+.5)*[w/mw,h/mh]-.5
        box=_roi_box(frame,roi_um,centre)
        if box is None:continue
        x0,y0,x1,y1=box;actual_centre=[(x0+x1-1)/2,(y0+y1-1)/2]
        return {'box_px':box,'point_xy_um':transform.stage_xy(actual_centre,frame).tolist(),
                'substrate_fraction':float(fractions[yy[index],xx[index]]),'score':float(scores[yy[index],xx[index]]),
                'selection':'maximum noise-suppressed texture over all supported centres'}
    return None


def _strict_surface(points,residual_limit):
    if len(points)<3:return None
    values=np.unique(np.asarray(points,float),axis=0)
    design=np.column_stack([values[:,:2]-values[:,:2].mean(axis=0),np.ones(len(values))])
    if np.linalg.matrix_rank(design)<3:return None
    residual=values[:,2]-design@np.linalg.lstsq(design,values[:,2],rcond=None)[0]
    if np.max(abs(residual))>residual_limit:return None
    return FocusMap(values,max_residual_um=residual_limit)


def rectangle_plane_domain(plan):
    """Camera-centre domain whose field intersects this measured chip plan.

    The outline already includes its measured geometry uncertainty margin.
    Minkowski expansion adds only the calibrated camera half-footprint, not
    another chip, a pitch prior, or a larger arbitrary travel allowance.
    """
    if plan is None:return None
    estimate=plan.get('boundary_estimate',{})
    if estimate.get('method')!='measured_sides_shared_rectangle_orientation':return None
    lines=estimate.get('lines',[])
    if estimate.get('status')!='estimated' or len(lines)!=4 or any(not line.get('source_images') or line.get('edge_point_count',0)<3 for line in lines):
        raise AcquisitionError('Rectangle focus support requires four measured sides.')
    polygon=np.asarray(plan['scan_plan']['polygon_um'],float)
    if polygon.shape!=(4,2) or not np.isfinite(polygon).all():raise AcquisitionError('Rectangle focus support polygon is invalid.')
    if not np.allclose(polygon,np.asarray(estimate['polygon_um'],float),rtol=0,atol=.01):
        raise AcquisitionError('Rectangle focus support differs from its measured planning outline.')
    h,w=plan['camera_shape'][:2];matrix=np.asarray(plan['transform']['matrix'],float)
    offsets=np.array([[-w/2,-h/2],[-w/2,h/2],[w/2,h/2],[w/2,-h/2]])@matrix.T
    envelope=cv2.convexHull((polygon[:,None,:]+offsets[None,:,:]).reshape(-1,2).astype(np.float32))[:,0,:]
    return {'kind':'same_measured_rectangle_with_camera_footprint','polygon_um':envelope.tolist(),
        'measured_planning_polygon_um':polygon.tolist(),'camera_footprint_offsets_um':offsets.tolist(),
        'coverage_plan_fingerprint':plan.get('fingerprint'),'discovery_sha256':plan.get('discovery_sha256'),
        'qualification':'Conditional planar-chip prediction within this measured chip and its intersecting camera fields; not independent local focus verification.'}


def bounded_plane_prediction(points,x,y,options,*,plan=None,limits=None):
    """Use ALL measurements; conditional uncertainty does not bound chip warp.

    The reported sigma estimates uncertainty of the fitted mean plane, using
    residual standard error plus a measurement floor in quadrature. With only
    four points it is an estimate, not an independently validated confidence
    interval. Unmeasured warp, debris height and material focus remain untested.
    """
    from scipy.spatial import ConvexHull
    values=np.unique(np.asarray(points,float),axis=0)
    if values.ndim!=2 or values.shape[1]!=3 or len(values)<4 or not np.isfinite(values).all():
        raise AcquisitionError('A bounded focus plane requires at least four finite measured XYZ points.')
    origin=values[:,:2].mean(axis=0)
    design=np.column_stack([values[:,:2]-origin,np.ones(len(values))])
    if np.linalg.matrix_rank(design)<3:raise AcquisitionError('Focus plane anchors are collinear.')
    coefficients=np.linalg.lstsq(design,values[:,2],rcond=None)[0]
    residual=values[:,2]-design@coefficients
    if np.max(abs(residual))>options.focus_max_residual_um:
        raise AcquisitionError('Not all measured anchors agree with the permitted focus-plane residual.')
    hull=ConvexHull(values[:,:2]);polygon=values[hull.vertices,:2];target=np.array([x,y],float)
    violations=hull.equations[:,:2]@target+hull.equations[:,2]
    distance=0.
    if np.max(violations)>.002:
        ends=np.roll(polygon,-1,axis=0);segments=ends-polygon
        t=np.clip(np.sum((target-polygon)*segments,axis=1)/np.sum(segments**2,axis=1),0,1)
        distance=float(np.linalg.norm(polygon+t[:,None]*segments-target,axis=1).min())
    diameter=float(np.linalg.norm(polygon[:,None,:]-polygon[None,:,:],axis=2).max())
    limit=min(options.focus_max_extrapolation_um,options.focus_max_extrapolation_fraction*diameter)
    domain=rectangle_plane_domain(plan)
    if domain is not None:
        envelope=np.asarray(domain['polygon_um'],np.float32)
        measured=np.asarray(domain['measured_planning_polygon_um'],np.float32)
        if cv2.pointPolygonTest(envelope,tuple(target),False)<0:
            raise AcquisitionError('Field lies outside this measured rectangle and camera-footprint focus domain.')
        if any(cv2.pointPolygonTest(measured,tuple(point),False)<0 for point in values[:,:2]):
            raise AcquisitionError('Focus anchors do not all belong to this measured rectangle.')
    elif distance>limit:raise AcquisitionError('Field exceeds the bounded focus-plane extrapolation distance.')
    vector=np.r_[target-origin,1.];leverage=float(vector@np.linalg.inv(design.T@design)@vector)
    rms=float(np.sqrt(np.mean(residual**2)));standard_error=float(np.sqrt(np.sum(residual**2)/(len(values)-3)))
    sigma=float(np.sqrt(leverage*(standard_error**2+options.focus_measurement_floor_um**2)))
    if sigma>options.focus_max_prediction_sigma_um:
        raise AcquisitionError('Estimated focus-plane prediction uncertainty exceeds the permitted limit.')
    predicted=float(vector@coefficients)
    if limits is not None:limits.check(Position(float(x),float(y),predicted))
    return {'method':'measured_rectangle_plane_prediction' if domain else 'bounded_measured_plane_extrapolation' if distance else 'measured_surface_interpolation',
        'predicted_z_um':predicted,'focus_verified':False,'local_focus_verified':False,
        'plane_prediction_accepted':True,'extrapolated':bool(distance),'extrapolation_distance_um':distance,
        'extrapolation_limit_um':None if domain else limit,'legacy_extrapolation_limit_um':limit,
        'focus_support_domain':domain,'anchor_count':len(values),'anchor_points_um':values.tolist(),'sample_rms_um':rms,
        'max_anchor_residual_um':float(np.max(abs(residual))),'residual_standard_error_um':standard_error,
        'measurement_floor_um':options.focus_measurement_floor_um,'prediction_leverage':leverage,
        'conditional_prediction_sigma_um':sigma,'uncertainty_kind':'Mean-plane standard-error estimate conditional on a planar chip; not a bound on unmeasured warp or material focus.',
        'thin_flake_focus_validated':False}


def _accept_resume_payload(manifest,payload,fingerprint,allow_source_change=False):
    """Accept an explicit code repair without changing acquisition provenance."""
    if manifest['fingerprint']==fingerprint:return
    previous={key:manifest.get(key) for key in payload}
    old_fingerprint=hashlib.sha256(json.dumps(previous,sort_keys=True).encode()).hexdigest()
    if old_fingerprint!=manifest['fingerprint']:
        raise SafetyError('Full-chip resume manifest provenance fingerprint is invalid.')
    differences=[key for key,value in payload.items()
                 if json.dumps(previous[key],sort_keys=True)!=json.dumps(value,sort_keys=True)]
    if not allow_source_change or differences!=['algorithm_source_sha256']:
        raise SafetyError('Full-chip resume configuration or provenance differs: '+', '.join(differences))
    manifest.setdefault('algorithm_transitions',[]).append({
        'at':time.time(),'from_sha256':previous['algorithm_source_sha256'],
        'to_sha256':payload['algorithm_source_sha256'],'previous_fingerprint':manifest['fingerprint'],
        'new_fingerprint':fingerprint,'completed_tiles_retained':len(manifest.get('completed',{})),
        'authorization':'explicit resume with allow_source_change; all other acquisition provenance identical'})
    manifest['algorithm_source_sha256']=payload['algorithm_source_sha256']
    manifest['fingerprint']=fingerprint


def run_full_chip(scope,plan,reference_frame,folder,*,context=None,prior_scan=None,seed_focus_results=None,resume=False,allow_source_change=False,callback=None,check_cancel=None):
    """Acquire the complete saved plan, recording each field's focus provenance."""
    from .model import CameraTransform
    if not getattr(scope,'armed',False):raise SafetyError('Arm the microscope before a full-chip scan.')
    unsigned={key:value for key,value in plan.items() if key!='fingerprint'}
    if hashlib.sha256(json.dumps(unsigned,sort_keys=True).encode()).hexdigest()!=plan['fingerprint']:
        raise SafetyError('Complete coverage plan was altered after generation.')
    if hashlib.sha256(np.ascontiguousarray(reference_frame.rgb).tobytes()).hexdigest()!=plan['focus_reference_pixels_sha256']:
        raise SafetyError('Focus reference image differs from the reviewed coverage plan.')
    options=FullChipOptions(**plan['options']);transform=CameraTransform(**plan['transform'])
    validate_search_envelope(scope,plan['scan_plan']['bounds_um'])
    if _camera_context(reference_frame)!=plan['acquisition_settings']:raise SafetyError('Reference profile differs from the complete coverage plan.')
    source=Path(plan['discovery_folder'])
    if file_hash(source/'discovery.json')!=plan['discovery_sha256'] or file_hash(source/'evidence.npz')!=plan['evidence_sha256']:
        raise SafetyError('Boundary evidence changed after coverage planning.')
    context=context or {};prior,prior_provenance=load_prior_focus_points(prior_scan,plan,context)
    seeds,seed_provenance=load_seed_focus_points(seed_focus_results,plan,limits=scope.limits)
    search_seeds=(seed_provenance or {}).get('search_seed_points_um',[])
    prior=np.unique(np.asarray(prior+seeds,float),axis=0).tolist() if prior or seeds else []
    payload={'schema':1,'plan':plan['scan_plan'],'transform':plan['transform'],'limits':asdict(scope.limits),
             'context':context,'acquisition_settings':plan['acquisition_settings'],'camera_shape':plan['camera_shape'],
             'source':reference_frame.metadata.get('source','unknown'),'material_model':None,
             'coverage_plan_fingerprint':plan['fingerprint'],'full_chip_options':plan['options'],
             'prior_focus_provenance':prior_provenance,'seed_focus_provenance':seed_provenance,'coverage_kind':plan['kind'],
             'algorithm_source_sha256':file_hash(__file__)}
    fingerprint=hashlib.sha256(json.dumps(payload,sort_keys=True).encode()).hexdigest()
    from .storage import managed_output_path
    folder=managed_output_path(folder);manifest_path=folder/'manifest.json'
    if folder.exists() and any(folder.iterdir()) and not resume:raise ValueError('Select an empty full-chip directory or explicit resume.')
    folder.mkdir(parents=True,exist_ok=True)
    manifest={**payload,'fingerprint':fingerprint,'started_at':time.time(),'completed':{},'state':'running',
              'tile_count':plan['tile_count'],'focus_points_um':prior,'focus_limits':[]}
    if manifest_path.exists():
        manifest=json.loads(manifest_path.read_text())
        _accept_resume_payload(manifest,payload,fingerprint,allow_source_change=bool(resume and allow_source_change))
        manifest['state']='running';manifest.pop('error',None)
    for name,row in manifest['completed'].items():
        for relative,digest in row['hashes'].items():
            if file_hash(folder/relative)!=digest:raise AcquisitionError('Saved complete-field evidence changed: '+relative)
    for relative,digest in manifest.get('focus_anchor_evidence',{}).items():
        if file_hash(folder/relative)!=digest:raise AcquisitionError('Saved focus-anchor evidence changed: '+relative)
    remaining=plan['tile_count']-len(manifest['completed'])
    if shutil.disk_usage(folder).free<remaining*reference_frame.rgb.nbytes*1.3+100_000_000:raise OSError('Insufficient space for the full remaining coverage plan.')
    atomic_json(folder/'coverage-plan.json',plan)
    points=manifest.get('focus_points_um',prior);surface=_strict_surface(points,options.focus_max_residual_um)
    reference=SubstrateReference.from_frame(reference_frame)
    support=np.asarray(plan['support_points_um']);centres=plan['tile_centres_um']
    bx0,bx1,by0,by1=plan['scan_plan']['bounds_um']
    mosaic=Mosaic(plan['scan_plan']['bounds_um'],max(2.,max(reference_frame.pixel_size_um)*4,(bx1-bx0)/3000,(by1-by0)/3000))
    cell=float(plan['support_evidence']['support_cell_um'])
    coverage_evidence=EvidenceMap([bx0-4*cell,bx1+4*cell,by0-4*cell,by1+4*cell],cell)
    boundary_options=BoundaryOptions(coverage_evidence.bounds,cell_um=cell)
    reference_rgb=np.median(reference_frame.rgb.reshape(-1,3),axis=0)
    original_check=scope.check_cancel
    def check():
        original_check()
        if check_cancel:check_cancel()
        if (folder/'STOP').exists():raise Cancelled('Full-chip STOP requested.')
    scope.check_cancel=check
    def save():manifest['updated_at']=time.time();atomic_json(manifest_path,manifest)
    def record_boundary(frame,name,existing=False):
        relative=Path('boundary-labels')/(name+'.npz');path=folder/relative
        if existing:
            with np.load(path) as stored:labels=stored['labels'].copy()
        else:
            labels=classify_boundary(frame,reference,reference_rgb,boundary_options)
            path.parent.mkdir(exist_ok=True);np.savez_compressed(path,labels=labels)
        coverage_evidence.add(labels,[frame.position.x,frame.position.y],frame.rgb.shape,transform)
        return relative
    def save_coverage_evidence():
        np.savez_compressed(folder/'coverage-evidence.npz',flags=coverage_evidence.flags,cell_um=cell,bounds_um=coverage_evidence.bounds)
        measured=coverage_evidence.enclosure(plan['seed_xy_um'])
        manifest['boundary_validation']={'estimate':plan['boundary_estimate'],'acquired_raster_evidence':measured,
            'method':'Registered labels from the scientific raster only; no extra boundary-probe acquisition.',
            'classification_assumption':'Same-profile substrate/holder classification is correct at the recorded evidence resolution.'}
        validated=measured.get('status')=='enclosed' and measured.get('interior_fully_acquired',False)
        manifest['chip_extent_validated']=bool(validated)
        return bool(validated)
    def checkpoint_anchors():
        # The surface helper saves each successful anchor before attempting the
        # next. Retain those measured points even if STOP interrupts the hull.
        report_path=folder/'focus-surface/focus-surface.json'
        if not report_path.exists():return
        report=json.loads(report_path.read_text());known=manifest.setdefault('focus_anchor_evidence',{})
        for anchor in report.get('anchors',[]):
            if anchor.get('status')!='verified':continue
            raw=folder/'focus-surface'/anchor['image'];frame=load_frame(raw)
            if _camera_context(frame)!=plan['acquisition_settings']:raise SafetyError('Saved focus-anchor profile differs.')
            point=[frame.position.x,frame.position.y,frame.position.z]
            if not np.allclose(point,anchor['position_um'],rtol=0,atol=.001):raise AcquisitionError('Focus-anchor position metadata differs.')
            if not any(np.linalg.norm(np.asarray(point)-p)<1e-6 for p in points):points.append(point)
            for path in (raw,raw.with_suffix('.json')):
                relative=path.relative_to(folder).as_posix();digest=file_hash(path)
                if relative in known and known[relative]!=digest:raise AcquisitionError('Focus-anchor evidence changed.')
                known[relative]=digest
        manifest['focus_points_um']=points;save()
    def focus_local(adapter,output):
        from .acquisition_budget import budget_scope
        # Recovery is part of this attempt, not a fresh image/time allowance.
        with budget_scope(adapter,360.,48) as bounded:
            result=autofocus(bounded,options.focus_half_range_um,options.focus_step_um,options.focus_fine_step_um,output,
                             callback=(lambda frame,info:callback('full_chip_focus',frame,info)) if callback else None)
            if not result.success and options.focus_search_bounds_um is not None and 'Peak at search boundary' in result.reason:
                lo,hi=options.focus_search_bounds_um
                if options.adaptive_focus_recovery:
                    result=recover_focus(bounded,lo,hi,Path(output)/'recovery',
                        callback=(lambda frame,info:callback('full_chip_focus',frame,info)) if callback else None)
                else:result=autofocus_range(bounded,lo,hi,options.focus_step_um,options.focus_fine_step_um,Path(output)/'recovery')
        return result
    def finish_local_focus(result,attempt):
        attempt.update(state='verified' if result.success else 'unresolved',result=asdict(result),finished_at=time.time())
        save()
        if not result.success:
            raise FocusSupportError('Local focus unresolved; no final tile acquired: '+result.reason)
    try:
        check()
        if getattr(scope,'lease',None):scope.lease.acquire()
        if remaining:
            initial=scope.capture();transform.validate_frame(initial)
            if initial.rgb.shape!=reference_frame.rgb.shape or _camera_context(initial)!=plan['acquisition_settings']:
                raise SafetyError('Current camera/objective/illumination profile differs from the coverage plan.')
            checkpoint_observation(scope,initial,folder/'preflight','camera_profile_verified',{'acquisition_settings':plan['acquisition_settings']})
        if remaining and (not manifest.get('anchors_prepared') or len(points)<4):
            edge_mode=plan.get('focus_anchor_mode')=='chip_edges'
            adapter=FocusROIAdapter(scope,options.edge_focus_roi_um if edge_mode else options.focus_roi_um,transform,
                                    reference_frame=reference_frame,edge=edge_mode,
                                    edge_proposals=(plan.get('edge_anchor_evidence') or {}).get('anchors') if edge_mode else None)
            # Registration can place the measured edge away from the original
            # stage centre. Reuse that actual point within the same field;
            # never relabel its XYZ as a measurement of the requested centre.
            matching_tolerance=.5
            if edge_mode:
                anchor_xy=np.asarray(plan['anchor_xy_um'],float)
                distances=np.linalg.norm(anchor_xy[:,None,:]-anchor_xy[None,:,:],axis=2)
                distances[distances==0]=np.inf
                h,w=reference_frame.rgb.shape[:2]
                half_field_diagonal=max(np.linalg.norm(np.asarray(transform.matrix)@v)
                                        for v in ([w/2,h/2],[w/2,-h/2]))
                matching_tolerance=min(float(half_field_diagonal),float(distances.min())/3)
            try:
                surface,points=prepare_focus_surface(adapter,plan['anchor_xy_um'],
                    {'half_range_um':options.focus_half_range_um,'coarse_step_um':options.focus_step_um,'fine_step_um':options.focus_fine_step_um,
                     'search_bounds_um':options.focus_search_bounds_um,'max_residual_um':options.focus_max_residual_um,
                     'seed_match_tolerance_um':matching_tolerance,
                     'max_anchor_elapsed_s':360.,'max_anchor_captures':48,
                     'adaptive_recovery':options.adaptive_focus_recovery,'recover_initial_unresolved':True},
                    folder/'focus-surface',callback,seed_points=points,
                    **({'search_seed_points':search_seeds} if search_seeds else {}))
            finally:checkpoint_anchors()
            if edge_mode and (len(points)<4 or surface is None):
                manifest.update(anchors_prepared=False,focus_points_um=points);save()
                raise FocusSupportError('Chip-edge focus surface unresolved; saved anchors retained. Repair/refine the missing or inconsistent support before starting a full raster.')
            manifest.update(anchors_prepared=True,focus_points_um=points);save()
        for index,(x,y) in enumerate(centres):
            check();name=f'tile_{index:06d}'
            if name in manifest['completed']:
                saved=load_frame(folder/'tiles'/(name+'.tiff'));mosaic.add(saved,transform);record_boundary(saved,name,existing=True);continue
            scope.travel(x,y);method=None;focus=None;prediction=None;plane_rejection=None
            try:
                prediction_context={'plan':plan,'limits':scope.limits} if plan.get('boundary_estimate',{}).get('method')=='measured_sides_shared_rectangle_orientation' else {}
                detail=bounded_plane_prediction(points,x,y,options,**prediction_context);prediction=detail['predicted_z_um']
            except AcquisitionError as exc:plane_rejection=str(exc)
            if prediction is not None:
                move_z_bounded(scope,prediction)
            else:
                attempts=manifest.setdefault('local_focus_attempts',[])
                if len(attempts)>=options.max_local_focus_attempts:
                    raise FocusSupportError(f'Exceptional local-focus budget exhausted ({options.max_local_focus_attempts} attempts); refine the focus surface before continuing this chip.')
                attempt={'tile_id':name,'state':'started','started_at':time.time(),'plane_rejection':plane_rejection,
                         'max_elapsed_s':180.,'max_captures':48}
                attempts.append(attempt);save()  # An interrupted attempt still consumes its budget on resume.
                if points:
                    nearest=min(points,key=lambda p:(p[0]-x)**2+(p[1]-y)**2);move_z_bounded(scope,nearest[2])
                preview=scope.capture();roi=supported_roi(preview,reference,options.focus_roi_um,options.minimum_substrate_fraction,transform)
                checkpoint_observation(scope,preview,folder/'focus'/name,'focus_support_selection',{'roi':roi})
                if roi is not None:
                    focus=focus_local(FocusROIAdapter(scope,options.focus_roi_um,transform,roi['box_px'],reference_frame),folder/'focus'/name)
                    finish_local_focus(focus,attempt)
                    detail={'method':'in_field_substrate_roi_autofocus','focus_verified':bool(focus.success),'extrapolated':False,
                            'focus_point_xy_um':roi['point_xy_um'],'roi_source_px':roi['box_px'],'substrate_fraction':roi['substrate_fraction'],
                            'result':asdict(focus)}
                    if focus.success:points.append([*roi['point_xy_um'],focus.z_um])
                else:
                    nearest=support[int(np.argmin(np.linalg.norm(support-[x,y],axis=1)))]
                    distance=float(np.linalg.norm(nearest-[x,y]))
                    if distance>options.max_local_focus_offset_um:
                        raise FocusSupportError(f'No substrate-supported focus ROI or bounded local focus point for {name}; nearest is {distance:.1f} um away.')
                    scope.travel(*nearest)
                    focus=focus_local(FocusROIAdapter(scope,options.focus_roi_um,transform,reference_frame=reference_frame),folder/'focus'/name)
                    finish_local_focus(focus,attempt)
                    if focus.success:points.append([float(nearest[0]),float(nearest[1]),focus.z_um])
                    scope.travel(x,y)
                    detail={'method':'nearby_measured_height','focus_verified':False,'extrapolated':False,
                            'focus_point_xy_um':nearest.tolist(),'offset_um':distance,'local_autofocus_succeeded':bool(focus.success),
                            'result':asdict(focus),'limitation':'Measured at a nearby substrate point; focus at this edge field is not verified.'}
                surface=_strict_surface(points,options.focus_max_residual_um)
                detail['plane_prediction_rejection']=plane_rejection
                detail['local_focus_verified']=bool(detail['focus_verified'])
            detail['thin_flake_focus_validated']=False
            detail['capture_z_target_tolerance_um']=float(getattr(scope,'focus_target_tolerance_um',.25))
            focus_limited=not (detail['focus_verified'] or detail.get('plane_prediction_accepted',False))
            frame=scope.capture();transform.validate_frame(frame)
            if _camera_context(frame)!=plan['acquisition_settings']:raise SafetyError('Camera/objective/illumination profile changed during full coverage.')
            if np.linalg.norm(np.array([frame.position.x,frame.position.y])-[x,y])>.5:raise AcquisitionError('Full-chip tile readback differs from its planned location.')
            frame.metadata['full_chip_focus']=detail;save_frame(folder/'tiles',name,frame)
            vendor_receipt=None
            if hasattr(scope,'ack_persisted_frame'):
                vendor_receipt=scope.ack_persisted_frame(frame,folder/'tiles'/(name+'.tiff'))
            atomic_json(folder/'tiles'/(name+'.flakes.json'),{'flakes':[],'quality':{'raw_acquisition_only':True,'full_chip_focus':detail}})
            paths=[Path('tiles')/(name+suffix) for suffix in ('.tiff','.json','.flakes.json')]
            vendor_archive=Path('tiles')/(name+'.zen-vendor.json')
            if vendor_receipt and vendor_receipt.get('status')=='removed':paths.append(vendor_archive)
            paths.append(record_boundary(frame,name))
            manifest['completed'][name]={'position':asdict(frame.position),'sequence':frame.sequence,
                'focus':asdict(focus) if focus is not None and detail['method']=='in_field_substrate_roi_autofocus' else None,'focus_method':detail['method'],
                'focus_prediction_um':prediction,'full_chip_focus':detail,'focus_limited':focus_limited,
                'vendor_result_retention':vendor_receipt,
                'hashes':{p.as_posix():file_hash(folder/p) for p in paths}}
            if focus_limited:manifest['focus_limits'].append(name)
            manifest['focus_points_um']=points;mosaic.add(frame,transform);save()
            if (index+1)%10==0:mosaic.save(folder)
            if callback:callback('tile',frame,{'index':index+1,'total':len(centres),'focus_method':detail['method'],'focus_limited':focus_limited})
        boundary_valid=save_coverage_evidence()
        state='complete_with_boundary_limits' if not boundary_valid else ('complete_with_focus_limits' if manifest['focus_limits'] else 'complete')
        manifest.update(state=state,raw_coverage_complete=True,planned_area_acquired=True,
                        focus_coverage_complete=not bool(manifest['focus_limits']))
    except BaseException as exc:
        manifest.update(state='stopped' if isinstance(exc,Cancelled) else 'failed',error=str(exc))
        stop_errors=scope.stop()
        if stop_errors:raise AcquisitionError(str(exc)+'; hardware STOP could not be confirmed: '+'; '.join(stop_errors)) from exc
        raise
    finally:
        primary_error=sys.exc_info()[1]
        scope.check_cancel=original_check
        # Persist the terminal state before any potentially large derived write.
        # Disk/RAM failure while exporting an overview must not hide STOP or
        # leave the last durable manifest claiming acquisition is still running.
        finalization_errors=[]
        try:save()
        except Exception as error:finalization_errors.append('terminal_manifest: '+str(error))
        if not finalization_errors:
            try:
                save_coverage_evidence();save()
                if primary_error is None:
                    mosaic.save(folder);atomic_json(folder/'flakes.json',{'flakes':[]});export_csv(folder/'flakes.csv',[])
            except Exception as error:finalization_errors.append('derived_outputs: '+str(error))
        if primary_error is not None:manifest['derived_outputs_pending']=True
        if finalization_errors:
            manifest['finalization_errors']=finalization_errors
            if primary_error is None:manifest.update(state='failed',error='Full-chip finalization failed: '+'; '.join(finalization_errors))
        if finalization_errors or primary_error is not None:
            try:save()
            except Exception:pass  # No successful persistence claim if the device refuses every write.
        if finalization_errors and primary_error is None:
            raise AcquisitionError(manifest['error'])
    return manifest
