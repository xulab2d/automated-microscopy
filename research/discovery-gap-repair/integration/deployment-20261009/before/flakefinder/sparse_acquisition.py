"""Budgeted sparse chip-edge acquisition under an explicit convex-chip model.

Four coarse rays and bracket bisection replace dense boundary following. Coarse
hops can cross an unseen gap: this estimates one separated convex chip and does
not guarantee arbitrary chip discovery. The scientific raster checks its extent.
The caller owns and arms the adapter; importing this module touches no hardware.
"""
from dataclasses import asdict,dataclass
from pathlib import Path
import hashlib,json,math,time

import numpy as np

from .chip_discovery import BoundaryOptions,EvidenceMap,classify_boundary,contained_field_size,file_hash,validate_bounds,validate_search_envelope
from .imaging import save_frame,load_frame
from .layout import SubstrateReference,SUBSTRATE,NON_SUBSTRATE,classify_seed_patch
from .model import AcquisitionError,Cancelled,SafetyError,atomic_json
from .motion import move_z_bounded
from .sparse_edges import coherent_edge,fit_outline


@dataclass
class SparseEdgeOptions:
    search_bounds_um:list[float]
    coarse_step_um:float=1500.
    max_probes:int=32
    cell_um:float=10.
    dark_holder_ratio:float=.15
    margin_um:float=75.
    focus_policy_id:str='unchanged height; geometric evidence only'

    def __post_init__(self):
        self.search_bounds_um=validate_bounds(self.search_bounds_um)
        if not np.isfinite([self.coarse_step_um,self.cell_um,self.dark_holder_ratio,self.margin_um]).all():raise ValueError('Sparse edge parameters must be finite.')
        if self.coarse_step_um<=0 or self.cell_um<=0 or not 1<=self.max_probes<=32 or not 0<=self.dark_holder_ratio<.5 or self.margin_um<=0:
            raise ValueError('Invalid sparse edge settings; the total budget cannot exceed 32 frames.')


def _side(labels):
    h,w=labels.shape;centre=labels[max(0,h//2-3):h//2+4,max(0,w//2-3):w//2+4]
    fractions={'substrate':float(np.mean(labels==SUBSTRATE)),'holder':float(np.mean(labels==NON_SUBSTRATE))}
    if np.mean(centre==SUBSTRATE)>=.8 and fractions['substrate']>=.55:side='substrate'
    elif np.mean(centre==NON_SUBSTRATE)>=.8 and fractions['holder']>=.55:side='holder'
    else:side='unknown'
    return side,fractions


class _Budget(Exception):pass
class _FocusUnresolved(Exception):pass


def _bracket_side(record,allow_fraction=False):
    """A mostly-substrate field can bracket an edge despite a dirty centre.

    This advances an INSIDE search bound only; it never invents a measured edge
    or declares an enclosure. Original per-frame classification is retained.
    """
    side=record['classification'];fractions=record.get('fractions',{})
    if (allow_fraction and side=='unknown' and fractions.get('substrate',0)>=.90
            and fractions.get('holder',1)<=.05):
        record['bracket_inference']={'side':'substrate','method':'whole_field_substrate_fraction_at_least_90_percent_and_holder_at_most_5_percent',
            'qualification':'Search bracket only; centre may contain contamination and no edge is inferred.'}
        return 'substrate'
    return side


def discover_sparse_outline(scope,seed_xy,transform,reference_frame,folder,options,*,context=None,resume=False,continue_unresolved=False,before_capture=None,callback=None,
                            focus_search_bounds_um=None,allow_source_change=False):
    """Save an estimated outline with <=32 geometric probe frames.

    Successful edge frames stop their rays immediately. Holder observations
    bracket the last substrate centre; bisection stops at one third field width.
    At most two lateral checks address contamination in a narrow edge bracket.
    No frontier expansion and no automatic return motion are performed.
    If explicit focus bounds are supplied, one separately bounded edge-focus
    sequence may precede a full-field recapture; all focus images are audited.
    """
    if not isinstance(options,SparseEdgeOptions):options=SparseEdgeOptions(**options)
    if continue_unresolved and not resume:raise ValueError('Targeted unresolved continuation requires explicit resume.')
    if allow_source_change and not resume:raise ValueError('Source transition requires explicit sparse-discovery resume.')
    if not getattr(scope,'armed',False) or scope.limits is None:raise SafetyError('An armed travel envelope is required.')
    if not transform.calibrated:raise SafetyError('A measured camera transform is required.')
    if focus_search_bounds_um is not None:
        focus_search_bounds_um=list(map(float,focus_search_bounds_um))
        if (len(focus_search_bounds_um)!=2 or not np.isfinite(focus_search_bounds_um).all() or
                not scope.limits.z_min<=focus_search_bounds_um[0]<focus_search_bounds_um[1]<=scope.limits.z_max):
            raise SafetyError('Discovery autofocus bounds must remain inside the existing armed Z limits.')
    seed=np.asarray(seed_xy,float)
    if seed.shape!=(2,) or not np.isfinite(seed).all():raise ValueError('A finite verified substrate seed is required.')
    x0,x1,y0,y1=options.search_bounds_um
    if not x0<seed[0]<x1 or not y0<seed[1]<y1:raise ValueError('Seed must lie inside search bounds.')
    validate_search_envelope(scope,options.search_bounds_um)
    reference=SubstrateReference.from_frame(reference_frame);reference_rgb=np.median(reference_frame.rgb.reshape(-1,3),axis=0).tolist()
    field=contained_field_size(reference_frame,transform)
    boundary_options=BoundaryOptions(options.search_bounds_um,options.cell_um,max_probes=options.max_probes,dark_holder_ratio=options.dark_holder_ratio)
    evidence=EvidenceMap(options.search_bounds_um,options.cell_um)
    payload={'schema':1,'algorithm':'sparse_coarse_bracket_halfplanes_v1','algorithm_source_sha256':file_hash(__file__),
        'edge_model_sha256':file_hash(Path(__file__).with_name('sparse_edges.py')),'seed_xy_um':seed.tolist(),
        'options':asdict(options),'context':context or {},'transform':asdict(transform),'source_shape':list(reference_frame.rgb.shape),
        'reference':reference.to_dict(),'reference_rgb':reference_rgb,
        'reference_pixels_sha256':hashlib.sha256(np.ascontiguousarray(reference_frame.rgb).tobytes()).hexdigest(),
        'limits':asdict(scope.limits),'field_rectangle_um':field.tolist(),
        'assumption':'One separated convex chip with approximately straight edges. Coarse hops may cross unobserved gaps.',
        'coverage_kind':'estimated_boundary_not_full_interior_acquisition','all_chips_found':False,
        'coverage_requirement':'Validate extent from scientific-raster border images; expand or replan if substrate reaches its perimeter.'}
    fingerprint=hashlib.sha256(json.dumps(payload,sort_keys=True).encode()).hexdigest()
    folder=Path(folder);destination=folder/'discovery.json'
    if folder.exists() and any(folder.iterdir()) and not resume:raise ValueError('Select an empty sparse edge directory or explicit resume.')
    folder.mkdir(parents=True,exist_ok=True)
    state={**payload,'fingerprint':fingerprint,'state':'running','started_at':time.time(),'records':[],'rays':{}}
    if destination.exists():
        state=json.loads(destination.read_text())
        if state['fingerprint']!=fingerprint:
            expected={k:v for k,v in payload.items() if k!='algorithm_source_sha256'}
            previous={k:state.get(k) for k in expected}
            if not (continue_unresolved or allow_source_change) or json.dumps(previous,sort_keys=True)!=json.dumps(expected,sort_keys=True):
                raise SafetyError('Sparse edge resume configuration/reference differs.')
            state.setdefault('execution_changes',[]).append({'kind':'explicit_source_transition' if allow_source_change else 'explicit_targeted_unresolved_continuation',
                'previous_algorithm_source_sha256':state.get('algorithm_source_sha256'),
                'algorithm_source_sha256':payload['algorithm_source_sha256'],'previous_fingerprint':state['fingerprint'],
                'preserved_frames':len(state['records']),'at':time.time()})
            state.update(algorithm_source_sha256=payload['algorithm_source_sha256'],fingerprint=fingerprint)
        if continue_unresolved:
            state.setdefault('continuations',[]).append({'at':time.time(),'existing_frames':len(state['records']),
                'maximum_total_frames':options.max_probes,'method':'reuse saved rays; advance high-substrate ambiguous bracket; acquire only new bisection/lateral points'})
        state.update(state='running');state.pop('error',None)
    if focus_search_bounds_um is not None:
        policy={'max_attempts':1,'roi_um':64.,'search_bounds_um':focus_search_bounds_um,
                'trigger':'Observed mixed substrate/holder without a coherent line, with a localized green transition.',
                'purpose':'Focus geometric edge evidence before line fitting; not a full-chip focus surface.',
                'probe_budget':'Focused full-field recapture counts toward max_probes; bounded autofocus images are recorded separately.'}
        if state.get('edge_focus_policy') not in (None,policy):raise SafetyError('Saved discovery autofocus policy differs.')
        state['edge_focus_policy']=policy
    by_xy={};edges=[]
    for record in state['records']:
        for relative,digest in record['hashes'].items():
            if file_hash(folder/relative)!=digest:raise AcquisitionError('Saved sparse edge evidence changed: '+relative)
        with np.load(folder/record['labels_file']) as stored:labels=stored['labels'].copy()
        evidence.add(labels,record['position_um'],payload['source_shape'],transform)
        by_xy[tuple(np.round(record['requested_xy_um'],3))]=record
        if record.get('edge'):edges.append(record['edge'])
    for record in state.get('support_records',[]):
        for relative,digest in record['hashes'].items():
            if file_hash(folder/relative)!=digest:raise AcquisitionError('Saved interior support evidence changed: '+relative)
        with np.load(folder/record['labels_file']) as stored:labels=stored['labels'].copy()
        evidence.add(labels,record['position_um'],payload['source_shape'],transform)
    original_check=scope.check_cancel
    def check():
        original_check()
        if (folder/'STOP').exists():raise Cancelled('Sparse edge STOP requested.')
    scope.check_cancel=check
    def save():state['updated_at']=time.time();atomic_json(destination,state)
    def store_frame(frame,xy,phase,**extra):
        transform.validate_frame(frame);key=tuple(np.round(xy,3))
        if list(frame.rgb.shape)!=payload['source_shape'] or np.linalg.norm(np.array([frame.position.x,frame.position.y])-xy)>.5:
            raise AcquisitionError('Sparse edge frame size or stage position differs from the plan.')
        labels=classify_boundary(frame,reference,reference_rgb,boundary_options);side,fractions=_side(labels)
        name=f'probe_{len(state["records"]):06d}';image=f'frames/{name}.tiff'
        edge=coherent_edge(labels,[frame.position.x,frame.position.y],frame.rgb.shape,transform.matrix)
        if edge:
            edge.update(image=image,phase=phase,position_um=[frame.position.x,frame.position.y,frame.position.z])
            edges.append(edge)
        save_frame(folder/'frames',name,frame)
        relative=Path('labels')/(name+'.npz');path=folder/relative;path.parent.mkdir(exist_ok=True)
        temp=path.with_suffix('.tmp.npz');np.savez_compressed(temp,labels=labels);temp.replace(path)
        paths=[Path('frames')/(name+suffix) for suffix in ('.tiff','.json')]+[relative]
        record={'name':name,'phase':phase,'requested_xy_um':list(map(float,xy)),
                'position_um':[frame.position.x,frame.position.y,frame.position.z],'image':image,'labels_file':relative.as_posix(),
                'classification':side,'fractions':fractions,'edge':edge,'hashes':{p.as_posix():file_hash(folder/p) for p in paths},**extra}
        evidence.add(labels,record['position_um'],frame.rgb.shape,transform)
        state['records'].append(record);by_xy[key]=record;save()
        if callback:callback('sparse_edge_probe',frame,record)
        return record
    def focus_if_needed(record,frame=None):
        fractions=record.get('fractions',{})
        if (focus_search_bounds_um is None or state.get('edge_focus_attempts') or record.get('edge') or
                min(fractions.get('substrate',0),fractions.get('holder',0))<.05 or
                len(state['records'])>=options.max_probes):return record
        from .full_chip import edge_focus_roi,FocusROIAdapter
        from .focus_recovery import recover_focus
        frame=frame if frame is not None else load_frame(folder/record['image'])
        # Selection failures are ordinary image evidence; hardware/AF exceptions
        # below are never swallowed as image-classification failures.
        try:selection=edge_focus_roi(frame,64.,transform)
        except AcquisitionError:return record
        check();xy=record['requested_xy_um']
        scope.travel(*xy);move_z_bounded(scope,float(frame.position.z));check()
        focus_folder=folder/'edge-focus'/'attempt_000'
        attempt={'state':'focusing','source_image':record['image'],'requested_xy_um':xy,
                 'initial_z_um':frame.position.z,'search_bounds_um':focus_search_bounds_um,
                 'selection':selection,'folder':'edge-focus/attempt_000','started_at':time.time()}
        state.setdefault('edge_focus_attempts',[]).append(attempt);save()
        adapter=FocusROIAdapter(scope,64.,transform,box=selection['box_px'],reference_frame=reference_frame,edge=True)
        adapter.selection_frame=frame;adapter.selection=selection;adapter.save_focus_selection(focus_folder/'selection')
        def focus_progress(f,sample):
            check()
            if callback:callback('sparse_edge_focus',f,{'source_image':record['image'],**sample})
        try:
            result=recover_focus(adapter,*focus_search_bounds_um,output=focus_folder,callback=focus_progress)
        except BaseException as exc:
            attempt.update(state='stopped' if isinstance(exc,Cancelled) else 'failed',error=str(exc));save();raise
        attempt.update(state='focused' if result.success else 'unresolved',result=asdict(result),finished_at=time.time())
        atomic_json(focus_folder/'focus-result.json',asdict(result))
        attempt['result_sha256']=file_hash(focus_folder/'focus-result.json');save()
        if not result.success:raise _FocusUnresolved(result.reason)
        check();focused=scope.capture();check()
        if abs(focused.position.z-result.z_um)>float(getattr(scope,'focus_target_tolerance_um',.25)):
            raise AcquisitionError('Focused edge recapture differs from the verified focus height.')
        fresh=store_frame(focused,xy,'focused_'+record['phase'],focus_source_image=record['image'],focus_attempt=0)
        attempt.update(focused_image=fresh['image'],focused_position_um=fresh['position_um']);save()
        return fresh
    def acquire(xy,phase):
        check();key=tuple(np.round(xy,3))
        if key in by_xy:
            cached=by_xy[key]
            attempts=state.get('edge_focus_attempts',[])
            if (attempts and attempts[-1].get('state')=='focused' and not cached.get('edge') and
                    min(cached.get('fractions',{}).get('substrate',0),cached.get('fractions',{}).get('holder',0))>=.05 and
                    abs(cached['position_um'][2]-attempts[-1]['result']['z_um'])>float(getattr(scope,'focus_target_tolerance_um',.25))):
                # An old blurred mixed field is not a focused boundary check.
                # Preserve it and use one new geometric probe at measured Z.
                if len(state['records'])>=options.max_probes:raise _Budget()
                scope.travel(*map(float,xy));check()
                return store_frame(scope.capture(),xy,'refocused_'+phase,focus_source_image=cached['image'],focus_attempt=0)
            return focus_if_needed(cached)
        if len(state['records'])>=options.max_probes:raise _Budget()
        if not x0<=xy[0]<=x1 or not y0<=xy[1]<=y1:return None
        scope.travel(float(xy[0]),float(xy[1]));check()
        if before_capture:before_capture(scope,list(map(float,xy)));check()
        frame=scope.capture();record=store_frame(frame,xy,phase)
        return focus_if_needed(record,frame)
    def ray_edge(record,direction):
        edge=record.get('edge') if record else None
        return edge is not None and np.dot(edge['normal'],direction)>.15 and np.dot(edge['normal'],seed)<edge['offset_um']
    try:
        check()
        if getattr(scope,'lease',None):scope.lease.acquire()
        if state.get('edge_focus_attempts'):
            last_focus=state['edge_focus_attempts'][-1]
            if last_focus.get('state')!='focused':raise _FocusUnresolved('Previously interrupted or unresolved discovery focus; no automatic repeat.')
            result_path=folder/last_focus['folder']/'focus-result.json'
            if file_hash(result_path)!=last_focus.get('result_sha256'):raise SafetyError('Saved discovery focus result changed.')
            move_z_bounded(scope,float(last_focus['result']['z_um']));check()
        initial=acquire(seed,'seed')
        with np.load(folder/initial['labels_file']) as stored:
            support=classify_seed_patch(stored['labels'],payload['source_shape'],transform.matrix)
        state['seed_verification']={**support,'patch_diameter_um':40.,'image':initial['image'],
            'qualification':'Local centre patch only; whole-field substrate fraction is not an interior-seed requirement.'}
        if support['classification']!='substrate_seed':
            # An unconfirmed search proposal is ordinary image evidence, not a
            # microscope failure. Keep it unresolved and let the caller search on.
            state.update(state='unresolved',reason='seed_not_verified')
            return state
        for name,direction,maximum,width in [('east',np.array([1.,0]),x1-seed[0],field[0]),
                ('north',np.array([0.,1.]),y1-seed[1],field[1]),('west',np.array([-1.,0]),seed[0]-x0,field[0]),
                ('south',np.array([0.,-1.]),seed[1]-y0,field[1])]:
            check();inside=0.;outside=None;found=None;last=0.;minimum=width/3
            ray={'status':'searching','direction':direction.tolist()};state['rays'][name]=ray;save()
            while last<maximum:
                distance=min(last+options.coarse_step_um,maximum)
                record=acquire(seed+direction*distance,'coarse_'+name);last=distance
                if ray_edge(record,direction):found=record;break
                side=_bracket_side(record,continue_unresolved)
                if side=='holder':outside=distance;break
                if side=='substrate':inside=distance
            if found is None and outside is not None:
                while outside-inside>minimum:
                    midpoint=(inside+outside)/2
                    record=acquire(seed+direction*midpoint,'bisect_'+name)
                    if ray_edge(record,direction):found=record;break
                    side=_bracket_side(record,continue_unresolved)
                    if side=='substrate':inside=midpoint
                    elif side=='holder':outside=midpoint
                    else:break  # Ambiguous data never arbitrarily chooses a bracket side.
                if found is None:
                    tangent=np.array([-direction[1],direction[0]])
                    middle=seed+direction*((inside+outside)/2)
                    for sign in (-1,1):
                        record=acquire(middle+sign*tangent*min(field)*.6,'lateral_'+name)
                        if ray_edge(record,direction):found=record;break
            ray.update(status='coherent_edge' if found else 'unresolved',last_substrate_distance_um=inside,
                first_holder_distance_um=outside,edge_image=found['image'] if found else None)
            save()
    except _Budget:
        state.update(budget_exhausted=True)
    except _FocusUnresolved as exc:
        state.update(state='unresolved',reason='edge_focus_unresolved',focus_failure_reason=str(exc))
    except BaseException as exc:
        state.update(state='stopped' if isinstance(exc,Cancelled) else 'failed',error=str(exc));scope.stop();raise
    finally:
        scope.check_cancel=original_check
        result=fit_outline(edges,seed,options.search_bounds_um,options.margin_um)
        if state.get('reason')=='edge_focus_unresolved':
            result.update(status='unresolved',reason='edge_focus_unresolved',polygon_um=[])
        result.update(evidence={'folder':str(folder.resolve()),'images':len(state['records']),'coherent_edge_images':len(edges)},
                      acquisition_assumption=payload['assumption'],all_chips_found=False)
        state['estimated_outline']=result
        if state['state'] not in ('stopped','failed'):state['state']='estimated' if result['status']=='estimated' else 'unresolved'
        np.savez_compressed(folder/'evidence.npz',flags=evidence.flags,cell_um=evidence.cell,bounds_um=evidence.bounds)
        save();result['evidence']['discovery_sha256']=file_hash(destination);atomic_json(folder/'estimated-outline.json',result)
    return state


def acquire_interior_support(scope,discovery_folder,transform,reference_frame,points_xy,*,callback=None):
    """Add at most four real interior observations to existing boundary evidence.

    These are separately budgeted autofocus-support frames, not new chip-edge
    claims. The caller owns focus height, adapter and campaign STOP checks.
    """
    folder=Path(discovery_folder);destination=folder/'discovery.json'
    state=json.loads(destination.read_text(encoding='utf-8'))
    if state.get('estimated_outline',{}).get('status')!='estimated':raise ValueError('Interior support requires an estimated bounded chip outline.')
    points=np.asarray(points_xy,float)
    if points.ndim!=2 or points.shape[1]!=2 or not 1<=len(points)<=4 or not np.isfinite(points).all():raise ValueError('Supply one to four finite interior support points.')
    if not getattr(scope,'armed',False):raise SafetyError('Interior support acquisition requires an armed owned adapter.')
    reference=SubstrateReference.from_frame(reference_frame)
    if (state['reference']['context']!=reference.context or state['reference_pixels_sha256']!=hashlib.sha256(np.ascontiguousarray(reference_frame.rgb).tobytes()).hexdigest()
            or state['transform']!=json.loads(json.dumps(asdict(transform)))):raise SafetyError('Interior support reference/profile/transform differs from discovery.')
    import cv2
    polygon=np.asarray(state['estimated_outline']['polygon_um'],np.float32)
    if any(cv2.pointPolygonTest(polygon,tuple(map(float,point)),False)<=0 for point in points):raise ValueError('Support points must be strictly inside the estimated chip.')
    bounds=state['options']['search_bounds_um'];validate_search_envelope(scope,bounds)
    if any(not bounds[0]<=p[0]<=bounds[1] or not bounds[2]<=p[1]<=bounds[3] for p in points):raise ValueError('Support point exceeds recorded search bounds.')
    with np.load(folder/'evidence.npz') as saved:
        evidence=EvidenceMap(saved['bounds_um'].tolist(),float(saved['cell_um']));evidence.flags[:]=saved['flags']
    options=BoundaryOptions(bounds,cell_um=state['options']['cell_um'],dark_holder_ratio=state['options']['dark_holder_ratio'])
    records=state.setdefault('support_records',[])
    for record in records:
        for relative,digest in record['hashes'].items():
            if file_hash(folder/relative)!=digest:raise AcquisitionError('Interior support evidence changed: '+relative)
    saved_points=[r['requested_xy_um'] for r in records]
    pending=[p for p in points if not any(np.linalg.norm(p-q)<.01 for q in saved_points)]
    if len(records)+len(pending)>4:raise ValueError('Interior-support budget is four frames per discovered chip.')
    original_check=scope.check_cancel
    def check():
        original_check()
        if (folder/'STOP').exists():raise Cancelled('Discovery support STOP requested.')
    scope.check_cancel=check
    try:
        for point in pending:
            check();scope.travel(*map(float,point));check()
            frame=scope.capture();transform.validate_frame(frame)
            if frame.rgb.shape!=reference_frame.rgb.shape or np.linalg.norm(np.array([frame.position.x,frame.position.y])-point)>.5:
                raise AcquisitionError('Interior support capture position or dimensions differ.')
            labels=classify_boundary(frame,reference,state['reference_rgb'],options);side,fractions=_side(labels)
            name=f'support_{len(records):03d}';save_frame(folder/'support-frames',name,frame)
            relative=Path('support-labels')/(name+'.npz');(folder/relative).parent.mkdir(exist_ok=True)
            np.savez_compressed(folder/relative,labels=labels)
            files=[Path('support-frames')/(name+s) for s in ('.tiff','.json')]+[relative]
            record={'name':name,'phase':'interior_focus_support','requested_xy_um':point.tolist(),
                'position_um':[frame.position.x,frame.position.y,frame.position.z],'classification':side,'fractions':fractions,
                'image':files[0].as_posix(),'labels_file':relative.as_posix(),'hashes':{p.as_posix():file_hash(folder/p) for p in files}}
            evidence.add(labels,record['position_um'],frame.rgb.shape,transform);records.append(record)
            np.savez_compressed(folder/'evidence.npz',flags=evidence.flags,cell_um=evidence.cell,bounds_um=evidence.bounds)
            state['support_policy']='At most four real inset corner captures to spread substrate-supported focus anchors; no inferred substrate pixels.'
            state['support_algorithm_source_sha256']=file_hash(__file__)
            state['updated_at']=time.time();atomic_json(destination,state)
            updated_outline=json.loads(json.dumps(state['estimated_outline']))
            updated_outline.setdefault('evidence',{}).update(discovery_sha256=file_hash(destination),interior_support_images=len(records))
            atomic_json(folder/'estimated-outline.json',updated_outline)
            if callback:callback('interior_focus_support',frame,record)
    except BaseException:
        scope.stop();raise
    finally:scope.check_cancel=original_check
    return state
