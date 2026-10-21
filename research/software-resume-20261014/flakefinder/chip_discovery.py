"""Image-evidenced chip enclosures and count-independent search planning.

This module does not assume rectangular chips, a holder grid, or a chip count.
It distinguishes an observed outer enclosure from full interior acquisition and
from proof of physical chip identity. Only a closed measured non-substrate collar
can bound a verified substrate seed. Unknown image regions never close a collar.
"""
from __future__ import annotations

from collections import deque
from dataclasses import asdict,dataclass
from pathlib import Path
import hashlib,json,math,time

import cv2
import numpy as np

from .imaging import save_frame
from .layout import SubstrateReference,SUBSTRATE,NON_SUBSTRATE,UNKNOWN,resize_rgb_float32
from .model import AcquisitionError,Cancelled,Position,SafetyError,atomic_json
from .scan import ScanPlan,tile_centres


def file_hash(path):
    result=hashlib.sha256()
    with Path(path).open('rb') as stream:
        for block in iter(lambda:stream.read(1024*1024),b''):result.update(block)
    return result.hexdigest()


def validate_bounds(values):
    if len(values)!=4 or not np.isfinite(values).all():raise ValueError('Four finite search bounds are required.')
    x0,x1,y0,y1=map(float,values)
    if x0>=x1 or y0>=y1:raise ValueError('Search bounds must increase.')
    return [x0,x1,y0,y1]


def validate_search_envelope(scope,bounds):
    x0,x1,y0,y1=validate_bounds(bounds);current=scope.position()
    approach=float(getattr(scope,'xy_backlash_um',0))
    if not math.isfinite(approach) or approach<0:raise SafetyError('Invalid XY approach distance.')
    for xy in [(x0-approach,y0-approach),(x1,y1)]:scope.limits.check(Position(*xy,current.z))


def contained_field_size(frame,transform):
    """Stage-aligned rectangle guaranteed inside the calibrated camera field."""
    transform.validate_frame(frame)
    inverse=np.linalg.inv(transform.matrix);h,w=frame.rgb.shape[:2];aspect=w/h
    ry=min((w-1)/2/(abs(inverse[0,0])*aspect+abs(inverse[0,1])),
           (h-1)/2/(abs(inverse[1,0])*aspect+abs(inverse[1,1])))
    return np.array([2*aspect*ry,2*ry])


@dataclass
class BoundaryOptions:
    search_bounds_um:list[float]
    cell_um:float=10.
    overlap:float=.20
    max_probes:int=600
    max_evidence_cells:int=12000000
    dark_holder_ratio:float=.15
    mixed_min_fraction:float=.015
    holder_stop_fraction:float=.65
    focus_policy_id:str='unchanged_height; geometric edge evidence only'

    def __post_init__(self):
        self.search_bounds_um=validate_bounds(self.search_bounds_um)
        if not np.isfinite([self.cell_um,self.overlap,self.dark_holder_ratio,self.mixed_min_fraction,self.holder_stop_fraction]).all():
            raise ValueError('Boundary parameters must be finite.')
        if self.cell_um<=0 or not .1<=self.overlap<=.5 or self.max_probes<1 or self.max_evidence_cells<100:
            raise ValueError('Invalid boundary sampling or resource limit.')
        if not 0<=self.dark_holder_ratio<.5 or not 0<self.mixed_min_fraction<.25 or not .5<self.holder_stop_fraction<1:
            raise ValueError('Invalid substrate/holder evidence thresholds.')


class EvidenceMap:
    """Registered tri-state evidence; contradicting observations become unknown."""
    OBSERVED,SUBSTRATE,HOLDER=1,2,4

    def __init__(self,bounds_um,cell_um,max_cells=12000000):
        self.bounds=validate_bounds(bounds_um);self.cell=float(cell_um)
        x0,x1,y0,y1=self.bounds
        shape=(math.ceil((y1-y0)/self.cell),math.ceil((x1-x0)/self.cell))
        if self.cell<=0 or math.prod(shape)>max_cells:raise ValueError('Evidence map exceeds its explicit memory budget; narrow bounds or choose a coarser cell size.')
        self.flags=np.zeros(shape,np.uint8)

    def index(self,xy):
        ij=np.floor((np.asarray(xy)-[self.bounds[0],self.bounds[2]])/self.cell).astype(int)
        if np.any(ij<0) or ij[0]>=self.flags.shape[1] or ij[1]>=self.flags.shape[0]:raise ValueError('Point lies outside the evidence map.')
        return tuple(ij)

    def add(self,labels,position,source_shape,transform):
        if labels.ndim!=2 or not np.isin(labels,[UNKNOWN,SUBSTRATE,NON_SUBSTRATE]).all():raise ValueError('Invalid image labels.')
        h,w=source_shape[:2];mh,mw=labels.shape;matrix=np.asarray(transform.matrix)
        linear=matrix@np.diag([w/mw,h/mh])/self.cell
        source_origin=np.array([(w/mw-1)/2,(h/mh-1)/2])
        stage_origin=np.asarray(position[:2])+matrix@(source_origin-[(w-1)/2,(h-1)/2])
        offset=(stage_origin-[self.bounds[0],self.bounds[2]])/self.cell-.5
        corners=np.array([[0,0],[mw-1,0],[mw-1,mh-1],[0,mh-1]])@linear.T+offset
        left,top=np.maximum(0,np.floor(corners.min(axis=0)-2).astype(int))
        right,bottom=np.minimum([self.flags.shape[1],self.flags.shape[0]],np.ceil(corners.max(axis=0)+3).astype(int))
        if right<=left or bottom<=top:return
        affine=np.column_stack([linear,offset-[left,top]])
        target=self.flags[top:bottom,left:right];shape=(right-left,bottom-top)
        for pixels,bit in [(np.ones_like(labels),self.OBSERVED),(np.uint8(labels==SUBSTRATE),self.SUBSTRATE),(np.uint8(labels==NON_SUBSTRATE),self.HOLDER)]:
            measured=cv2.warpAffine(pixels,affine,shape,flags=cv2.INTER_NEAREST,borderMode=cv2.BORDER_CONSTANT)>0
            target[measured]|=bit

    def enclosure(self,seed_xy):
        """Unknown interior is permitted; unknown boundary never seals a region."""
        seed=self.index(seed_xy)
        s=(self.flags&self.SUBSTRATE)>0;h=(self.flags&self.HOLDER)>0
        if not s[seed[1],seed[0]] or h[seed[1],seed[0]]:
            return {'status':'unresolved','reason':'Seed substrate evidence is absent or contradictory.'}
        # Eight-connected passable pixels conservatively retain diagonal gaps.
        traversable=np.uint8(~(h&~s));cv2.floodFill(traversable,None,seed,2,flags=8)
        region=traversable==2
        if region[0].any() or region[-1].any() or region[:,0].any() or region[:,-1].any():
            return {'status':'unresolved','reason':'No closed observed non-substrate collar; unknown perimeter or search-limit contact remains.'}
        yy,xx=np.nonzero(region);x0,y0=self.bounds[0],self.bounds[2]
        # A bounding rectangle includes uncertain edge cells and arbitrary
        # concavities. Extra holder acquisition is preferable to omitted corners.
        bounds=[x0+max(0,xx.min()-2)*self.cell,x0+min(self.flags.shape[1],xx.max()+3)*self.cell,
                y0+max(0,yy.min()-2)*self.cell,y0+min(self.flags.shape[0],yy.max()+3)*self.cell]
        contours,_=cv2.findContours(region.astype(np.uint8),cv2.RETR_EXTERNAL,cv2.CHAIN_APPROX_SIMPLE)
        contour=max(contours,key=cv2.contourArea)[:,0,:]
        outline=contour*self.cell+[x0+self.cell/2,y0+self.cell/2]
        expanded=cv2.dilate(region.astype(np.uint8),np.ones((5,5),np.uint8))
        expanded_contours,_=cv2.findContours(expanded,cv2.RETR_EXTERNAL,cv2.CHAIN_APPROX_SIMPLE)
        planning=cv2.convexHull(max(expanded_contours,key=cv2.contourArea))[:,0,:]*self.cell+[x0+self.cell/2,y0+self.cell/2]
        observed=(self.flags&self.OBSERVED)>0
        return {'status':'enclosed','kind':'observed_non_substrate_collar','seed_xy_um':list(seed_xy),
                'scan_bounds_um':bounds,'outline_um':outline.tolist(),
                'planning_polygon_um':planning.tolist(),
                'edge_sampling_uncertainty_um':2*self.cell,
                'interior_observed_fraction':float(np.mean(observed[region])),
                'interior_fully_acquired':bool(observed[region].all()),
                'physical_chip_identity':'not established; occlusions, holes and unobserved interior require full raster review',
                'classification_assumption':'Reference-colour substrate and non-substrate labels are correct at this evidence resolution.'}


def classify_boundary(frame,reference,reference_rgb,options):
    labels=reference.classify(frame)
    # Very dark pixels fail the colour-reference valid-exposure gate. They may
    # count as holder only under this explicit, recorded brightness assumption.
    if options.dark_holder_ratio:
        rgb=resize_rgb_float32(frame.rgb,(labels.shape[1],labels.shape[0]))
        dark=(rgb<np.asarray(reference_rgb)*options.dark_holder_ratio).all(axis=2)
        labels[dark]=NON_SUBSTRATE
    return labels


def _statistics(labels,options):
    fractions={'substrate':float(np.mean(labels==SUBSTRATE)),'holder':float(np.mean(labels==NON_SUBSTRATE)),
               'unknown':float(np.mean(labels==UNKNOWN))}
    h,w=labels.shape;centre=labels[max(0,h//2-2):h//2+3,max(0,w//2-2):w//2+3]
    fractions['holder_stop']=bool(fractions['holder']>=options.holder_stop_fraction and np.mean(centre==NON_SUBSTRATE)>=.8)
    fractions['seed_supported']=bool(np.mean(centre==SUBSTRATE)>=.8)
    fractions['boundary_frontier']=bool(fractions['substrate']>=options.mixed_min_fraction and
        (fractions['holder']>=options.mixed_min_fraction or fractions['unknown']>=.05))
    return fractions


def discover_seed_enclosure(scope,seed_xy,transform,reference_frame,folder,options,*,context=None,
                            resume=False,before_capture=None,callback=None,check_cancel=None):
    """Acquire a measured enclosure without operating any unowned hardware.

    The caller supplies an already armed, leased microscope adapter and verified
    substrate seed/reference. ``before_capture(scope, xy)`` may set a measured
    focus height or autofocus; identify that policy in options.focus_policy_id.
    Without it, images are geometric evidence, never focus-validated flake data.
    A STOP file and the adapter's cancellation mechanism are checked throughout.
    All XY motion uses scope.travel; this routine never issues a Z command itself.
    """
    if not isinstance(options,BoundaryOptions):options=BoundaryOptions(**options)
    if not getattr(scope,'armed',False) or scope.limits is None:raise SafetyError('Arm an explicit travel envelope before discovery.')
    if not transform.calibrated:raise SafetyError('Measured camera transform required.')
    seed=np.asarray(seed_xy,float)
    if seed.shape!=(2,) or not np.isfinite(seed).all():raise ValueError('A finite verified substrate seed is required.')
    x0,x1,y0,y1=options.search_bounds_um
    if not x0<seed[0]<x1 or not y0<seed[1]<y1:raise ValueError('Seed must be strictly inside search bounds.')
    validate_search_envelope(scope,options.search_bounds_um)
    field=contained_field_size(reference_frame,transform)
    if options.cell_um>min(field)/12:raise ValueError('Evidence cells are too coarse for this camera field.')
    pitch=field*(1-options.overlap)
    reference=SubstrateReference.from_frame(reference_frame)
    reference_rgb=np.median(reference_frame.rgb.reshape(-1,3),axis=0).tolist()
    settings=asdict(options);settings.pop('max_probes')
    payload={'schema':1,'algorithm':'overlapping_cardinal_frontier_collar_v1','algorithm_source_sha256':file_hash(__file__),
             'seed_xy_um':seed.tolist(),'options':settings,'context':context or {},'transform':asdict(transform),
             'limits':asdict(scope.limits),'reference':reference.to_dict(),'reference_rgb':reference_rgb,
             'reference_pixels_sha256':hashlib.sha256(np.ascontiguousarray(reference_frame.rgb).tobytes()).hexdigest(),
             'source_shape':list(reference_frame.rgb.shape),'pitch_um':pitch.tolist(),'field_rectangle_um':field.tolist()}
    fingerprint=hashlib.sha256(json.dumps(payload,sort_keys=True).encode()).hexdigest()
    folder=Path(folder);destination=folder/'discovery.json'
    if folder.exists() and any(folder.iterdir()) and not resume:raise ValueError('Select an empty discovery directory or explicit resume.')
    folder.mkdir(parents=True,exist_ok=True)
    state={**payload,'fingerprint':fingerprint,'state':'running','started_at':time.time(),'records':[],
           'probe_budget':options.max_probes,'ray_status':{},'coverage_kind':'boundary_evidence_not_full_interior_acquisition'}
    if destination.exists():
        state=json.loads(destination.read_text())
        if state['fingerprint']!=fingerprint:raise SafetyError('Discovery resume configuration/reference differs.')
        if state.get('probe_budget')!=options.max_probes:
            state.setdefault('budget_changes',[]).append({'previous':state.get('probe_budget'),'current':options.max_probes,'after_probes':len(state['records'])})
        state.update(state='running',probe_budget=options.max_probes);state.pop('error',None)
    evidence=EvidenceMap(options.search_bounds_um,options.cell_um,options.max_evidence_cells)
    records={}
    for record in state['records']:
        for relative,digest in record['hashes'].items():
            if file_hash(folder/relative)!=digest:raise AcquisitionError('Saved discovery evidence changed: '+relative)
        labels=np.load(folder/record['labels_file'])['labels']
        evidence.add(labels,record['position_um'],payload['source_shape'],transform)
        records[tuple(record['grid_ij'])]=record
    def save():
        state['updated_at']=time.time();atomic_json(destination,state)
    def check():
        scope.check_cancel()
        if check_cancel:check_cancel()
        if (folder/'STOP').exists():raise Cancelled('Chip discovery STOP requested.')
    def in_bounds(xy):return x0<=xy[0]<=x1 and y0<=xy[1]<=y1
    def acquire(index,phase):
        check();index=tuple(index)
        if index in records:return records[index]
        if len(records)>=options.max_probes:raise _ProbeBudget()
        xy=seed+np.asarray(index)*pitch
        if not in_bounds(xy):return None
        scope.limits.check(Position(*xy,scope.position().z));scope.travel(*xy);check()
        if before_capture:before_capture(scope,xy.tolist());check()
        frame=scope.capture();transform.validate_frame(frame)
        if list(frame.rgb.shape)!=payload['source_shape'] or np.linalg.norm(np.array([frame.position.x,frame.position.y])-xy)>.5:
            raise AcquisitionError('Boundary frame dimensions or stage readback disagree with the plan.')
        labels=classify_boundary(frame,reference,reference_rgb,options)
        name=f'probe_{len(records):06d}';save_frame(folder/'frames',name,frame)
        labels_path=folder/'labels'/(name+'.npz');labels_path.parent.mkdir(exist_ok=True)
        np.savez_compressed(labels_path,labels=labels)
        paths=[Path('frames')/(name+suffix) for suffix in ('.tiff','.json')]+[labels_path.relative_to(folder)]
        record={'name':name,'grid_ij':list(index),'phase':phase,'requested_xy_um':xy.tolist(),
                'position_um':[frame.position.x,frame.position.y,frame.position.z],'labels_file':labels_path.relative_to(folder).as_posix(),
                'image':f'frames/{name}.tiff','hashes':{p.as_posix():file_hash(folder/p) for p in paths},
                'fractions':_statistics(labels,options),'focus_policy_id':options.focus_policy_id}
        evidence.add(labels,record['position_um'],payload['source_shape'],transform)
        records[index]=record;state['records'].append(record);save()
        if callback:callback('boundary_probe',frame,record)
        if len(records)%8==0:
            closed=evidence.enclosure(seed)
            if closed['status']=='enclosed':state.update(state='complete',enclosure=closed);raise _Enclosed()
        return record
    try:
        check()
        if getattr(scope,'lease',None):scope.lease.acquire()
        first=acquire((0,0),'seed')
        if not first['fractions']['seed_supported']:raise AcquisitionError('Seed centre is not verified substrate under the reference profile.')
        closed=evidence.enclosure(seed)
        if closed['status']=='enclosed':state.update(state='complete',enclosure=closed);return state
        # Overlapping fields give direct gap evidence; no exponential blind hops.
        for name,direction in [('east',(1,0)),('north',(0,1)),('west',(-1,0)),('south',(0,-1))]:
            step=1
            while True:
                index=(direction[0]*step,direction[1]*step)
                row=acquire(index,'cardinal_'+name)
                if row is None:state['ray_status'][name]='search_limit_reached';break
                if row['fractions']['holder_stop']:state['ray_status'][name]='observed_non_substrate_field';break
                step+=1
        frontier=set()
        def extend(index):
            for dx in (-1,0,1):
                for dy in (-1,0,1):
                    other=(index[0]+dx,index[1]+dy)
                    if other not in records and in_bounds(seed+np.asarray(other)*pitch):frontier.add(other)
        for index,row in records.items():
            if row['fractions']['boundary_frontier']:extend(index)
        while frontier:
            check();position=scope.position();point=np.array([position.x,position.y])
            index=min(frontier,key=lambda item:(float(np.linalg.norm(seed+np.asarray(item)*pitch-point)),item))
            frontier.remove(index);row=acquire(index,'boundary_frontier')
            if row and row['fractions']['boundary_frontier']:extend(index)
        state['enclosure']=evidence.enclosure(seed)
        state['state']='complete' if state['enclosure']['status']=='enclosed' else 'unresolved'
    except _Enclosed:pass
    except _ProbeBudget:
        state.update(state='unresolved',error='Explicit probe budget exhausted; resume with a larger budget to continue.')
        state['enclosure']=evidence.enclosure(seed)
    except BaseException as exc:
        state.update(state='stopped' if isinstance(exc,Cancelled) else 'failed',error=str(exc))
        scope.stop();raise
    finally:
        np.savez_compressed(folder/'evidence.npz',flags=evidence.flags,cell_um=evidence.cell,bounds_um=evidence.bounds)
        save()
    return state


class _ProbeBudget(Exception):pass
class _Enclosed(Exception):pass


def full_enclosure_scan_plan(discovery,**scan_options):
    """Full polygon-intersecting coverage, including uncertain edge cells.

    Convex coverage may include holder in concavities; no thin substrate sliver
    is omitted. Edge tiles need an explicit focus policy because their centres
    can lie off substrate. An enclosure alone is not a focus-surface calibration.
    """
    if discovery.get('state')!='complete' or discovery.get('enclosure',{}).get('status')!='enclosed':
        raise AcquisitionError('A closed image-evidenced enclosure is required before full coverage planning.')
    scan_options.setdefault('polygon_um',discovery['enclosure']['planning_polygon_um'])
    return ScanPlan(discovery['enclosure']['scan_bounds_um'],**scan_options)


def plan_unknown_count_search(bounds_um,minimum_inscribed_diameter_um,detectable_patch_diameter_um=40.,max_probes=100000):
    """Lattice guarantee for an unoccluded substrate disk, not arbitrary chip area.

    Any disk of the declared minimum diameter fully contained in the search
    bounds contains a survey-centred disk of detectable_patch_diameter. This is
    a geometric guarantee conditional on focus and substrate classification.
    Thin slivers, masked/occluded chips and material unlike the reference are
    explicitly outside that guarantee. The unobserved gaps remain unknown.
    """
    bounds=validate_bounds(bounds_um);diameter=float(minimum_inscribed_diameter_um);patch=float(detectable_patch_diameter_um)
    if not np.isfinite([diameter,patch]).all() or not 0<patch<diameter:raise ValueError('Minimum inscribed diameter must exceed a positive detectable patch diameter.')
    pitch=(diameter-patch)/math.sqrt(2)
    x0,x1,y0,y1=bounds
    nx=max(2,math.ceil((x1-x0)/pitch)+1);ny=max(2,math.ceil((y1-y0)/pitch)+1)
    if type(max_probes) is not int or not 1<=max_probes<=1000000:
        raise ValueError('Search probe budget must be an integer from 1 to 1000000.')
    if nx*ny>max_probes:raise ValueError('Search exceeds explicit probe budget.')
    xs=np.linspace(x0,x1,nx);ys=np.linspace(y0,y1,ny)
    points=[[float(x),float(y)] for row,y in enumerate(ys) for x in (xs if row%2==0 else xs[::-1])]
    return {'schema':1,'kind':'count_independent_substrate_seed_search','bounds_um':bounds,
            'minimum_inscribed_diameter_um':diameter,'detectable_patch_diameter_um':patch,
            'maximum_lattice_pitch_um':pitch,'actual_pitch_um':[float(xs[1]-xs[0]),float(ys[1]-ys[0])],
            'probe_centres_um':points,'probe_count':len(points),'expected_chip_count':None,
            'geometric_guarantee':'Every fully contained unoccluded substrate disk of the declared minimum diameter contains a probe-centred detectable patch.',
            'limits':'Classification/focus must resolve that patch. Slivers, occlusion, unknown materials and chips crossing search bounds are not guaranteed.',
            'coverage_kind':'sampling guarantee; not continuous image coverage or proof all chips were found'}


def discover_substrate_seeds(scope,transform,reference_frame,plan,folder,*,context=None,resume=False,
                             before_capture=None,focus_policy_id='unchanged_height; seed survey only',
                             callback=None,check_cancel=None):
    """Execute a count-independent survey; return ungrouped image-proven seeds.

    Multiple seeds may belong to one chip. Only later measured enclosures/full
    scans may consolidate them. A completed sampling plan is never described as
    complete chip coverage or an unconditional claim that all chips were found.
    """
    expected=plan_unknown_count_search(plan['bounds_um'],plan['minimum_inscribed_diameter_um'],
                                      plan['detectable_patch_diameter_um'],max_probes=max(plan['probe_count'],1))
    if expected!=plan:raise ValueError('Search plan differs from its declared geometric guarantee.')
    if not getattr(scope,'armed',False) or scope.limits is None:raise SafetyError('Arm an explicit envelope before seed discovery.')
    if not transform.calibrated:raise SafetyError('Measured camera transform required.')
    if min(contained_field_size(reference_frame,transform))<=plan['detectable_patch_diameter_um']:
        raise ValueError('Detectable patch must fit inside the camera footprint.')
    validate_search_envelope(scope,plan['bounds_um'])
    reference=SubstrateReference.from_frame(reference_frame)
    payload={'schema':1,'algorithm':'inscribed_disk_lattice_seed_survey_v1','algorithm_source_sha256':file_hash(__file__),
             'plan':plan,'transform':asdict(transform),'limits':asdict(scope.limits),
             'reference':reference.to_dict(),'context':context or {},'focus_policy_id':focus_policy_id,
             'reference_pixels_sha256':hashlib.sha256(np.ascontiguousarray(reference_frame.rgb).tobytes()).hexdigest()}
    fingerprint=hashlib.sha256(json.dumps(payload,sort_keys=True).encode()).hexdigest()
    folder=Path(folder);destination=folder/'seed-survey.json'
    if folder.exists() and any(folder.iterdir()) and not resume:raise ValueError('Select an empty survey directory or explicit resume.')
    folder.mkdir(parents=True,exist_ok=True)
    state={**payload,'fingerprint':fingerprint,'state':'running','records':[],'seeds':[],
           'chip_count':None,'coverage_kind':'conditional_sampling_guarantee_not_continuous_coverage'}
    if destination.exists():
        state=json.loads(destination.read_text())
        if state['fingerprint']!=fingerprint:raise SafetyError('Seed survey resume configuration/reference differs.')
        state.update(state='running');state.pop('error',None)
    for row in state['records']:
        for relative,digest in row['hashes'].items():
            if file_hash(folder/relative)!=digest:raise AcquisitionError('Saved survey evidence changed: '+relative)
    completed={row['index'] for row in state['records']}
    def check():
        scope.check_cancel()
        if check_cancel:check_cancel()
        if (folder/'STOP').exists():raise Cancelled('Substrate seed survey STOP requested.')
    def save():state['updated_at']=time.time();atomic_json(destination,state)
    try:
        check()
        if getattr(scope,'lease',None):scope.lease.acquire()
        for index,xy in enumerate(plan['probe_centres_um']):
            check()
            if index in completed:continue
            scope.travel(*xy);check()
            if before_capture:before_capture(scope,xy);check()
            frame=scope.capture();transform.validate_frame(frame)
            if frame.rgb.shape!=reference_frame.rgb.shape or np.linalg.norm(np.array([frame.position.x,frame.position.y])-xy)>.5:
                raise AcquisitionError('Survey frame dimensions or stage position changed.')
            labels=reference.classify(frame);mh,mw=labels.shape;h,w=frame.rgb.shape[:2]
            yy,xx=np.mgrid[:mh,:mw]
            pixels=np.stack([(xx+.5)*w/mw-w/2,(yy+.5)*h/mh-h/2],axis=-1)
            offsets=pixels@np.asarray(transform.matrix).T
            patch=np.sum(offsets**2,axis=2)<=(plan['detectable_patch_diameter_um']/2)**2
            if patch.sum()<9:raise ValueError('Declared detectable patch has too few classification pixels.')
            substrate=float(np.mean(labels[patch]==SUBSTRATE));holder=float(np.mean(labels[patch]==NON_SUBSTRATE))
            classification='substrate_seed' if substrate>=.9 else ('non_substrate_sample' if holder>=.95 else 'uncertain')
            name=f'survey_{index:06d}';save_frame(folder/'frames',name,frame)
            row={'index':index,'requested_xy_um':xy,'position_um':[frame.position.x,frame.position.y,frame.position.z],
                 'classification':classification,'patch_substrate_fraction':substrate,'patch_non_substrate_fraction':holder,
                 'image':f'frames/{name}.tiff',
                 'hashes':{f'frames/{name}{suffix}':file_hash(folder/'frames'/(name+suffix)) for suffix in ('.tiff','.json')}}
            state['records'].append(row)
            if classification=='substrate_seed':state['seeds'].append({'id':f'seed-{index:06d}','centre_um':[frame.position.x,frame.position.y],
                'status':'substrate_verified','source':row['image'],'grouping':'unresolved; may share a chip with another seed'})
            save()
            if callback:callback('substrate_seed_probe',frame,row)
        state['uncertain_probe_count']=sum(row['classification']=='uncertain' for row in state['records'])
        state['state']='complete_with_uncertainty' if state['uncertain_probe_count'] else 'complete'
        state['all_chips_found']=False
        state['guarantee_status']='Sampling plan executed; minimum unoccluded-disk and reliable-classification assumptions remain explicit.'
    except BaseException as exc:
        state.update(state='stopped' if isinstance(exc,Cancelled) else 'failed',error=str(exc));scope.stop();raise
    finally:save()
    return state
