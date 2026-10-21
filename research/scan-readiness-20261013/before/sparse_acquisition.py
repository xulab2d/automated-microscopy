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
from .vendor_lifecycle import checkpoint_observation,acknowledge_raw
from .acquisition_budget import AcquisitionBudget,BudgetScope,BudgetExhausted
from .model import AcquisitionError,Cancelled,SafetyError,atomic_json
from .motion import move_z_bounded
from .sparse_edges import validated_coherent_edge,fit_validated_outline
from .photometric_edges import ALGORITHM_VERSION,assess_edge_profile,spatial_bracket_evidence


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
    # The legacy aggregate-only switch no longer bypasses spatial evidence.
    # Original classifications remain intact; inference advances brackets only.
    if record['classification']!='unknown':return record['classification']
    spatial=record.get('spatial_bracket_evidence',{'side':'unknown'})
    if spatial['side'] in ('substrate','holder'):record['bracket_inference']=spatial.copy()
    return spatial['side']


def _gap_probe_direction(lines, seed, bounds, previous=()):
    if len(lines) < 2:
        return None
    angles = sorted(np.mod([np.arctan2(line['normal'][1], line['normal'][0]) for line in lines], 2*np.pi))
    gaps = sorted(((angles[(i+1)%len(angles)] + (2*np.pi if i+1==len(angles) else 0) - a, a)
                   for i, a in enumerate(angles)), reverse=True)
    seed = np.asarray(seed, float)
    for gap, start in gaps:
        # Retry a poorly observed sector with bounded subdivisions, rather than
        # repeating the same ray. These are search directions, not fitted lines.
        for fraction in (.5, .25, .75):
            angle = start + fraction*gap
            direction = np.array([np.cos(angle), np.sin(angle)])
            # A missing-direction probe must escape every measured halfplane.
            # Otherwise it can only rediscover a known edge and spend the scarce
            # residual budget without closing the presently unbounded sector.
            if any(direction @ np.asarray(line['normal'], float) > 1e-9 for line in lines):
                continue
            if any(direction @ np.asarray(old) > np.cos(np.deg2rad(15)) for old in previous):
                continue
            distance = min(((bounds[2*i+(1 if direction[i]>0 else 0)]-seed[i])/direction[i]
                            for i in range(2) if abs(direction[i])>1e-9), default=0)
            if distance > 0:
                return dict(direction=direction.tolist(), maximum_um=float(distance),
                            angular_gap_deg=float(np.degrees(gap)), sector_fraction=fraction)
    return None


def _complete_missing_edge_directions(edges, seed, bounds, field, step, margin,
        acquire, ray_edge, bracket_side, check, save_ray, remaining, fit_model, max_attempts=3):
    """Use spare geometric-probe budget after a failed four-ray outline fit.

    The same live ``edges`` list is extended only by the supplied acquire
    callback. No estimated line, rectangle, moved seed or new safety envelope is
    introduced. A failed ray consumes a direction attempt, not unbounded retries.
    """
    previous = []
    for attempt in range(max_attempts):
        check()
        result = fit_model()
        if result['status']=='estimated' or remaining() <= 0:
            return result
        request = _gap_probe_direction(result.get('lines', []), seed, bounds, previous)
        if request is None:
            return result
        direction = np.asarray(request['direction']); previous.append(direction.tolist())
        maximum = request['maximum_um']; name = 'gap_%03d' % attempt
        ray = dict(status='searching', purpose='missing_measured_halfplane', **request)
        save_ray(name, ray)
        width = min(field[i]/abs(direction[i]) for i in range(2) if abs(direction[i])>1e-9)
        inside = last = 0.; outside = None; found = None
        while last < maximum and remaining() > 0:
            check(); distance = min(last+step, maximum)
            record = acquire(np.asarray(seed)+direction*distance, 'coarse_'+name); last=distance
            if ray_edge(record, direction):found=record;break
            if record is None:break
            side=bracket_side(record)
            if side=='holder':outside=distance;break
            if side=='substrate':inside=distance
        if found is None and outside is not None:
            while outside-inside > width/3 and remaining() > 0:
                check(); midpoint=(inside+outside)/2
                record=acquire(np.asarray(seed)+direction*midpoint, 'bisect_'+name)
                if ray_edge(record,direction):found=record;break
                if record is None:break
                side=bracket_side(record)
                if side=='substrate':inside=midpoint
                elif side=='holder':outside=midpoint
                else:break
            if found is None:
                middle=np.asarray(seed)+direction*((inside+outside)/2)
                tangent=np.array([-direction[1],direction[0]])
                for sign in (-1,1):
                    if remaining() <= 0:break
                    check(); record=acquire(middle+sign*tangent*min(field)*.6,'lateral_'+name)
                    if ray_edge(record,direction):found=record;break
        ray.update(status='coherent_edge' if found else 'unresolved', last_substrate_distance_um=inside,
                   first_holder_distance_um=outside, edge_image=found['image'] if found else None)
        save_ray(name,ray)
    return fit_model()


def discover_sparse_outline(scope,seed_xy,transform,reference_frame,folder,options,*,context=None,resume=False,continue_unresolved=False,before_capture=None,callback=None,
                            focus_search_bounds_um=None,allow_source_change=False,edge_validation_profile=None,
                            max_elapsed_s=600.,max_total_captures=64):
    """Save an estimated outline with <=32 geometric probe frames.

    Successful edge frames stop their rays immediately. Holder observations
    bracket the last substrate centre; bisection stops at one third field width.
    At most two lateral checks address contamination in a narrow edge bracket.
    No frontier expansion and no automatic return motion are performed.
    If explicit focus bounds are supplied, one separately bounded edge-focus
    sequence may precede a full-field recapture; all focus images are audited.
    """
    operation_budget=AcquisitionBudget(max_elapsed_s,max_total_captures)
    scope=BudgetScope(scope,operation_budget)
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
    profile_report=assess_edge_profile(edge_validation_profile,reference_frame)
    payload={'schema':2,'algorithm':ALGORITHM_VERSION,'algorithm_source_sha256':file_hash(__file__),
        'edge_model_sha256':file_hash(Path(__file__).with_name('sparse_edges.py')),
        'photometric_model_sha256':file_hash(Path(__file__).with_name('photometric_edges.py')),
        'edge_validation_profile':edge_validation_profile,'seed_xy_um':seed.tolist(),
        'options':asdict(options),'context':context or {},'transform':asdict(transform),'source_shape':list(reference_frame.rgb.shape),
        'reference':reference.to_dict(),'reference_rgb':reference_rgb,
        'reference_pixels_sha256':hashlib.sha256(np.ascontiguousarray(reference_frame.rgb).tobytes()).hexdigest(),
        'limits':asdict(scope.limits),'field_rectangle_um':field.tolist(),
        'assumption':'One separated convex chip with approximately straight edges. Coarse hops may cross unobserved gaps.',
        'coverage_kind':'estimated_boundary_not_full_interior_acquisition','all_chips_found':False,
        'coverage_requirement':'Validate extent from scientific-raster border images; expand or replan if substrate reaches its perimeter.'}
    fingerprint=hashlib.sha256(json.dumps(payload,sort_keys=True).encode()).hexdigest()
    from .storage import managed_output_path
    folder=managed_output_path(folder);destination=folder/'discovery.json'
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
    if not profile_report['supported']:
        state.update(state='unresolved',reason='unsupported_edge_profile',profile_validation=profile_report,
                     estimated_outline={'status':'unresolved','reason':'unsupported_edge_profile','polygon_um':[]})
        atomic_json(destination,state);atomic_json(folder/'estimated-outline.json',state['estimated_outline'])
        return state
    if focus_search_bounds_um is not None:
        policy={'max_attempts':4,'minimum_separation_um':float(2*min(field)),'roi_um':64.,'search_bounds_um':focus_search_bounds_um,
                'trigger':'Observed mixed substrate/holder without a coherent line, with a localized green transition.',
                'purpose':'Focus geometric edge evidence before line fitting; not a full-chip focus surface.',
                'probe_budget':'Focused full-field recapture counts toward max_probes; bounded autofocus images are recorded separately.'}
        if state.get('edge_focus_policy') not in (None,policy):raise SafetyError('Saved discovery autofocus policy differs.')
        state['edge_focus_policy']=policy
    imported=state.get('imported_records',[])
    if imported:
        # A separate explicit restart session may carry old immutable evidence.
        # Its declared NEW budget counts only newly captured records; old
        # identities and their consumed budgets remain in the import lineage.
        binding=hashlib.sha256(json.dumps(imported,sort_keys=True).encode()).hexdigest()
        if binding!=payload['context'].get('restart_imported_records_sha256'):
            raise SafetyError('Imported sparse observations differ from the fingerprint-bound restart context.')
        if payload['context'].get('restart_placement_status')!='verified_current':
            raise SafetyError('Imported XY evidence is historical; current specimen placement must be independently verified before motion.')
        allowance=payload['context'].get('restart_probe_allowance')
        if allowance is None or not 0<allowance==options.max_probes:
            raise SafetyError('Imported discovery has no matching positive new probe allowance.')
        # Bracket inference annotates its working records. Keep the serialized
        # hash-bound imported objects immutable across cancellation/resume.
        imported=json.loads(json.dumps(imported))
    by_xy={};edges=[];observations=[]
    for record in imported+state['records']:
        for relative,digest in record['hashes'].items():
            if file_hash(folder/relative)!=digest:raise AcquisitionError('Saved sparse edge evidence changed: '+relative)
        with np.load(folder/record['labels_file']) as stored:labels=stored['labels'].copy()
        observations.append((record['image'],labels,record['position_um'][:2]))
        evidence.add(labels,record['position_um'],payload['source_shape'],transform)
        record['spatial_bracket_evidence']=spatial_bracket_evidence(labels,record['classification'])
        by_xy[tuple(np.round(record['requested_xy_um'],3))]=record
        if record.get('edge'):edges.append(record['edge'])
    for record in state.get('support_records',[]):
        for relative,digest in record['hashes'].items():
            if file_hash(folder/relative)!=digest:raise AcquisitionError('Saved interior support evidence changed: '+relative)
        with np.load(folder/record['labels_file']) as stored:labels=stored['labels'].copy()
        observations.append((record['image'],labels,record['position_um'][:2]))
        evidence.add(labels,record['position_um'],payload['source_shape'],transform)
    original_check=scope.check_cancel
    def check():
        original_check()
        if (folder/'STOP').exists():raise Cancelled('Sparse edge STOP requested.')
    scope.check_cancel=check
    def save():
        state['updated_at']=time.time();state['operation_budget']=operation_budget.report();atomic_json(destination,state)
    def fit_model():
        return fit_validated_outline(edges,seed,options.search_bounds_um,options.margin_um,observations,transform.matrix,payload['source_shape'])
    def save_ray(name,ray):state['rays'][name]=ray;save()
    def store_frame(frame,xy,phase,**extra):
        transform.validate_frame(frame);key=tuple(np.round(xy,3))
        if list(frame.rgb.shape)!=payload['source_shape'] or np.linalg.norm(np.array([frame.position.x,frame.position.y])-xy)>.5:
            raise AcquisitionError('Sparse edge frame size or stage position differs from the plan.')
        capture_profile=assess_edge_profile(edge_validation_profile,frame)
        if not capture_profile['supported']:raise AcquisitionError('Sparse edge capture profile changed: '+capture_profile['reason'])
        labels=classify_boundary(frame,reference,reference_rgb,boundary_options);side,fractions=_side(labels)
        name=f'probe_{len(state["records"]):06d}';image=f'frames/{name}.tiff'
        edge,edge_validation=validated_coherent_edge(frame,labels,transform.matrix,edge_validation_profile)
        if edge:
            edge.update(image=image,phase=phase,position_um=[frame.position.x,frame.position.y,frame.position.z])
            edges.append(edge)
        save_frame(folder/'frames',name,frame)
        relative=Path('labels')/(name+'.npz');path=folder/relative;path.parent.mkdir(exist_ok=True)
        temp=path.with_suffix('.tmp.npz');np.savez_compressed(temp,labels=labels);temp.replace(path)
        paths=[Path('frames')/(name+suffix) for suffix in ('.tiff','.json')]+[relative]
        record={'name':name,'phase':phase,'requested_xy_um':list(map(float,xy)),
                'position_um':[frame.position.x,frame.position.y,frame.position.z],'image':image,'labels_file':relative.as_posix(),
                'classification':side,'fractions':fractions,'edge':edge,'edge_validation':edge_validation,
                'spatial_bracket_evidence':spatial_bracket_evidence(labels,side),'hashes':{p.as_posix():file_hash(folder/p) for p in paths},**extra}
        observations.append((image,labels,record['position_um'][:2]))
        evidence.add(labels,record['position_um'],frame.rgb.shape,transform)
        state['records'].append(record);by_xy[key]=record;save()
        checkpoint_observation(scope,frame,folder/'vendor-observations','sparse_edge_probe',record,selected_raw=folder/image)
        if callback:callback('sparse_edge_probe',frame,record)
        return record
    def focus_if_needed(record,frame=None):
        fractions=record.get('fractions',{})
        attempts=state.get('edge_focus_attempts',[])
        local=any(np.linalg.norm(np.asarray(a['requested_xy_um'])-record['requested_xy_um'])<2*min(field) for a in attempts)
        if (focus_search_bounds_um is None or len(attempts)>=4 or local or record.get('edge') or
                min(fractions.get('substrate',0),fractions.get('holder',0))<.05 or
                len(state['records'])>=options.max_probes):return record
        from .full_chip import edge_focus_roi,FocusROIAdapter
        from .focus_recovery import recover_focus
        frame=frame if frame is not None else load_frame(folder/record['image'])
        # Selection failures are ordinary image evidence; hardware/AF exceptions
        # below are never swallowed as image-classification failures.
        try:selection=edge_focus_roi(frame,64.,transform)
        except AcquisitionError as primary_error:
            proposal=record.get('edge_validation',{}).get('original_label_proposal')
            if not proposal:return record
            try:selection=edge_focus_roi(frame,64.,transform,boundary_proposal=proposal)
            except AcquisitionError:return record
            selection['primary_selector_rejection']=str(primary_error)
        check();xy=record['requested_xy_um']
        scope.travel(*xy);move_z_bounded(scope,float(frame.position.z));check()
        attempt_index=len(attempts);focus_relative=f'edge-focus/attempt_{attempt_index:03d}'
        focus_folder=folder/focus_relative
        attempt={'state':'focusing','source_image':record['image'],'requested_xy_um':xy,
                 'initial_z_um':frame.position.z,'search_bounds_um':focus_search_bounds_um,
                  'selection':selection,'folder':focus_relative,'started_at':time.time()}
        state.setdefault('edge_focus_attempts',[]).append(attempt);save()
        adapter=FocusROIAdapter(scope,64.,transform,box=selection['box_px'],reference_frame=reference_frame,edge=True)
        adapter.selection_frame=frame;adapter.selection=selection;adapter.save_focus_selection(focus_folder/'selection')
        # A resolved local peak from the exact continuation parent needs one
        # fresh same-ROI confirmation, not another full exponential search.
        from .focus_checkpoint import find_local_focus,confirmation_ok
        from .autofocus import focus_mask_for_frame,focus_metrics_for_frame,FocusResult
        saved=find_local_focus(folder,context,xy,reference_frame)
        if saved is not None:
            adapter.box=saved['box'];move_z_bounded(scope,saved['result']['z_um']);check()
            full=scope.capture();check();crop=adapter._focus_frame(full)
            measured=focus_metrics_for_frame(crop,focus_mask_for_frame(crop))
            accepted=confirmation_ok(saved,crop,measured,float(getattr(scope,'focus_target_tolerance_um',.25)))
            receipt={k:v for k,v in saved.items() if k not in ('frame','result')}
            receipt.update(accepted=accepted,confirmation={'z_um':crop.position.z,**measured})
            atomic_json(focus_folder/'focus-reuse.json',receipt)
            if accepted:
                result=FocusResult(True,crop.position.z,measured['score'],saved['result']['prominence'],
                    'Pinned same-placement local focus independently reconfirmed.',[receipt['confirmation']])
                save_frame(focus_folder,'verified_focus',crop)
                atomic_json(focus_folder/'focus-result.json',asdict(result))
                attempt.update(state='focused',result=asdict(result),finished_at=time.time(),
                    reused_local_focus=receipt,result_sha256=file_hash(focus_folder/'focus-result.json'));save()
                fresh=store_frame(full,xy,'focused_'+record['phase'],focus_source_image=record['image'],focus_attempt=attempt_index)
                adapter.forget_retention_frame(crop)
                attempt.update(focused_image=fresh['image'],focused_position_um=fresh['position_um']);save()
                return fresh
            checkpoint_observation(adapter,crop,focus_folder,'local_focus_reuse_rejected',receipt)
            adapter.box=selection['box_px']
        def focus_progress(f,sample):
            check()
            if callback:callback('sparse_edge_focus',f,{'source_image':record['image'],**sample})
        partial=find_local_focus(folder,context,xy,reference_frame,partial=True) if saved is None else None
        from .pending_focus import find_pending,confirm_pending
        pending=find_pending(folder,context,xy,reference_frame) if saved is None else None
        try:
            result=None
            if pending is not None:
                adapter.box=pending['box']
                result=confirm_pending(adapter,pending,focus_folder/'pending-confirmation',focus_progress)
                receipt={**pending,'accepted':result.success,'fresh_result':asdict(result)}
                atomic_json(focus_folder/'pending-focus-reuse.json',receipt)
                attempt['partial_search_reuse']=receipt;save()
                if not result.success:result=None
            if result is None and partial is not None:
                from .autofocus import quick_autofocus
                adapter.box=partial['box'];move_z_bounded(scope,partial['result']['z_um']);check()
                actual=scope.position().z
                half=min(2.,actual-partial['bracket_um'][0],partial['bracket_um'][1]-actual)
                receipt={k:v for k,v in partial.items() if k not in ('frame','result')}
                receipt.update(accepted=False,starting_z_um=actual)
                atomic_json(focus_folder/'partial-focus-reuse.json',receipt)
                if half>=max(.75,2*float(getattr(scope,'focus_target_tolerance_um',.25))):
                    result=quick_autofocus(adapter,half,focus_folder/'partial-refinement',focus_progress,
                        fallback_half_range_um=None,max_elapsed_s=180.,max_captures=8)
                    receipt.update(accepted=result.success and result.score>=.75*partial['result']['score'],fresh_result=asdict(result))
                    if not receipt['accepted']:result=None
                atomic_json(focus_folder/'partial-focus-reuse.json',receipt)
                attempt['partial_search_reuse']=receipt;save()
            if result is None:
                adapter.box=selection['box_px']
                result=recover_focus(adapter,*focus_search_bounds_um,output=focus_folder,callback=focus_progress,
                                     max_elapsed_s=360.,max_captures=48)
        except BaseException as exc:
            attempt.update(state='stopped' if isinstance(exc,Cancelled) else 'failed',error=str(exc));save();raise
        attempt.update(state='focused' if result.success else 'unresolved',result=asdict(result),finished_at=time.time())
        atomic_json(focus_folder/'focus-result.json',asdict(result))
        attempt['result_sha256']=file_hash(focus_folder/'focus-result.json');save()
        if not result.success:raise _FocusUnresolved(result.reason)
        check();focused=scope.capture();check()
        if abs(focused.position.z-result.z_um)>float(getattr(scope,'focus_target_tolerance_um',.25)):
            raise AcquisitionError('Focused edge recapture differs from the verified focus height.')
        if (partial is not None or pending is not None) and attempt.get('partial_search_reuse',{}).get('accepted'):
            # The new confirmed result, not the old exploratory peak, is reusable.
            crop=adapter._focus_frame(focused);save_frame(focus_folder,'verified_focus',crop);adapter.forget_retention_frame(crop)
        fresh=store_frame(focused,xy,'focused_'+record['phase'],focus_source_image=record['image'],focus_attempt=attempt_index)
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
        known=[a for a in state.get('edge_focus_attempts',[]) if a.get('state')=='focused']
        if known:
            nearest=min(known,key=lambda a:np.linalg.norm(np.asarray(a['requested_xy_um'])-xy))
            move_z_bounded(scope,float(nearest['result']['z_um']));check()
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
        _complete_missing_edge_directions(edges,seed,options.search_bounds_um,field,
            options.coarse_step_um,options.margin_um,acquire,ray_edge,_bracket_side,check,save_ray,
            lambda:options.max_probes-len(state['records']),fit_model)
        operation_budget.check()
    except BudgetExhausted as exc:
        state.update(state='unresolved',reason=exc.outcome,budget_exhausted=True)
    except _Budget:
        state.update(budget_exhausted=True,reason='geometric_probe_budget_exhausted')
    except _FocusUnresolved as exc:
        state.update(state='unresolved',reason='edge_focus_unresolved',focus_failure_reason=str(exc))
    except BaseException as exc:
        state.update(state='stopped' if isinstance(exc,Cancelled) else 'failed',error=str(exc));scope.stop();raise
    finally:
        scope.check_cancel=original_check
        result=fit_model()
        if state.get('reason') in ('edge_focus_unresolved','time_budget_exhausted','capture_budget_exhausted'):
            result.update(status='unresolved',reason=state['reason'],polygon_um=[])
        if result.get('status')!='estimated':result.setdefault('outcome','edge_not_found')
        else:result['outcome']='estimated_boundary'
        result.update(evidence={'folder':str(folder.resolve()),'images':len(state['records'])+len(imported),
                      'new_probe_images':len(state['records']),'imported_probe_images':len(imported),'coherent_edge_images':len(edges)},
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
            checkpoint_observation(scope,frame,folder/'vendor-observations','interior_support_probe',record,selected_raw=folder/files[0])
            if callback:callback('interior_focus_support',frame,record)
    except BaseException:
        scope.stop();raise
    finally:scope.check_cancel=original_check
    return state
