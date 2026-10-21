"""Interleave fresh-coordinate chip discovery and complete planned acquisition.

Importing or planning touches no hardware. This campaign never loads an old
layout, prior focus scan, or specimen XY position. Analysis is left queued.
"""
from dataclasses import dataclass,asdict,replace
from pathlib import Path
import hashlib,io,json,math,shutil,time,uuid

import cv2,numpy as np

from .chip_discovery import plan_unknown_count_search,validate_bounds,validate_search_envelope,contained_field_size,file_hash,EvidenceMap
from .config import connect,arm,transform_from_config
from .full_chip import FullChipOptions,build_full_chip_plan,run_full_chip,bounded_plane_prediction,load_seed_focus_points
from .imaging import load_frame,save_frame
from .layout import SubstrateReference,SUBSTRATE,NON_SUBSTRATE
from .model import AcquisitionError,FocusSupportError,SafetyError,Cancelled,Limits,atomic_json
from .motion import move_z_bounded
from .sparse_acquisition import SparseEdgeOptions,discover_sparse_outline,acquire_interior_support
from .photometric_edges import ALGORITHM_VERSION,assess_edge_profile
from statistics import median


def interior_reseed(discovery, centres, pending, *, minimum_shift_um):
    """Prioritize one existing lattice sample after an observed open ray hits bounds.

    Only a pending lattice point can be returned. The normal campaign must still
    acquire and classify it. Coarse substrate observations can cross gaps, so this
    is explicitly a search-order heuristic and cannot suppress any other sample.
    """
    seed = discovery.get('seed_xy_um', discovery.get('seed_um'))
    if not seed or len(seed) != 2 or not pending:
        return None
    bounds = discovery.get('options', {}).get('search_bounds_um')
    if not bounds or len(bounds) != 4:
        return None
    exhausted = False
    for ray in discovery.get('rays', {}).values():
        if ray.get('status') != 'unresolved' or ray.get('first_holder_distance_um') is not None:
            continue
        direction = ray.get('direction', [])
        if len(direction) != 2:
            continue
        maximum = min(((bounds[2*i+(1 if direction[i]>0 else 0)]-seed[i])/direction[i]
                       for i in range(2) if abs(direction[i]) > 1e-9), default=0)
        if maximum > 0 and ray.get('last_substrate_distance_um', 0) >= maximum - .5:
            exhausted = True
    if not exhausted:
        return None
    points = [r['position_um'][:2] for r in discovery.get('records', [])
              if r.get('classification') == 'substrate' and len(r.get('position_um', [])) >= 2]
    if len(points) < 3 or not all(math.isfinite(v) for p in points for v in p):
        return None
    target = [median(p[i] for p in points) for i in range(2)]
    index = min(pending, key=lambda j: (sum((centres[j][i]-target[i])**2 for i in range(2)),j))
    xy = centres[index]
    if math.dist(seed, xy) < minimum_shift_um:
        return None
    return {'index': int(index), 'requested_xy_um': list(xy), 'observed_substrate_median_um': target,
            'source_record_count': len(points), 'method': 'observed_substrate_extent_search_order_v1',
            'qualification': 'Unverified next lattice sample; no connected-chip, boundary, or coverage claim.'}


class CampaignBudgetReached(Cancelled):
    pass


@dataclass(frozen=True)
class CampaignStorageOptions:
    """Conservative planning allowances, not a guarantee against other writers."""
    reserve_bytes: int = 10*1024**3
    scan_bytes_per_raw_byte: float = 1.5
    vendor_bytes_per_raw_byte: float = 1.25
    unexpected_local_focus_fraction: float = .05
    guarded_extra_local_focus_attempts: int = 2

    def __post_init__(self):
        if not all(np.isfinite(v) for v in asdict(self).values()):raise ValueError('Storage allowances must be finite.')
        if self.reserve_bytes<0 or min(self.scan_bytes_per_raw_byte,self.vendor_bytes_per_raw_byte)<1:
            raise ValueError('Storage reserve must be nonnegative and raw-byte factors at least one.')
        if not 0<=self.unexpected_local_focus_fraction<=1:raise ValueError('Invalid local-focus storage allowance.')
        if isinstance(self.guarded_extra_local_focus_attempts,bool) or not isinstance(self.guarded_extra_local_focus_attempts,int) or self.guarded_extra_local_focus_attempts<1:
            raise ValueError('Guarded autofocus planning still requires a positive extra local-focus allowance.')


def validate_campaign_envelope(config,bounds_um):
    """Validate configured travel, including negative XY preload, without I/O."""
    bounds=validate_bounds(bounds_um)
    if not config.get('limits'):raise SafetyError('Set specimen-specific XYZ travel limits before starting a campaign.')
    limits=Limits(**config['limits'])
    backlash=float(config.get('zen',{}).get('xy_backlash_um',0)) if config.get('backend')=='zen' else 0.
    if not np.isfinite(backlash) or backlash<0:raise SafetyError('XY backlash must be finite and nonnegative.')
    allowed=[limits.x_min+backlash,limits.x_max,limits.y_min+backlash,limits.y_max]
    if bounds[0]<allowed[0] or bounds[1]>allowed[1] or bounds[2]<allowed[2] or bounds[3]>allowed[3]:
        raise SafetyError(f'Campaign XY bounds including {backlash:g} um negative backlash preload exceed configured limits. '
                          f'Allowed requested envelope is [xmin >= {allowed[0]:.6f}, xmax <= {allowed[1]:.6f}, '
                          f'ymin >= {allowed[2]:.6f}, ymax <= {allowed[3]:.6f}].')
    return {'search_bounds_um':bounds,'negative_backlash_margin_um':backlash,'allowed_requested_bounds_um':allowed}


def _existing_volume(path):
    """Resolve storage for a not-yet-created folder without creating anything."""
    path=Path(path).resolve()
    while not path.exists():path=path.parent
    return str(path.stat().st_dev),path


def estimate_campaign_storage(config,plan,raw_frame_bytes,output,*,geometry=None,options=None,disk_usage=None):
    """Check both raw output and vendor acquisition copies before starting a chip.

    Focus captures are full-camera captures even when autofocus scores a crop.
    Geometry-predicted local sweeps plus an explicit extra fraction are reserved;
    this estimate does not claim every future autofocus outcome is predictable.
    """
    options=options or CampaignStorageOptions(**config.get('campaign_storage',{}))
    disk_usage=disk_usage or shutil.disk_usage
    if not isinstance(raw_frame_bytes,int) or raw_frame_bytes<=0:raise ValueError('Raw frame size must be a positive byte count.')
    focus=FullChipOptions(**plan['options']);tiles=int(plan['tile_count']);anchors=len(plan['anchor_xy_um'])
    if tiles<1:raise ValueError('Storage planning requires at least one tile.')
    geometry=geometry or focus_geometry_diagnostics(plan)
    keep_vendor=bool(config.get('zen',{}).get('keep_vendor_results',True))
    guarded=config.get('backend')=='zen' and bool(config.get('zen',{}).get('job_template')) and not keep_vendor
    extra=options.guarded_extra_local_focus_attempts if guarded else math.ceil(tiles*options.unexpected_local_focus_fraction)
    # The raster stops after this many exceptional local-focus attempts. A
    # synthetic pre-anchor geometry rejection cannot authorize more attempts.
    local=min(tiles,int(geometry['geometry_rejected_tiles'])+extra,focus.max_local_focus_attempts)
    # Seed + coarse/fine sweeps. Include a possible bounded recovery per attempt.
    fine=max(3,math.ceil(2*focus.focus_step_um/focus.focus_fine_step_um)+1)
    per_focus=1+max(3,math.ceil(2*focus.focus_half_range_um/focus.focus_step_um)+1)+fine
    if focus.focus_search_bounds_um is not None:
        if focus.adaptive_focus_recovery:
            # recover_focus uses its own fixed defaults: 2 um coarse, .5 um fine.
            per_focus+=24+2+max(3,math.ceil(6/2)+1)+max(3,math.ceil(4/.5)+1)
        else:
            low,high=focus.focus_search_bounds_um
            per_focus+=1+max(3,math.ceil((high-low)/focus.focus_step_um)+1)+fine
    per_focus+=3 # Textured-ROI preview, mask seed/verification, and anchor record.
    focus_attempts=anchors+local
    vendor_enabled=config.get('backend')=='zen' and bool(config.get('zen',{}).get('job_template'))
    copies=[('scan_and_derived',Path(output),math.ceil((tiles+4*focus_attempts+4)*raw_frame_bytes*options.scan_bytes_per_raw_byte))]
    observation_allowance=0;pending_vendor_allowance=0
    if vendor_enabled:
        if guarded:
            # All AF/preview exports now retire only after durable observation
            # receipts. Retain their bounded JSON/JPG/archive allowance and
            # the existing entire pending-output cap for one in-flight job.
            from .vendor_lifecycle import PENDING_BYTES
            observation_allowance=(focus_attempts*per_focus+5)*4*1024**2
            pending_vendor_allowance=PENDING_BYTES
            copies[0]=(copies[0][0],copies[0][1],copies[0][2]+observation_allowance)
            vendor_bytes=pending_vendor_allowance
        else:
            captures=(tiles if keep_vendor else 1)+focus_attempts*per_focus+5
            vendor_bytes=math.ceil(captures*raw_frame_bytes*options.vendor_bytes_per_raw_byte)
        copies.append(('vendor_job_results',Path(config.get('zen',{}).get('job_output_dir','data/zen-captures')),
                      vendor_bytes))
    volumes={}
    for kind,path,required in copies:
        key,existing=_existing_volume(path)
        if key not in volumes:
            volumes[key]={'volume_id':key,'checked_existing_path':str(existing),'free_bytes':int(disk_usage(existing).free),
                          'estimated_new_bytes':0,'reserve_bytes':int(options.reserve_bytes),'destinations':[]}
        item=volumes[key];item['estimated_new_bytes']+=required
        item['destinations'].append({'kind':kind,'path':str(path.resolve()),'estimated_new_bytes':required})
    for item in volumes.values():
        item['required_free_bytes']=item['estimated_new_bytes']+item['reserve_bytes']
        item['sufficient']=item['free_bytes']>=item['required_free_bytes']
    return {'sufficient':all(v['sufficient'] for v in volumes.values()),'volumes':list(volumes.values()),
            'raw_frame_bytes':raw_frame_bytes,'tile_count':tiles,'anchor_focus_attempts':anchors,
            'geometry_local_focus_attempts':int(geometry['geometry_rejected_tiles']),
            'reserved_local_focus_attempts':local,'full_frame_captures_per_focus_allowance':per_focus,
            'runtime_local_focus_attempt_limit':focus.max_local_focus_attempts,
            'durable_observation_allowance_bytes':observation_allowance,
            'pending_vendor_allowance_bytes':pending_vendor_allowance,
            'options':asdict(options),'vendor_copies_counted':vendor_enabled,
            'keep_vendor_results':keep_vendor,'scientific_vendor_peak_copies':tiles if keep_vendor else 1,
            'planning_policy':'expected_focus_with_per_capture_reserve_guard' if guarded else 'conservative_focus_fraction',
            'per_capture_guard':{'reserve_bytes':10*1024**3,'next_capture_allowance_bytes':512*1024**2} if guarded else None,
            'qualification':'Planning allowance including full-frame vendor results, geometry-predicted and extra autofocus, and free-space reserve; unrelated disk writers or additional focus failures can exceed it.'}


@dataclass(frozen=True)
class CampaignOptions:
    minimum_chip_diameter_um: float
    max_chips: int = 3
    walltime_budget_s: float = 3600.
    detectable_patch_diameter_um: float = 40.
    max_seed_probes: int = 100000
    boundary_radius_um: float = 9000.
    boundary_coarse_step_um: float = 1500.
    boundary_max_probes: int = 32
    boundary_cell_um: float = 10.
    boundary_margin_um: float = 75.
    support_inset_um: float = 500.
    support_trigger_fraction: float = .05
    grid_prior: dict | None = None

    def __post_init__(self):
        if not all(np.isfinite(v) for k,v in asdict(self).items() if k!='grid_prior'):raise ValueError('Campaign parameters must be finite.')
        if self.grid_prior is not None:
            from .grid_search import GridPriorOptions
            if not isinstance(self.grid_prior,dict):raise ValueError('Grid prior must be an options object.')
            object.__setattr__(self,'grid_prior',asdict(GridPriorOptions(**self.grid_prior)))
        if not 0<self.detectable_patch_diameter_um<self.minimum_chip_diameter_um:raise ValueError('Minimum chip diameter must exceed detectable patch diameter.')
        for key in ('max_chips','max_seed_probes','boundary_max_probes'):
            value=getattr(self,key)
            if isinstance(value,bool) or not isinstance(value,int) or value<1:raise ValueError(key+' must be a positive integer.')
        if self.boundary_max_probes>32:raise ValueError('Sparse boundary budget cannot exceed 32 frames.')
        if min(self.walltime_budget_s,self.boundary_radius_um,self.boundary_coarse_step_um,self.boundary_cell_um,self.boundary_margin_um,self.support_inset_um)<=0:
            raise ValueError('Campaign physical and time budgets must be positive.')
        if not 0<=self.support_trigger_fraction<=1:raise ValueError('Invalid support trigger fraction.')


def plan_campaign_search(bounds_um,minimum_chip_diameter_um,start_xy,detectable_patch_diameter_um=40.,max_seed_probes=100000):
    """Pure lattice planning, initially ordered nearest the supplied fresh XY."""
    plan=plan_unknown_count_search(bounds_um,minimum_chip_diameter_um,detectable_patch_diameter_um,max_seed_probes)
    start=np.asarray(start_xy,float)
    if start.shape!=(2,) or not np.isfinite(start).all():raise ValueError('Fresh starting XY must be finite.')
    order=sorted(range(plan['probe_count']),key=lambda i:(float(np.sum((np.asarray(plan['probe_centres_um'][i])-start)**2)),i))
    return {**plan,'initial_nearest_order':order,'start_xy_um':start.tolist(),
        'ordering':'Nearest remaining probe from current position; scan each discovered chip before continuing search.'}


def strictly_inside_outline(point,outline):
    polygon=np.asarray(outline,np.float32)
    return bool(len(polygon)>=3 and cv2.pointPolygonTest(polygon,tuple(map(float,point)),False)>0)


def distinct_measured_chips(outlines):
    """Conservatively group overlapping measured enclosures, never estimates.

    Convex hull overlap can undercount an irregular enclosure, but cannot turn
    two overlapping records of the same measured chip into separate target chips.
    The input is the campaign's evidence-validated measured-outline collection.
    """
    groups=[]
    for key,polygon in outlines:
        hull=cv2.convexHull(np.asarray(polygon,np.float32))
        matches=[i for i,group in enumerate(groups)
                 if any(cv2.intersectConvexConvex(hull,other[2])[0]>0 for other in group)]
        merged=[(key,polygon,hull)]
        for i in reversed(matches):merged.extend(groups.pop(i))
        groups.append(merged)
    return [{'id':group[0][0],'boundary_status':'measured_enclosure',
             'stage_polygon_um':group[0][1],'enclosure_record_ids':[row[0] for row in group]}
            for group in groups]


def observed_rectangle_outline(outline):
    """Four locally measured sides qualify a model, never a predicted grid site."""
    if (not isinstance(outline,dict) or outline.get('status')!='estimated'
            or outline.get('method')!='measured_sides_shared_rectangle_orientation'
            or len(outline.get('lines',[]))!=4
            or any(line.get('edge_point_count',0)<3 or not line.get('source_images') for line in outline['lines'])):return False
    polygon=np.asarray(outline.get('polygon_um',[]),float)
    return bool(polygon.shape==(4,2) and np.isfinite(polygon).all() and cv2.contourArea(polygon.astype(np.float32))>0)


def rectangle_overlap(first,second):
    a=cv2.convexHull(np.asarray(first,np.float32));b=cv2.convexHull(np.asarray(second,np.float32))
    return float(cv2.intersectConvexConvex(a,b)[0])/max(1.,min(cv2.contourArea(a),cv2.contourArea(b)))


def campaign_chip_budget_count(options,chip_states,validated_outlines):
    """Count distinct observed chips for the operational cap, with model qualification."""
    if options.grid_prior is None:return len(chip_states)
    outlines=list(validated_outlines)
    outlines.extend((key,chip['observed_rectangle_outline']['polygon_um']) for key,chip in chip_states.items()
                    if observed_rectangle_outline(chip.get('observed_rectangle_outline')))
    return len(distinct_measured_chips(outlines))


def campaign_grid_observations(state,validated_outlines):
    observations=distinct_measured_chips(validated_outlines)
    polygons=[item['stage_polygon_um'] for item in observations]
    for attempt in state.get('boundary_attempts',[]):
        outline=attempt.get('search_outline')
        if not observed_rectangle_outline(outline):continue
        if any(rectangle_overlap(outline['polygon_um'],polygon)>0 for polygon in polygons):continue
        observations.append({'id':attempt['id'],'boundary_status':'estimated','estimated_outline':outline})
        polygons.append(outline['polygon_um'])
    return observations


def hydrate_observed_rectangles(out,state):
    """Recover model-qualified search anchors from preserved campaign discoveries."""
    for attempt in state.get('boundary_attempts',[]):
        path=Path(attempt['folder'])/'discovery.json'
        if not path.exists():continue
        saved=json.loads(path.read_text(encoding='utf-8'));outline=saved.get('estimated_outline')
        if not observed_rectangle_outline(outline):continue
        key=attempt.get('chip_id');plan_path=out/'plans'/f'{key}.json'
        if key and plan_path.exists():
            plan=json.loads(plan_path.read_text(encoding='utf-8'))
            if plan.get('discovery_sha256')!=file_hash(path):raise SafetyError('Saved rectangle discovery changed since its chip plan.')
        attempt.update(search_outline=outline,search_outline_discovery_sha256=file_hash(path))
        if key in state.get('chip_states',{}):state['chip_states'][key]['observed_rectangle_outline']=outline


def campaign_options_equal(saved,options):
    # New search-order defaults are normalized only for comparison. Explicit
    # source-transition authorization remains independently required.
    return _same_json(asdict(CampaignOptions(**saved)),asdict(options))


def local_boundary_bounds(seed,bounds,radius):
    x0,x1,y0,y1=validate_bounds(bounds);x,y=map(float,seed)
    if not x0<x<x1 or not y0<y<y1:return None
    return [max(x0,x-radius),min(x1,x+radius),max(y0,y-radius),min(y1,y+radius)]


def check_campaign_stop(folder,deadline,clock=time.monotonic):
    if (Path(folder)/'STOP').exists():raise Cancelled('Campaign STOP requested.')
    if clock()>=deadline:raise CampaignBudgetReached('Campaign wall-time budget reached; remaining coverage is unacquired.')


def handoff_monotonic_deadline(out):
    path=Path(out)/'rectangle-handoff-proof.json'
    if not path.exists():return float('inf')
    from .rectangle_handoff import KIND
    proof=json.loads(path.read_text(encoding='utf-8'))
    if proof.get('kind')!=KIND:raise SafetyError('Unrecognized campaign handoff proof.')
    deadline=proof.get('contract',{}).get('deadline_at')
    if type(deadline) not in (float,int) or not math.isfinite(deadline):raise SafetyError('Handoff requires its original absolute deadline.')
    return time.monotonic()+deadline-time.time()


def focus_geometry_diagnostics(plan):
    """Pure geometry-cost estimate; synthetic equal Z is NEVER a focus anchor."""
    options=FullChipOptions(**plan['options'])
    points=[[float(x),float(y),0.] for x,y in plan['anchor_xy_um']]
    rejected=0
    for x,y in plan['tile_centres_um']:
        try:bounded_plane_prediction(points,x,y,options)
        except AcquisitionError:rejected+=1
    return {'geometry_rejected_tiles':rejected,'total_tiles':len(plan['tile_centres_um']),
        'geometry_rejected_fraction':rejected/max(len(plan['tile_centres_um']),1),
        'qualification':'Equal synthetic Z used only to estimate distance/leverage rejection; not measured focus evidence.'}


def interior_support_points(plan,inset_um=500.):
    """Up to four separated polygon corners moved inward; classification follows."""
    from .full_chip import _anchor_points
    polygon=np.asarray(plan['scan_plan']['polygon_um'],float);centre=polygon.mean(axis=0)
    corners=_anchor_points(polygon,4);points=[]
    for vertex in corners:
        direction=centre-vertex;distance=np.linalg.norm(direction)
        point=vertex+direction*min(.5,float(inset_um)/distance)
        if strictly_inside_outline(point,polygon):points.append(point.tolist())
    return points


def seed_connected_coverage(evidence_path,seed_xy,patch_diameter_um=40.):
    """Observed substrate connected to this seed, without inferring chip extent."""
    path=Path(evidence_path)
    with np.load(path) as saved:
        flags=saved['flags'];cell=float(saved['cell_um']);bounds=saved['bounds_um'].tolist()
    good=((flags&EvidenceMap.OBSERVED)>0)&((flags&EvidenceMap.SUBSTRATE)>0)&((flags&EvidenceMap.HOLDER)==0)
    count,components=cv2.connectedComponents(np.uint8(good),connectivity=8)
    sx,sy=np.floor((np.asarray(seed_xy)-[bounds[0],bounds[2]])/cell).astype(int)
    label=int(components[sy,sx]) if 0<=sy<components.shape[0] and 0<=sx<components.shape[1] else 0
    connected=components==label if label else np.zeros(good.shape,bool)
    radius=int(np.ceil(patch_diameter_um/2/cell))
    if radius:connected=cv2.erode(np.uint8(connected),cv2.getStructuringElement(cv2.MORPH_ELLIPSE,(2*radius+1,2*radius+1)),borderType=cv2.BORDER_CONSTANT,borderValue=0).astype(bool)
    return {'mask':connected,'cell_um':cell,'bounds_um':bounds,'source':str(path.resolve()),'source_sha256':file_hash(path),
        'qualification':'Observed noncontradictory substrate connected to the verified seed; does not establish a closed chip extent.',
        'seed_component_observed':bool(label),'detectable_patch_diameter_um':patch_diameter_um}


def point_in_observed_coverage(point,coverage):
    x,y=np.floor((np.asarray(point)-[coverage['bounds_um'][0],coverage['bounds_um'][2]])/coverage['cell_um']).astype(int)
    mask=coverage['mask']
    return bool(0<=y<mask.shape[0] and 0<=x<mask.shape[1] and mask[y,x])


def _seed_classification(frame,reference,transform,diameter):
    from .layout import classify_seed_patch
    return classify_seed_patch(reference.classify(frame),frame.rgb.shape,transform.matrix,diameter_um=diameter)


def _same_json(left,right):
    return json.dumps(left,sort_keys=True)==json.dumps(right,sort_keys=True)


def _configuration_path(out,state):
    path=(out/state.get('configuration_file','configuration.json')).resolve()
    if not path.is_relative_to(out.resolve()):raise SafetyError('Saved configuration revision escapes campaign folder.')
    return path


def _retention_only_change(previous,current):
    """Allow exactly the explicitly requested true/default -> false retention change."""
    if _same_json(previous,current):return False
    allowed=json.loads(json.dumps(previous))
    if previous.get('backend')!='zen' or not previous.get('zen',{}).get('keep_vendor_results',True):
        raise SafetyError('Continuation configuration differs from the saved configuration.')
    allowed.setdefault('zen',{})['keep_vendor_results']=False
    if not _same_json(allowed,current):raise SafetyError('Only zen.keep_vendor_results true/default to false may change on continuation.')
    return True


def restore_campaign_coverage(out,state,transform,*,check=None,progress=None):
    """Verify/replay small registered labels, without decoding old scientific TIFFs."""
    from .rectangle_handoff import _retry_read
    def read_json(path):return _retry_read(lambda:json.loads(path.read_text(encoding='utf-8')),check=check)
    outlines=[];coverages=[];deferred=[];audit=[];last_progress=-float('inf')
    for key,chip in state.get('chip_states',{}).items():
        if check:check()
        from .completed_artifacts import resolve_completed_chip
        folder=resolve_completed_chip(out,state,key,check=check);plan=read_json(out/'plans'/(key+'.json'))
        unsigned={k:v for k,v in plan.items() if k!='fingerprint'}
        if hashlib.sha256(json.dumps(unsigned,sort_keys=True).encode()).hexdigest()!=plan['fingerprint']:
            raise SafetyError('Saved coverage plan fingerprint is invalid: '+key)
        manifest=read_json(folder/'manifest.json')
        if manifest.get('coverage_plan_fingerprint')!=plan['fingerprint']:
            raise SafetyError('Saved coverage manifest differs from its plan: '+key)
        if not _same_json(plan['options'],state['full_chip_options']) or not _same_json(plan['transform'],asdict(transform)):
            raise SafetyError('Completed chip geometry/options differ from campaign: '+key)
        if chip.get('state')=='focus_unresolved':
            # Explicit deferral is a search policy, not observed coverage. Do not
            # repeatedly retry the same modeled chip from another lattice seed.
            deferred.append((key,plan['scan_plan']['polygon_um']))
            audit.append({'chip_id':key,'policy':'deferred_estimated_chip_after_focus_failure',
                          'plan_fingerprint':plan['fingerprint']})
            continue
        if not manifest.get('planned_area_acquired'):
            raise SafetyError('An incomplete chip must be resumed explicitly before discovery continuation: '+key)
        cell=float(plan['support_evidence']['support_cell_um']);x0,x1,y0,y1=plan['scan_plan']['bounds_um']
        evidence=EvidenceMap([x0-4*cell,x1+4*cell,y0-4*cell,y1+4*cell],cell)
        if len(manifest.get('completed',{}))!=plan['tile_count']:raise SafetyError('Completed chip is missing planned fields: '+key)
        recovery=None
        for completed_count,(name,row) in enumerate(manifest['completed'].items(),1):
            if check:check()
            index=int(name.removeprefix('tile_'));xy=plan['tile_centres_um'][index]
            label_relative=f'boundary-labels/{name}.npz';meta_relative=f'tiles/{name}.json'
            def load_registered_field():
                nonlocal recovery
                previous_events=len(recovery.events) if recovery else 0
                try:
                    blobs={}
                    for relative in (label_relative,meta_relative):
                        if not (folder/relative).exists():continue
                        blob=(folder/relative).read_bytes()
                        if row.get('hashes',{}).get(relative)!=hashlib.sha256(blob).hexdigest():
                            raise SafetyError('Saved registered coverage evidence changed: '+str(folder/relative))
                        blobs[relative]=blob
                    if recovery is None and len(blobs)!=2:
                        from .boundary_replay_recovery import BoundaryArtifactRecovery
                        recovery=BoundaryArtifactRecovery(out,key,state,plan,manifest)
                    if meta_relative in blobs:
                        position=json.loads(blobs[meta_relative])['position']
                        position_matches=_same_json(position,row['position'])
                    else:
                        position=recovery.registration(name,row)
                        position_matches=all(abs(position[a]-row['position'][a])<=.01 for a in 'xyz')
                    if not position_matches or np.linalg.norm(np.array([row['position']['x'],row['position']['y']])-xy)>.5:
                        raise SafetyError('Saved registered field position differs: '+name)
                    if label_relative in blobs:
                        with np.load(io.BytesIO(blobs[label_relative]),allow_pickle=False) as saved:return saved['labels'].copy()
                    return recovery.labels(name,row)
                except BaseException:
                    if recovery:del recovery.events[previous_events:]
                    raise
            labels=_retry_read(load_registered_field,check=check)
            evidence.add(labels,[row['position']['x'],row['position']['y']],plan['camera_shape'],transform)
            if check:check()
            now=time.monotonic()
            if progress and (now-last_progress>=1. or completed_count==len(manifest['completed'])):
                progress({'chip_id':key,'index':completed_count,'total':len(manifest['completed']),
                          'operation':'verified_registered_coverage_field','field':name})
                last_progress=now
        evidence_path=folder/'coverage-evidence.npz'
        def verify_aggregate():
            with np.load(io.BytesIO(evidence_path.read_bytes()),allow_pickle=False) as saved:
                if not (np.array_equal(saved['flags'],evidence.flags) and float(saved['cell_um'])==cell and np.array_equal(saved['bounds_um'],evidence.bounds)):
                    raise SafetyError('Stored raster coverage differs from replayed source labels: '+key)
        _retry_read(verify_aggregate,check=check)
        coverage=_retry_read(lambda:seed_connected_coverage(evidence_path,plan['seed_xy_um'],state['options']['detectable_patch_diameter_um']),check=check)
        coverages.append((key,coverage));measured=evidence.enclosure(plan['seed_xy_um'])
        if manifest.get('chip_extent_validated'):
            if measured.get('status')!='enclosed' or not measured.get('interior_fully_acquired'):
                raise SafetyError('Claimed measured chip enclosure cannot be replayed: '+key)
            outlines.append((key,measured['outline_um']))
        audit.append({'chip_id':key,'plan_fingerprint':plan['fingerprint'],'manifest_sha256':_retry_read(lambda:file_hash(folder/'manifest.json'),check=check),
                      'registered_fields_verified':len(manifest['completed']),'coverage_sha256':coverage['source_sha256'],
                      'eligible_observed_cells':int(coverage['mask'].sum()),
                      **({'completed_chip_source':state['completed_chip_sources'][key]} if key in state.get('completed_chip_sources',{}) else {}),
                      **({'missing_artifact_replay':{**recovery.report(),'aggregate_coverage_exact_match':True}} if recovery else {})})
    return outlines,coverages,deferred,audit


def _pending_discoveries(out,state):
    """Recover saved substrate seeds interrupted before their chip was started."""
    pending=[]
    for index,record in enumerate(state.get('seed_records',[])):
        if record.get('classification')!='substrate_seed' or record.get('discovery_status'):continue
        attempt=next((item for item in state.get('boundary_attempts',[]) if item.get('seed_record_index')==index or
                      (item.get('seed_record_index') is None and np.allclose(item['seed_xy_um'],record['requested_xy_um'],rtol=0,atol=.5))),None)
        if attempt and (attempt.get('chip_id') or attempt.get('state') not in ('discovering','estimated','deferred_time_budget','deferred_storage_budget')):continue
        image=out/record['image']
        for relative,digest in record.get('hashes',{}).items():
            if file_hash(out/relative)!=digest:raise SafetyError('Saved pending seed evidence changed: '+relative)
        frame=load_frame(image)
        if not _same_json(asdict(frame.position),record['position_um']):raise SafetyError('Pending seed frame position changed.')
        if attempt and (Path(attempt['folder'])/'STOP').exists():raise Cancelled('Pending sparse-discovery STOP exists; clear deliberately before continuation.')
        pending.append((index,attempt))
    return pending


def _verify_legacy_seed_rejection(out,state,config,reference_frame,transform):
    """Identify the old, seed-only classification exception without relaxing failures.

    This verifies saved acquisition evidence, not the physical stage's current
    state. The explicit recovery flag follows normal owned-adapter arm/readback
    checks; historical STOP success was not recorded by the older campaign.
    """
    message='Seed centre is not verified substrate under this profile.'
    if state.get('state')!='failed' or state.get('error')!=message or state.get('current_chip') is not None:
        raise SafetyError('Recovery applies only to the exact saved seed-classification failure with no active chip.')
    attempts=state.get('boundary_attempts',[])
    if not attempts:raise SafetyError('Seed-classification recovery has no saved boundary attempt.')
    attempt=attempts[-1]
    if attempt.get('state')!='discovering' or attempt.get('chip_id'):
        raise SafetyError('Failed attempt is not an unstarted seed discovery.')
    index=attempt.get('seed_record_index');seeds=state.get('seed_records',[])
    if not isinstance(index,int) or index!=len(seeds)-1:raise SafetyError('Failed attempt is not the last saved seed observation.')
    seed=seeds[index]
    if seed.get('classification')!='substrate_seed' or seed.get('attempt_id')!=attempt.get('id'):
        raise SafetyError('Failed attempt does not match the saved campaign substrate seed.')
    folder=Path(attempt['folder']).resolve()
    if not folder.is_relative_to((out/'discovery').resolve()):raise SafetyError('Failed discovery is outside this campaign.')
    if (folder/'STOP').exists():raise Cancelled('Sparse-discovery STOP exists; recovery will not clear it.')
    path=folder/'discovery.json';discovery=json.loads(path.read_text(encoding='utf-8'))
    records=discovery.get('records',[])
    if (discovery.get('state')!='failed' or discovery.get('error')!=message or len(records)!=1 or
            records[0].get('phase')!='seed' or discovery.get('rays') or discovery.get('support_records')):
        raise SafetyError('Recovery requires exactly one saved seed capture and no boundary/support motion.')
    payload_keys=('schema','algorithm','algorithm_source_sha256','edge_model_sha256','seed_xy_um','options','context',
        'transform','source_shape','reference','reference_rgb','reference_pixels_sha256','limits','field_rectangle_um',
        'assumption','coverage_kind','all_chips_found','coverage_requirement')
    payload={key:discovery.get(key) for key in payload_keys}
    if hashlib.sha256(json.dumps(payload,sort_keys=True).encode()).hexdigest()!=discovery.get('fingerprint'):
        raise SafetyError('Failed discovery payload fingerprint is invalid.')
    if (not _same_json(discovery['transform'],asdict(transform)) or
            not _same_json(discovery['limits'],asdict(Limits(**config['limits']))) or
            not _same_json(discovery['context'],config.get('context',{})) or
            not _same_json(discovery['reference']['context'],state['reference_context']) or
            discovery['source_shape']!=list(reference_frame.rgb.shape) or
            discovery['reference_pixels_sha256']!=hashlib.sha256(np.ascontiguousarray(reference_frame.rgb).tobytes()).hexdigest()):
        raise SafetyError('Failed discovery optical/coordinate provenance differs from the campaign.')
    record=records[0]
    for xy in (attempt['seed_xy_um'],discovery['seed_xy_um'],record['requested_xy_um'],record['position_um'][:2]):
        if not np.allclose(xy,seed['requested_xy_um'],rtol=0,atol=.5):raise SafetyError('Failed seed positions disagree.')
    verified={}
    for base,row,image,label in ((out,seed,seed['image'],None),(folder,record,record['image'],record['labels_file'])):
        required=[image,str(Path(image).with_suffix('.json')).replace('\\','/')]+([label] if label else [])
        for relative in required:
            source=(base/relative).resolve()
            if not source.is_relative_to(base.resolve()) or row.get('hashes',{}).get(relative)!=file_hash(source):
                raise SafetyError('Failed seed evidence changed or lacks a recorded hash: '+relative)
            verified[str(source)]=row['hashes'][relative]
    with np.load(folder/record['labels_file']) as stored:labels=stored['labels']
    if labels.ndim!=2 or min(labels.shape)<7 or not np.isin(labels,[0,SUBSTRATE,NON_SUBSTRATE]).all():
        raise SafetyError('Failed seed labels are invalid.')
    h,w=labels.shape;centre=labels[h//2-3:h//2+4,w//2-3:w//2+4]
    legacy_accepted=bool(np.mean(centre==SUBSTRATE)>=.8 and np.mean(labels==SUBSTRATE)>=.55)
    if legacy_accepted:raise SafetyError('Saved labels do not reproduce the historical seed rejection.')
    return {'at':time.time(),'attempt_id':attempt['id'],'seed_record_index':index,'previous_error':message,
        'discovery_sha256':file_hash(path),'discovery_fingerprint':discovery['fingerprint'],'verified_seed_hashes':verified,
        'authorization':'explicit recover_seed_rejection with continue_discovery and allow_source_change',
        'scope':'Replay this exact saved seed under repaired classification; preserve all files and original active-time budget.',
        'historical_stop_success_recorded':False}


def _prepare_continuation(config,reference_path,reference_frame,transform,out,bounds,options,full_options,search,
                          *,allow_source_change=False,max_chips=None,additional_seconds=None,recover_seed_rejection=False,
                          check=None,progress=None):
    """Read-only preflight. No campaign mutation or adapter connection here."""
    check_campaign_stop(out,float('inf'))
    state=json.loads((out/'batch.json').read_text(encoding='utf-8'))
    layout=json.loads((out/'layout.json').read_text(encoding='utf-8'))
    recovery=None
    if recover_seed_rejection:
        if not allow_source_change or max_chips is not None or additional_seconds is not None:
            raise SafetyError('Seed recovery requires allow_source_change and preserves the saved budget without overrides.')
        recovery=_verify_legacy_seed_rejection(out,state,config,reference_frame,transform)
    if not recovery and state.get('state') not in ('chip_complete_discovery_pending','stopped','time_budget_reached','storage_budget_reached','chip_budget_reached','search_sampling_complete'):
        raise SafetyError('Discovery continuation requires a stopped or completed saved campaign, never an active/failed run.')
    if state.get('current_chip'):raise SafetyError('Resume the interrupted current chip before continuing discovery.')
    if layout.get('coordinate_frame_id')!=state.get('coordinate_frame_id'):raise SafetyError('Campaign coordinate-frame provenance differs.')
    previous_config_path=_configuration_path(out,state)
    previous_config=json.loads(previous_config_path.read_text(encoding='utf-8'))
    retention_changed=_retention_only_change(previous_config,config)
    if not campaign_options_equal(state['options'],options) or not _same_json(state['search_bounds_um'],bounds):
        raise SafetyError('Continuation geometry/options differ; use explicit continuation budget overrides only.')
    if not _same_json(state['full_chip_options'],asdict(full_options)):raise SafetyError('Continuation full-chip options differ.')
    if not _same_json(json.loads((out/'search-plan.json').read_text(encoding='utf-8')),search):
        raise SafetyError('Saved search lattice differs from fresh-coordinate plan.')
    if (str(reference_path)!=state['reference_path'] or file_hash(reference_path)!=state['reference_sha256'] or
        not _same_json(asdict(reference_frame.position),state['reference_position_um']) or
        not _same_json(SubstrateReference.from_frame(reference_frame).context,state['reference_context'])):
        raise SafetyError('Continuation reference/profile/coordinate evidence changed.')
    for path_key,hash_key in (('evidence_path','evidence_sha256'),('image','image_sha256')):
        focus=state.get('seed_focus',{})
        if focus.get(path_key) and file_hash(focus[path_key])!=focus[hash_key]:raise SafetyError('Saved starting focus evidence changed.')
    if state.get('seed_focus',{}).get('metadata_sha256') and file_hash(Path(state['seed_focus']['image']).with_suffix('.json'))!=state['seed_focus']['metadata_sha256']:
        raise SafetyError('Saved starting focus image metadata changed.')
    previous_source=state.get('algorithm_source_sha256');source=file_hash(__file__)
    if previous_source!=source and not allow_source_change:raise SafetyError('Campaign source changed; explicit allow_source_change is required.')
    seed_z=state.get('seed_focus',{}).get('z_um',float('nan'))
    if not np.isfinite(seed_z) or not config['limits']['z_min']<=seed_z<=config['limits']['z_max']:
        raise SafetyError('Saved discovery focus height is outside the unchanged travel envelope.')
    elapsed=float(state.get('elapsed_seconds',0))
    if not np.isfinite(elapsed) or elapsed<0:raise SafetyError('Invalid saved active elapsed time.')
    revised=replace(options,max_chips=options.max_chips if max_chips is None else max_chips,
                    walltime_budget_s=options.walltime_budget_s if additional_seconds is None else elapsed+additional_seconds)
    if additional_seconds is not None and (not np.isfinite(additional_seconds) or additional_seconds<=0):raise ValueError('Continuation duration must be positive.')
    completed_indices=[]
    for record in state.get('seed_records',[]):
        index=record.get('index')
        if index is None:continue
        if not isinstance(index,int) or not 0<=index<search['probe_count'] or not np.allclose(record['requested_xy_um'],search['probe_centres_um'][index],rtol=0,atol=1e-8):
            raise SafetyError('Saved seed index/location differs from the search lattice.')
        completed_indices.append(index)
    if len(set(completed_indices))!=len(completed_indices):raise SafetyError('Duplicate completed lattice indices in campaign.')
    pending=set(range(search['probe_count']))-set(completed_indices)
    if 'remaining_lattice_indices' in state and set(state['remaining_lattice_indices'])!=pending:
        raise SafetyError('Saved remaining lattice indices disagree with recorded probes.')
    outlines,coverages,deferred,audit=restore_campaign_coverage(out,state,transform,check=check,progress=progress)
    _pending_discoveries(out,state) # Validate any unfinished seed before the first write/connection.
    stamp=time.time()
    if recovery:
        state.setdefault('classification_recoveries',[]).append(recovery)
        state['boundary_attempts'][-1]['seed_rejection_recovery']=recovery
    if previous_source!=source:
        state.setdefault('algorithm_transitions',[]).append({'at':stamp,'from_sha256':previous_source,'to_sha256':source,
            'authorization':'explicit continue_discovery with allow_source_change; geometry/profile/reference unchanged'})
    if revised!=options:
        state.setdefault('budget_transitions',[]).append({'at':stamp,'from_options':asdict(options),'to_options':asdict(revised),
            'active_elapsed_seconds_at_transition':elapsed,'additional_active_seconds':additional_seconds,
            'authorization':'explicit continuation budget override'})
    revision=None
    if retention_changed:
        digest=hashlib.sha256(json.dumps(config,sort_keys=True).encode()).hexdigest()
        revision='configuration-revisions/'+digest+'.json'
        state.setdefault('configuration_transitions',[]).append({'at':stamp,'from_file':str(previous_config_path.relative_to(out)),
            'from_sha256':file_hash(previous_config_path),'to_file':revision,'change':{'zen.keep_vendor_results':False},
            'scope':'future captures only; existing raw data and vendor results unchanged'})
        state['configuration_file']=revision
    state.update(state='running',phase='continuing_discovery',options=asdict(revised),algorithm_source_sha256=source)
    state.pop('error',None)
    state.setdefault('continuation_attempts',[]).append({'at':stamp,'active_elapsed_seconds_at_start':elapsed,
        'remaining_lattice_probes':len(pending),'coverage_replay':audit,'configuration_file':state.get('configuration_file','configuration.json')})
    return state,layout,revised,pending,outlines,coverages,deferred,revision


def _resume_campaign_chip(config,reference_tiff,output,bounds,options,*,full_chip_options=None,
                          initial_discovery=None,seed_focus_z_um=None,seed_focus_evidence=None,seed_focus_frame=None,
                          seed_focus_results=None,scope=None,callback=None,allow_source_change=False):
    """Resume the saved interrupted chip in place; leave later discovery pending.

    This deliberately does not reconstruct a partially executed lattice search.
    Only an existing chip plan/manifest is continued, preserving its exact
    geometry, optics, limits, discovery and evidence. No files are archived.
    """
    from . import full_chip
    out=Path(output).resolve();resume_deadline=handoff_monotonic_deadline(out);check_campaign_stop(out,resume_deadline)
    state=json.loads((out/'batch.json').read_text(encoding='utf-8'))
    layout=json.loads((out/'layout.json').read_text(encoding='utf-8'))
    saved_config=json.loads(_configuration_path(out,state).read_text(encoding='utf-8'))
    if state.get('state') not in ('stopped','failed','storage_budget_reached'):
        raise SafetyError('Campaign resume requires a stopped/failed saved chip; running or completed campaigns are not resumed implicitly.')
    if not _same_json(saved_config,config):raise SafetyError('Campaign resume configuration differs from the saved configuration.')
    if not campaign_options_equal(state['options'],options) or not _same_json(state['search_bounds_um'],bounds):
        raise SafetyError('Campaign resume options or explicit native search bounds differ.')
    full=full_chip_options if full_chip_options is not None else config.get('full_chip',{})
    full=full if isinstance(full,FullChipOptions) else FullChipOptions(**full)
    if not _same_json(state['full_chip_options'],asdict(full)):raise SafetyError('Campaign resume full-chip options differ.')
    if layout.get('coordinate_frame_id')!=state.get('coordinate_frame_id'):
        raise SafetyError('Saved campaign and layout coordinate frames differ.')
    if initial_discovery is not None and str(Path(initial_discovery).resolve())!=state.get('initial_discovery'):
        raise SafetyError('Resume initial discovery differs from the saved campaign.')
    key=state.get('current_chip')
    if not key or key not in state.get('chip_states',{}):raise SafetyError('No interrupted current chip is saved for in-place resume.')
    if key in state.get('completed_chip_sources',{}):raise SafetyError('A read-only completed chip cannot be resumed for acquisition.')
    folder=out/key
    if (folder/'STOP').exists():raise Cancelled('Chip STOP exists; clear it deliberately before resuming.')
    plan=json.loads((out/'plans'/(key+'.json')).read_text(encoding='utf-8'))
    manifest=json.loads((folder/'manifest.json').read_text(encoding='utf-8'))
    unsigned={k:v for k,v in plan.items() if k!='fingerprint'}
    if hashlib.sha256(json.dumps(unsigned,sort_keys=True).encode()).hexdigest()!=plan['fingerprint']:
        raise SafetyError('Saved campaign coverage plan fingerprint is invalid.')
    if manifest['coverage_plan_fingerprint']!=plan['fingerprint'] or not _same_json(plan['options'],state['full_chip_options']):
        raise SafetyError('Saved chip plan differs from its manifest or campaign options.')
    reference_path=Path(reference_tiff).resolve()
    if str(reference_path)!=state['reference_path'] or file_hash(reference_path)!=state['reference_sha256']:
        raise SafetyError('Campaign resume reference path or raw image hash differs.')
    reference_frame=load_frame(reference_path);transform=transform_from_config(config);transform.validate_frame(reference_frame)
    if not _same_json(asdict(reference_frame.position),state['reference_position_um']):raise SafetyError('Campaign reference coordinate metadata changed.')
    if not _same_json(SubstrateReference.from_frame(reference_frame).context,state['reference_context']):
        raise SafetyError('Campaign reference optical profile changed.')
    if hashlib.sha256(np.ascontiguousarray(reference_frame.rgb).tobytes()).hexdigest()!=plan['focus_reference_pixels_sha256']:
        raise SafetyError('Campaign reference pixels differ from the saved chip plan.')
    discovery=Path(plan['discovery_folder'])
    if file_hash(discovery/'discovery.json')!=plan['discovery_sha256'] or file_hash(discovery/'evidence.npz')!=plan['evidence_sha256']:
        raise SafetyError('Campaign discovery evidence changed since the coverage plan.')
    focus=state.get('seed_focus',{})
    if seed_focus_z_um is not None and abs(float(seed_focus_z_um)-focus.get('z_um',float('inf')))>.5:
        raise SafetyError('Resume cannot replace the saved discovery starting height.')
    for value,path_key,hash_key in ((seed_focus_evidence,'evidence_path','evidence_sha256'),(seed_focus_frame,'image','image_sha256')):
        if value is not None and (str(Path(value).resolve())!=focus.get(path_key) or file_hash(value)!=focus.get(hash_key)):
            raise SafetyError('Resume discovery-focus evidence differs from the saved campaign.')
    saved_bundle=state.get('seed_focus_bundle_provenance')
    if seed_focus_results is None:seed_focus_results=(saved_bundle or {}).get('records')
    _,bundle=load_seed_focus_points(seed_focus_results,{
        **plan,'scan_plan':{'polygon_um':[[bounds[0],bounds[2]],[bounds[1],bounds[2]],[bounds[1],bounds[3]],[bounds[0],bounds[3]]]}
        },limits=Limits(**config['limits']))
    if not _same_json(bundle,saved_bundle):raise SafetyError('Resume focus seed bundle differs from the saved campaign.')
    # Resume the exact saved chip seed provenance, including typed discovery
    # ROI seeds. Never inject newly discovered evidence into an old fingerprint.
    chip_seeds=(manifest.get('seed_focus_provenance') or {}).get('records',[])
    _,chip_seed_provenance=load_seed_focus_points(chip_seeds or None,plan,limits=Limits(**config['limits']))
    payload={'schema':1,'plan':plan['scan_plan'],'transform':plan['transform'],'limits':asdict(Limits(**config['limits'])),
        'context':config.get('context',{}),'acquisition_settings':full_chip._camera_context(reference_frame),'camera_shape':list(reference_frame.rgb.shape),
        'source':reference_frame.metadata.get('source','unknown'),'material_model':None,
        'coverage_plan_fingerprint':plan['fingerprint'],'full_chip_options':plan['options'],'prior_focus_provenance':None,
        'seed_focus_provenance':chip_seed_provenance,'coverage_kind':plan['kind'],'algorithm_source_sha256':file_hash(full_chip.__file__)}
    fingerprint=hashlib.sha256(json.dumps(payload,sort_keys=True).encode()).hexdigest()
    # Pure preflight on an in-memory copy: do not rewrite the chip manifest.
    full_chip._accept_resume_payload(json.loads(json.dumps(manifest)),payload,fingerprint,allow_source_change)
    source_hash=file_hash(__file__);previous_source=state.get('algorithm_source_sha256')
    if previous_source!=source_hash and not allow_source_change:
        raise SafetyError('Campaign source version differs or was not recorded; use explicit allow_source_change to record this repair.')
    transition=None
    if previous_source!=source_hash:
        transition={'at':time.time(),'from_sha256':previous_source,'to_sha256':source_hash,
            'authorization':'explicit resume with allow_source_change; saved configuration, plan, bounds, reference, profile and focus evidence match',
            'previous_source_unrecorded':previous_source is None,'completed_tiles_retained':len(manifest.get('completed',{}))}
    remaining=plan['tile_count']-len(manifest.get('completed',{}))
    if remaining:
        pending_plan={**plan,'tile_count':remaining,'tile_centres_um':[xy for index,xy in enumerate(plan['tile_centres_um']) if f'tile_{index:06d}' not in manifest.get('completed',{})]}
        storage=estimate_campaign_storage(config,pending_plan,int(reference_frame.rgb.nbytes),folder)
        if not storage['sufficient']:raise OSError('Insufficient storage to resume the remaining chip coverage plan.')
    else:storage={'sufficient':True,'remaining_tiles':0}
    owned=scope is None;original_check=None;started=time.monotonic();elapsed=float(state.get('elapsed_seconds',0))
    def save():
        state.update(updated_at=time.time(),elapsed_seconds=elapsed+time.monotonic()-started)
        atomic_json(out/'batch.json',state);atomic_json(out/'layout.json',layout)
    def check():
        if original_check:original_check()
        check_campaign_stop(out,resume_deadline)
    def progress(phase,frame,info):
        check();state['phase']=phase;chip=state['chip_states'][key]
        if phase=='tile':chip.update(state='acquiring',completed_tiles=info['index'],total_tiles=info['total'])
        elif 'focus' in phase:chip.update(state='focusing',focus_progress={k:v for k,v in info.items() if k in ('anchor_index','z_um','search_phase','score')})
        save()
        if callback:callback(phase,frame,info)
    try:
        if owned:scope=connect(config)
        original_check=scope.check_cancel;scope.check_cancel=check;arm(scope,config);validate_search_envelope(scope,bounds)
        state.update(state='running',phase='resuming_saved_chip',algorithm_source_sha256=source_hash)
        state.pop('error',None)
        if transition:state.setdefault('algorithm_transitions',[]).append(transition)
        state.setdefault('resume_attempts',[]).append({'at':time.time(),'chip_id':key,'completed_tiles_retained':len(manifest.get('completed',{})),
            'verified_focus_points_retained':len(manifest.get('focus_points_um',[])),'storage_preflight':storage,
            'scope':'Resume this saved chip only; later campaign discovery remains pending.'})
        save();check()
        manifest=run_full_chip(scope,plan,reference_frame,folder,context=config.get('context'),prior_scan=None,
            seed_focus_results=chip_seeds or None,resume=True,allow_source_change=allow_source_change,callback=progress)
        chip=state['chip_states'][key]
        chip.update(state=manifest['state'],completed_tiles=len(manifest['completed']),
            planned_area_acquired=manifest.get('planned_area_acquired',False),chip_extent_validated=manifest.get('chip_extent_validated',False),
            focus_coverage_complete=manifest.get('focus_coverage_complete',False),analysis_state='pending_offline')
        if manifest.get('chip_extent_validated'):
            measured=manifest['boundary_validation']['acquired_raster_evidence']['outline_um'];chip['boundary_status']='measured_enclosure'
            for entry in layout['chips']:
                if entry['id']==key:entry.update(stage_polygon_um=measured,boundary_status='measured_enclosure')
        state.update(current_chip=None,state='chip_complete_discovery_pending',phase='awaiting_campaign_continuation',all_chips_found=False)
    except BaseException as exc:
        state.update(state='time_budget_reached' if isinstance(exc,CampaignBudgetReached) else 'stopped' if isinstance(exc,Cancelled) else 'failed',phase='stopped',error=str(exc))
        if scope is not None:scope.stop()
        if not isinstance(exc,Cancelled):raise
    finally:
        if scope is not None and original_check is not None:scope.check_cancel=original_check
        if (folder/'manifest.json').exists():
            latest=json.loads((folder/'manifest.json').read_text(encoding='utf-8'))
            state['chip_states'][key].update(state=latest.get('state','stopped'),completed_tiles=len(latest.get('completed',{})),total_tiles=latest.get('tile_count'))
        try:
            if owned and scope is not None:scope.close()
        finally:save()
    return state


def _require_discovery_contract(saved,contract):
    if (saved.get('algorithm')!=contract['algorithm'] or saved.get('edge_validation_profile')!=contract['profile']
            or saved.get('edge_model_sha256')!=contract['edge_model_sha256']
            or saved.get('photometric_model_sha256')!=contract['photometric_model_sha256']):
        raise SafetyError('Saved discovery has a different algorithm/profile; preserve its identity and reanalyse separately.')


def run_chip_campaign(config,reference_tiff,output,search_bounds_um,options,*,full_chip_options=None,initial_discovery=None,
                      seed_focus_z_um=None,seed_focus_evidence=None,seed_focus_frame=None,seed_focus_results=None,scope=None,callback=None,
                      resume=False,allow_source_change=False,continue_discovery=False,continuation_max_chips=None,continuation_walltime_s=None,
                      recover_seed_rejection=False,rectangle_mode=False):
    """Run or explicitly continue a campaign; optional scope is caller-owned and never closed here.

    No resume is inferred across coordinate re-zeroing. Explicit fresh initial
    discovery may be reused only after profile/reference validation. Completed chip rasters
    have normal manifests and may later be analyzed independently. STOP and the
    rectangle wall-time budget stops even an active raster, preserving committed tiles.
    """
    options=options if isinstance(options,CampaignOptions) else CampaignOptions(**options)
    envelope=validate_campaign_envelope(config,search_bounds_um)
    storage_options=CampaignStorageOptions(**config.get('campaign_storage',{}))
    from .storage import managed_output_path
    bounds=envelope['search_bounds_um'];out=managed_output_path(output)
    if rectangle_mode:
        from .rectangle_handoff import KIND
        proof_path=out/'rectangle-handoff-proof.json'
        if (not (continue_discovery or resume) or not allow_source_change or not proof_path.exists()
                or json.loads(proof_path.read_text(encoding='utf-8')).get('kind')!=KIND):
            raise SafetyError('Rectangle campaign upgrade requires its explicit copied-evidence handoff.')
    if resume and continue_discovery:raise ValueError('Choose resume or continue_discovery, not both.')
    if recover_seed_rejection and not continue_discovery:raise ValueError('Seed rejection recovery requires explicit continue_discovery.')
    if recover_seed_rejection:raise SafetyError('Legacy seed-rejection recovery is not compatible with the v2 discovery contract; preserve the recorded lineage.')
    if not continue_discovery and (continuation_max_chips is not None or continuation_walltime_s is not None):
        raise ValueError('Continuation budget overrides require continue_discovery.')
    if resume:
        return _resume_campaign_chip(config,reference_tiff,out,bounds,options,full_chip_options=full_chip_options,
            initial_discovery=initial_discovery,seed_focus_z_um=seed_focus_z_um,seed_focus_evidence=seed_focus_evidence,
            seed_focus_frame=seed_focus_frame,seed_focus_results=seed_focus_results,scope=scope,callback=callback,allow_source_change=allow_source_change)
    if allow_source_change and not continue_discovery:raise ValueError('allow_source_change requires explicit resume or continue_discovery.')
    if (out/'STOP').exists():raise Cancelled('Campaign STOP exists; it must be cleared deliberately before a new run.')
    if not continue_discovery and ((out/'batch.json').exists() or (out/'layout.json').exists()):raise ValueError('A campaign/layout already exists here; use an explicit continuation.')
    if not continue_discovery and out.exists() and any(out.glob('chip*/manifest.json')):raise ValueError('Existing chip scans require an explicit separate campaign folder.')
    reference_path=Path(reference_tiff).resolve();reference_frame=load_frame(reference_path)
    transform=transform_from_config(config);transform.validate_frame(reference_frame)
    if not transform.calibrated:raise SafetyError('Measured camera transform required.')
    reference=SubstrateReference.from_frame(reference_frame)
    edge_profile=config.get('chip_discovery',{}).get('edge_validation_profile')
    edge_contract=assess_edge_profile(edge_profile,reference_frame)
    if not edge_contract['supported']:raise SafetyError('Discovery unresolved before connection: '+edge_contract['reason'])
    discovery_contract={'algorithm':ALGORITHM_VERSION,'profile':edge_profile,
        'edge_model_sha256':file_hash(Path(__file__).with_name('sparse_edges.py')),
        'photometric_model_sha256':file_hash(Path(__file__).with_name('photometric_edges.py'))}
    continuation_saved=json.loads((out/'batch.json').read_text(encoding='utf-8')) if continue_discovery else None
    if continue_discovery:
        if continuation_saved.get('discovery_contract')!=discovery_contract:
            raise SafetyError('Discovery algorithm/profile transition requires a separate reviewed campaign lineage; legacy identities are not rewritten.')
        saved_focus=continuation_saved.get('seed_focus',{})
        if seed_focus_z_um is not None and abs(float(seed_focus_z_um)-saved_focus.get('z_um',float('inf')))>.5:
            raise SafetyError('Continuation cannot replace the saved discovery focus height.')
        for supplied,key in ((seed_focus_evidence,'evidence_path'),(seed_focus_frame,'image')):
            if supplied is not None and str(Path(supplied).resolve())!=saved_focus.get(key):
                raise SafetyError('Continuation cannot replace saved discovery focus evidence.')
        if initial_discovery is not None and str(Path(initial_discovery).resolve())!=continuation_saved.get('initial_discovery'):
            raise SafetyError('Continuation initial discovery differs from saved provenance.')
        if seed_focus_results is None:seed_focus_results=(continuation_saved.get('seed_focus_bundle_provenance') or {}).get('records')
    # Validate the explicit fresh bundle before outputs/connection. A later
    # per-chip selection uses only measurements strictly inside that chip.
    from .full_chip import _camera_context
    seed_validation_plan={'transform':asdict(transform),'camera_shape':list(reference_frame.rgb.shape),
        'acquisition_settings':_camera_context(reference_frame),'scan_plan':{'polygon_um':[
            [bounds[0],bounds[2]],[bounds[1],bounds[2]],[bounds[1],bounds[3]],[bounds[0],bounds[3]]]}}
    _,seed_bundle_provenance=load_seed_focus_points(seed_focus_results,seed_validation_plan,limits=Limits(**config['limits']))
    if continue_discovery and not _same_json(seed_bundle_provenance,continuation_saved.get('seed_focus_bundle_provenance')):
        raise SafetyError('Continuation focus bundle differs from saved provenance.')
    initial_state=None
    if initial_discovery is not None and not continue_discovery:
        initial_discovery=Path(initial_discovery).resolve()
        initial_state=json.loads((initial_discovery/'discovery.json').read_text(encoding='utf-8'))
        _require_discovery_contract(initial_state,discovery_contract)
        expected_pixels=hashlib.sha256(np.ascontiguousarray(reference_frame.rgb).tobytes()).hexdigest()
        if (initial_state.get('state') not in ('estimated','unresolved') or initial_state.get('reference_pixels_sha256')!=expected_pixels
                or initial_state.get('reference',{}).get('context')!=reference.context
                or initial_state.get('transform')!=json.loads(json.dumps(asdict(transform)))):
            raise SafetyError('Initial discovery must be finished and use this exact fresh reference/profile/transform.')
        if initial_state.get('context',{})!=config.get('context',{}):raise SafetyError('Initial discovery sample context differs.')
        if not np.allclose(initial_state.get('seed_xy_um'),[reference_frame.position.x,reference_frame.position.y],rtol=0,atol=.5):
            raise SafetyError('Initial discovery seed differs from the fresh reference position.')
        ob=initial_state['options']['search_bounds_um']
        if ob[0]<bounds[0] or ob[1]>bounds[1] or ob[2]<bounds[2] or ob[3]>bounds[3]:raise SafetyError('Initial discovery exceeds the explicit campaign envelope.')
        for record in initial_state.get('records',[]):
            for relative,digest in record.get('hashes',{}).items():
                if file_hash(initial_discovery/relative)!=digest:raise SafetyError('Initial discovery evidence changed: '+relative)
    seed=[reference_frame.position.x,reference_frame.position.y]
    if not bounds[0]<=seed[0]<=bounds[1] or not bounds[2]<=seed[1]<=bounds[3]:raise ValueError('Fresh reference lies outside the explicit native search bounds.')
    if min(contained_field_size(reference_frame,transform))<=options.detectable_patch_diameter_um:raise ValueError('Detectable patch must fit the reference field.')
    search=plan_campaign_search(bounds,options.minimum_chip_diameter_um,seed,options.detectable_patch_diameter_um,options.max_seed_probes)
    if config.get('context',{}).get('reference_correction'):
        from .reference_repair import preserved_search
        search=preserved_search(out,search)
    # Never inherit old interior_scan absolute Z recovery ranges implicitly.
    full_options=full_chip_options or config.get('full_chip',{})
    full_options=full_options if isinstance(full_options,FullChipOptions) else FullChipOptions(**full_options)
    if full_options.focus_search_bounds_um is not None:
        lo,hi=full_options.focus_search_bounds_um
        if not np.isfinite([lo,hi]).all() or not config['limits']['z_min']<=lo<hi<=config['limits']['z_max']:
            raise SafetyError('Full-chip focus recovery bounds must fit the fresh configured Z envelope.')
    if continue_discovery:
        replay_deadline=handoff_monotonic_deadline(out) if rectangle_mode else float('inf')
        def replay_check():
            if scope is not None:scope.check_cancel()
            check_campaign_stop(out,replay_deadline)
        state,layout,options,pending,validated_outlines,observed_coverages,deferred_outlines,revision=_prepare_continuation(
            config,reference_path,reference_frame,transform,out,bounds,options,full_options,search,
            allow_source_change=allow_source_change,max_chips=continuation_max_chips,additional_seconds=continuation_walltime_s,
            recover_seed_rejection=recover_seed_rejection,check=replay_check,
            progress=(lambda info:callback('campaign_coverage_replay',None,info)) if callback else None)
        state['storage_options']=asdict(storage_options)
        started=time.monotonic()-float(state.get('elapsed_seconds',0));deadline=started+options.walltime_budget_s
        first=False;seed_focus_z=float(state['seed_focus']['z_um'])
        if state.get('discovery_focus_seed'):
            last_focus=state['discovery_focus_seed'];result_path=(out/last_focus['result_path']).resolve()
            if not result_path.is_relative_to((out/'discovery').resolve()) or file_hash(result_path)!=last_focus['result_sha256']:
                raise SafetyError('Saved discovery focus starting-height evidence changed.')
            verified=json.loads(result_path.read_text(encoding='utf-8'))
            if (not verified.get('success') or abs(float(verified['z_um'])-float(last_focus['z_um']))>1e-6 or
                    not config['limits']['z_min']<float(verified['z_um'])<config['limits']['z_max']):
                raise SafetyError('Saved discovery focus starting height is invalid.')
            seed_focus_z=float(verified['z_um'])
        if revision:
            revision_path=out/revision
            if revision_path.exists() and not _same_json(json.loads(revision_path.read_text(encoding='utf-8')),config):
                raise SafetyError('Immutable configuration revision already exists with different contents.')
            if not revision_path.exists():atomic_json(revision_path,config)
    else:
        started=time.monotonic();deadline=started+options.walltime_budget_s
        state={'schema':1,'state':'running','coverage_kind':'interleaved_discovery_and_full_planned_chip_rasters',
            'coverage_note':'Individual planned raster coverage and measured chip-extent validation are separate. Remaining/uncertain search points do not imply absent chips.',
            'coordinate_frame_id':config.get('context',{}).get('prior_coordinate_frame_id') or str(uuid.uuid4()),
            'coordinate_policy':('Verified same-placement continuation; original native coordinate frame retained.' if config.get('context',{}).get('prior_coordinate_frame_id') else 'Fresh supplied reference and explicit native envelope; no old layout/focus positions reused.'),
            'algorithm_source_sha256':file_hash(__file__),'discovery_contract':discovery_contract,
            'started_at':time.time(),'options':asdict(options),'full_chip_options':asdict(full_options),
            'search_bounds_um':bounds,'envelope_preflight':envelope,'storage_options':asdict(storage_options),
            'reference_path':str(reference_path),'reference_sha256':hashlib.sha256(reference_path.read_bytes()).hexdigest(),
            'reference_position_um':asdict(reference_frame.position),'reference_context':reference.context,
            'initial_discovery':str(initial_discovery) if initial_discovery else None,'seed_focus_bundle_provenance':seed_bundle_provenance,
            'time_budget_policy':'Stop starting new chips at the budget; finish an already started chip unless STOP/error intervenes.',
            'current_chip':None,'phase':'connecting','chip_states':{},'seed_records':[],'boundary_attempts':[],
            'expected_chip_count':None,'all_chips_found':False,'analysis_policy':'Acquisition first; completed chips queued for independent offline analysis.'}
        layout={'schema':1,'coordinate_frame_id':state['coordinate_frame_id'],'chips':[],'expected_chip_count':None,
            'all_chips_found':False,'search_bounds_um':bounds,'coverage_kind':'fresh observed chips; discovery incomplete until explicitly reported'}
        out.mkdir(parents=True,exist_ok=True)
        atomic_json(out/'configuration.json',config);atomic_json(out/'search-plan.json',search)
        save_frame(out/'reference','substrate',reference_frame)
        pending=set(range(search['probe_count']));first=True;seed_focus_z=None
        validated_outlines=[];observed_coverages=[];deferred_outlines=[]
    owned=scope is None;original_check=None;in_full_chip=False
    seconds_per_tile=state.get('measured_full_scan_seconds_per_tile_including_focus')
    pending_discoveries=_pending_discoveries(out,state) if continue_discovery else []
    memory_progress=None
    grid_fit=None
    if rectangle_mode:
        deadline=min(deadline,handoff_monotonic_deadline(out))
        state['time_budget_policy']='Original absolute user deadline applies during focus and raster; committed partial coverage is retained.'
    hydrate_observed_rectangles(out,state)
    def update_grid_prior():
        nonlocal grid_fit
        if options.grid_prior is None:return
        from .grid_search import fit_grid_prior
        observations=campaign_grid_observations(state,validated_outlines)
        misses=[r for r in state.get('seed_records',[]) if r.get('classification')=='non_substrate_sample']
        grid_fit=fit_grid_prior(observations,options.grid_prior,misses=misses)
        expected=options.grid_prior['rows']*options.grid_prior['columns']
        measured=len(distinct_measured_chips(validated_outlines))
        observed=campaign_chip_budget_count(options,state['chip_states'],validated_outlines)
        state.update(expected_chip_count=expected,measured_distinct_chip_count=measured,
            observed_distinct_chip_count=observed,expected_chip_count_remaining=max(0,expected-measured),
            expected_observed_chip_count_remaining=max(0,expected-observed),grid_prior_fit=grid_fit,
            grid_target_status='measured_target_reached' if measured>=expected else 'observed_target_reached_extent_validation_pending' if observed>=expected else 'unresolved',
            chip_budget_count_policy='distinct_observed_four_side_rectangles_or_measured_enclosures',
            chip_budget_count_qualification='Four locally measured sides support a rectangle model; raster extent, focus and acquisition completion remain separate.',all_chips_found=False)
        layout.update(expected_chip_count=expected,measured_distinct_chip_count=measured,
            observed_distinct_chip_count=observed,all_chips_found=False)
    update_grid_prior()
    def save():
        state['updated_at']=time.time();state['elapsed_seconds']=time.monotonic()-started
        state['remaining_lattice_probes']=len(pending)
        state['remaining_lattice_indices']=sorted(pending)
        atomic_json(out/'batch.json',state);atomic_json(out/'layout.json',layout)
    def check():
        if original_check:original_check()
        check_campaign_stop(out,float('inf') if in_full_chip and not rectangle_mode else deadline)
    def progress(phase,frame,info):
        check();state['phase']=phase
        key=state.get('current_chip')
        if key:
            chip=state['chip_states'][key]
            if phase=='tile':chip.update(state='acquiring',completed_tiles=info['index'],total_tiles=info['total'])
            elif 'focus' in phase:chip.update(state='focusing',focus_progress={k:v for k,v in info.items() if k in ('anchor_index','z_um','search_phase','score')})
        save()
        if callback:callback(phase,frame,info)
    save()
    try:
        check()
        if owned:scope=connect(config)
        original_check=scope.check_cancel;scope.check_cancel=check
        arm(scope,config);validate_search_envelope(scope,bounds)
        current=scope.position()
        if not continue_discovery:
            if initial_state is None and np.linalg.norm(np.array([current.x,current.y])-seed)>.5:
                raise SafetyError('Fresh color-reference XY differs from current readback. Supply the matching fresh discovery or a current-position reference.')
            seed_focus_z=float(current.z if seed_focus_z_um is None else seed_focus_z_um)
            focus_provenance={'source':'actual_starting_Z_readback' if seed_focus_z_um is None else 'explicit_starting_Z',
                'z_um':seed_focus_z,'initial_readback':asdict(current),'color_reference_Z_is_not_focus_evidence':True,
                'focus_verified':False,'qualification':'Starting height for geometric search only; each chip measures its own focus surface.'}
            if seed_focus_evidence is not None:
                path=Path(seed_focus_evidence).resolve();saved_focus=json.loads(path.read_text(encoding='utf-8'))
                if not saved_focus.get('success') or not np.isfinite(saved_focus.get('z_um',np.nan)):
                    raise SafetyError('Seed focus evidence must contain a successful finite focus result.')
                measured_z=float(saved_focus['z_um'])
                if seed_focus_z_um is not None and abs(seed_focus_z-measured_z)>.5:raise SafetyError('Explicit seed focus Z differs from the supplied focus evidence.')
                if abs(current.z-measured_z)>.5:raise SafetyError('Current starting Z differs from the supplied fresh focus result.')
                seed_focus_z=measured_z
                focus_provenance.update(source='fresh_successful_focus_result',z_um=measured_z,focus_verified=True,
                    evidence_path=str(path),evidence_sha256=file_hash(path))
            if seed_focus_frame is not None:
                path=Path(seed_focus_frame).resolve();focus_frame=load_frame(path);transform.validate_frame(focus_frame)
                if SubstrateReference.from_frame(focus_frame).context!=reference.context or abs(focus_frame.position.z-seed_focus_z)>.5:
                    raise SafetyError('Seed focus image profile/height differs from the starting focus.')
                focus_provenance.update(image=str(path),image_sha256=file_hash(path),metadata_sha256=file_hash(path.with_suffix('.json')))
            if not scope.limits.z_min<=seed_focus_z<=scope.limits.z_max:raise SafetyError('Seed focus Z is outside the fresh travel envelope.')
            state['seed_focus']=focus_provenance;save()
        if getattr(scope,'lease',None):scope.lease.acquire()
        if not continue_discovery:
            from .memory_progress import prepare_progress
            memory_progress=prepare_progress(out,state,config,reference_frame,transform,search)
            if memory_progress is not None:
                first=False;pending=memory_progress['pending'];pending_discoveries=memory_progress['discoveries'];save()
        while first or pending or pending_discoveries:
            check()
            if campaign_chip_budget_count(options,state['chip_states'],validated_outlines)>=options.max_chips:
                state.update(state='chip_budget_reached',phase='finished_requested_chip_budget');break
            current=scope.position();resuming_seed=False;resuming_attempt=None;seed_priority=None
            if pending_discoveries:
                seed_record_index,resuming_attempt=pending_discoveries.pop(0)
                record=state['seed_records'][seed_record_index];index=record['index'];xy=record['requested_xy_um']
                if resuming_attempt.get('reference_reclassified_seed'):
                    enclosing=next((name for name,polygon in validated_outlines if strictly_inside_outline(xy,polygon)),None)
                    if enclosing:
                        resuming_attempt.update(state='skipped_inside_measured_chip',chip_id=enclosing);save();continue
                resuming_attempt['state']='discovering';save()
                frame=load_frame(out/record['image']);transform.validate_frame(frame);resuming_seed=True
            elif first:
                index=None;xy=seed;frame=reference_frame;first=False
            else:
                seed_priority=state.pop('priority_boundary_seed',None)
                if seed_priority is not None and seed_priority['index'] in pending:
                    index=seed_priority['index']
                else:
                    seed_priority=None
                    if grid_fit is not None:
                        from .grid_search import grid_probe_order
                        streak=state.get('grid_priority_streak',0)
                        maximum=options.grid_prior.get('max_prior_probes',8)
                        index=grid_probe_order(search,pending,[current.x,current.y],grid_fit,priority_streak=streak)[0]
                        baseline=streak>=maximum or not grid_fit.get('candidate_centres_um')
                        state['grid_priority_streak']=0 if baseline else streak+1
                        seed_priority={'method':'measured_grid_prior_search_order',
                            'fit_status':grid_fit['status'],'index':index,
                            'baseline_turn':baseline,'prior_streak':streak,
                            'qualification':'Unverified existing lattice sample; grid predictions are not chip or coverage evidence.'}
                        state.setdefault('grid_probe_history',[]).append({**seed_priority,'requested_xy_um':search['probe_centres_um'][index]})
                    else:
                        index=min(pending,key=lambda j:((search['probe_centres_um'][j][0]-current.x)**2+(search['probe_centres_um'][j][1]-current.y)**2,j))
                xy=search['probe_centres_um'][index]
                enclosing=next((name for name,polygon in validated_outlines if strictly_inside_outline(xy,polygon)),None)
                if enclosing:
                    pending.remove(index)
                    state['seed_records'].append({'index':index,'requested_xy_um':xy,'classification':'skipped_inside_measured_chip','chip_id':enclosing});save();continue
                measured=next(((name,coverage) for name,coverage in observed_coverages if point_in_observed_coverage(xy,coverage)),None)
                if measured:
                    name,coverage=measured;pending.remove(index)
                    state['seed_records'].append({'index':index,'requested_xy_um':xy,'classification':'skipped_observed_connected_substrate',
                        'chip_id':name,'evidence_source':coverage['source'],'evidence_sha256':coverage['source_sha256'],
                        'qualification':coverage['qualification']});save();continue
                deferred=next((name for name,polygon in deferred_outlines if strictly_inside_outline(xy,polygon)),None)
                if deferred:
                    pending.remove(index)
                    state['seed_records'].append({'index':index,'requested_xy_um':xy,'classification':'deferred_inside_focus_failed_estimate',
                        'chip_id':deferred,'qualification':'Deferred modeled chip, not observed full coverage or verified absence.'});save();continue
                state.update(phase='searching_for_substrate',current_chip=None);save()
                scope.travel(*xy);check();move_z_bounded(scope,seed_focus_z);check()
                frame=scope.capture();transform.validate_frame(frame)
                if frame.rgb.shape!=reference_frame.rgb.shape or np.linalg.norm(np.array([frame.position.x,frame.position.y])-xy)>.5:
                    raise AcquisitionError('Campaign seed capture geometry or position differs from its plan.')
            if not resuming_seed:
                classification=_seed_classification(frame,reference,transform,options.detectable_patch_diameter_um)
                name=f'seed_{len(state["seed_records"]):06d}';save_frame(out/'search'/'frames',name,frame)
                record={'index':index,'requested_xy_um':list(map(float,xy)),'position_um':asdict(frame.position),
                    'image':f'search/frames/{name}.tiff','reference_reused':index is None,**classification,
                    'hashes':{f'search/frames/{name}'+suffix:file_hash(out/'search'/'frames'/(name+suffix)) for suffix in ('.tiff','.json')}}
                if seed_priority is not None:record['search_priority']=seed_priority
                if index is not None:pending.remove(index)
                state['seed_records'].append(record);save()
                from .vendor_lifecycle import checkpoint_observation
                checkpoint_observation(scope, frame, out/'search'/'vendor-observations',
                    'substrate_seed_probe', record, selected_raw=out/record['image'])
                if callback:callback('substrate_seed_probe',frame,record)
                if classification['classification']!='substrate_seed':
                    if classification['classification']=='non_substrate_sample':update_grid_prior();save()
                    continue
            boundary_bounds=local_boundary_bounds(xy,bounds,options.boundary_radius_um)
            if boundary_bounds is None:
                record['discovery_status']='unresolved_seed_on_search_boundary';save();continue
            reuse_initial=initial_state is not None and index is None and not resuming_seed
            if resuming_attempt:
                attempt_record=resuming_attempt;attempt=attempt_record['id'];discovery_folder=Path(attempt_record['folder'])
                attempt_record.setdefault('continuations',[]).append({'at':time.time(),'reason':'explicit campaign continuation'})
            else:
                attempt=f'candidate{len(state["boundary_attempts"])+1:03d}'
                discovery_folder=initial_discovery if reuse_initial else out/'discovery'/attempt
                record['attempt_id']=attempt
                attempt_record={'id':attempt,'seed_record_index':seed_record_index if resuming_seed else len(state['seed_records'])-1,
                    'seed_xy_um':list(map(float,xy)),'state':'discovering','folder':str(discovery_folder)}
                state['boundary_attempts'].append(attempt_record)
            state.update(phase='measuring_chip_edges',current_chip=None);save()
            edge_options=SparseEdgeOptions(boundary_bounds,options.boundary_coarse_step_um,options.boundary_max_probes,
                options.boundary_cell_um,margin_um=options.boundary_margin_um,
                focus_policy_id=f'Fresh starting focus Z {seed_focus_z:.6f}; geometric evidence only')
            rectangle_options={'rectangle_mode':True,'rectangle_min_chip_um':options.minimum_chip_diameter_um,
                'rectangle_grid_pitch_um':(options.grid_prior or {}).get('spacing_um',10000.)} if rectangle_mode else {}
            if rectangle_options:edge_options=replace(edge_options,**rectangle_options)
            if not (rectangle_mode and resuming_seed):move_z_bounded(scope,seed_focus_z)
            check()
            if resuming_attempt and (discovery_folder/'discovery.json').exists():
                saved_discovery=json.loads((discovery_folder/'discovery.json').read_text(encoding='utf-8'))
                _require_discovery_contract(saved_discovery,discovery_contract)
                if saved_discovery['state']=='estimated' and not rectangle_mode:
                    for evidence_record in saved_discovery.get('records',[])+saved_discovery.get('support_records',[]):
                        for relative,digest in evidence_record.get('hashes',{}).items():
                            if file_hash(discovery_folder/relative)!=digest:raise SafetyError('Deferred discovery evidence changed: '+relative)
                    discovery=saved_discovery
                else:
                    imported_memory=memory_progress is not None and attempt_record.get('memory_progress_import')
                    recovery_budget=attempt_record.get('recovery_operation_budget',{}) if imported_memory else {}
                    saved_options={**saved_discovery['options'],**rectangle_options}
                    if rectangle_mode:
                        recovery_budget={'max_elapsed_s':max(.001,deadline-time.monotonic())}
                        attempt_record['algorithm_upgrade_budget']={**recovery_budget,
                            'capture_policy':'Derived by sparse acquisition from remaining rectangle rays, actual field size and remaining quick-focus allowance.',
                            'prior_operation_budget':saved_discovery.get('operation_budget'),
                            'qualification':'Original records/counters retained; remaining absolute user deadline bounds this operation.'}
                    discovery=discover_sparse_outline(scope,xy,transform,reference_frame,discovery_folder,SparseEdgeOptions(**saved_options),
                        context=saved_discovery['context'] if imported_memory or rectangle_mode else config.get('context'),resume=True,continue_unresolved=rectangle_mode or bool(attempt_record.get('seed_rejection_recovery')),callback=progress,
                        allow_source_change=allow_source_change,focus_search_bounds_um=full_options.focus_search_bounds_um,edge_validation_profile=edge_profile,
                        **{k:recovery_budget[k] for k in ('max_elapsed_s','max_total_captures') if k in recovery_budget})
            elif reuse_initial:
                attempt_record['reused_fresh_discovery']=True
                if initial_state['state']=='unresolved':
                    discovery=discover_sparse_outline(scope,xy,transform,reference_frame,discovery_folder,SparseEdgeOptions(**initial_state['options']),
                        context=config.get('context'),resume=True,continue_unresolved=True,callback=progress,
                        focus_search_bounds_um=full_options.focus_search_bounds_um,edge_validation_profile=edge_profile)
                else:discovery=initial_state
            else:
                discovery=discover_sparse_outline(scope,xy,transform,reference_frame,discovery_folder,edge_options,
                    context=config.get('context'),callback=progress,focus_search_bounds_um=full_options.focus_search_bounds_um,edge_validation_profile=edge_profile,
                    **({'max_elapsed_s':max(.001,deadline-time.monotonic())} if rectangle_mode else {}))
            attempt_record.update(state=discovery['state'],probe_count=len(discovery['records']))
            if rectangle_mode and discovery.get('rectangle_probe_allowance'):
                attempt_record['rectangle_work_budget']=discovery['rectangle_probe_allowance']
            focus_attempts=discovery.get('edge_focus_attempts',[])
            if focus_attempts and focus_attempts[-1].get('state')=='focused' and focus_attempts[-1].get('focused_image'):
                measured=focus_attempts[-1];focus_z=float(measured['result']['z_um'])
                if not scope.limits.z_min<focus_z<scope.limits.z_max:raise SafetyError('Measured discovery focus exceeds the current Z limits.')
                result_path=(discovery_folder/measured['folder']/'focus-result.json').resolve()
                if not result_path.is_relative_to(out):raise SafetyError('Discovery focus provenance must belong to this campaign.')
                seed_focus_z=focus_z
                state['discovery_focus_seed']={'z_um':focus_z,'attempt_id':attempt,'result_path':str(result_path.relative_to(out)),
                    'result_sha256':measured['result_sha256'],'focused_image':str(discovery_folder/measured['focused_image']),
                    'qualification':'Measured chip-edge height seeds later geometric searches only; no focus plane is transferred between chips.'}
                attempt_record['edge_focus_starting_height']=dict(state['discovery_focus_seed'])
            if discovery.get('reason'):attempt_record['reason']=discovery['reason']
            if discovery.get('seed_verification'):attempt_record['seed_verification']=discovery['seed_verification']
            outline=discovery.get('estimated_outline',{})
            if observed_rectangle_outline(outline):
                attempt_record.update(search_outline=outline,search_outline_discovery_sha256=file_hash(discovery_folder/'discovery.json'))
                duplicate=next((name for name,chip in state['chip_states'].items()
                    if observed_rectangle_outline(chip.get('observed_rectangle_outline'))
                    and rectangle_overlap(outline['polygon_um'],chip['observed_rectangle_outline']['polygon_um'])>=.8),None)
                if duplicate:
                    attempt_record.update(state='duplicate_observed_rectangle',duplicate_of_chip_id=duplicate,
                        qualification='Same observed rectangle model; existing chip focus/raster state retained, no new chip count.')
                    update_grid_prior();save();continue
                update_grid_prior()
            if outline.get('status')!='estimated':
                # At most one immediate extent-based retry per lineage. A
                # prioritized retry that fails returns to ordinary search.
                if record.get('search_priority',{}).get('method')!='observed_substrate_extent_search_order_v1':
                    priority=interior_reseed(discovery,search['probe_centres_um'],pending,
                        minimum_shift_um=2*options.boundary_coarse_step_um)
                    if priority is not None:
                        priority.update(source_attempt_id=attempt,source_discovery_sha256=file_hash(discovery_folder/'discovery.json'))
                        state['priority_boundary_seed']=priority
                save();continue
            check();key=f'chip{len(state["chip_states"])+1:03d}'
            try:
                plan=build_full_chip_plan(discovery_folder,reference_frame,transform,full_options,key,estimated_outline=outline)
            except AcquisitionError as exc:
                attempt_record.update(state='unresolved_focus_support',error=str(exc));save();continue
            geometry=focus_geometry_diagnostics(plan);attempt_record['initial_focus_geometry']=geometry
            if plan.get('focus_anchor_mode')!='chip_edges' and geometry['geometry_rejected_tiles']>max(4,options.support_trigger_fraction*plan['tile_count']):
                state['phase']='measuring_interior_focus_support';save()
                points=interior_support_points(plan,options.support_inset_um)
                acquire_interior_support(scope,discovery_folder,transform,reference_frame,points,callback=progress)
                plan=build_full_chip_plan(discovery_folder,reference_frame,transform,full_options,key,estimated_outline=outline)
                geometry=focus_geometry_diagnostics(plan);attempt_record['updated_focus_geometry']=geometry
            if seconds_per_tile is not None:
                estimate=seconds_per_tile*plan['tile_count']
                attempt_record['estimated_full_scan_seconds']=estimate
                if estimate>deadline-time.monotonic():
                    if rectangle_mode:
                        attempt_record['completion_forecast']={'estimated_seconds_including_previous_chip_focus':estimate,
                            'remaining_session_seconds':max(0.,deadline-time.monotonic()),'complete_chip_expected':False,
                            'policy':'Diagnostic forecast; acquire useful partial coverage until the original actual deadline.'}
                    else:
                        attempt_record['state']='deferred_time_budget'
                        state.update(state='time_budget_reached',phase='next_chip_exceeds_measured_time_budget');save();break
            storage=estimate_campaign_storage(config,plan,int(reference_frame.rgb.nbytes),out/key,
                                              geometry=geometry,options=storage_options)
            attempt_record['storage_preflight']=storage
            if not storage['sufficient']:
                attempt_record['state']='deferred_storage_budget'
                state.update(state='storage_budget_reached',phase='next_chip_exceeds_free_storage');save();break
            atomic_json(out/'plans'/(key+'.json'),plan)
            state['chip_states'][key]={'state':'focusing','centre_um':list(map(float,xy)),
                'completed_tiles':0,'total_tiles':plan['tile_count'],'candidate_count':None,'analysis_state':'pending_offline',
                'discovery_folder':str(discovery_folder),'boundary_status':'estimated','all_chips_found':False}
            if observed_rectangle_outline(outline):state['chip_states'][key]['observed_rectangle_outline']=outline
            layout['chips'].append({'id':key,'centre_um':list(map(float,xy)),'status':'substrate_verified',
                'estimated_polygon_um':outline['polygon_um'],'boundary_status':'estimated'})
            state.update(current_chip=key,phase='measuring_focus_surface');attempt_record['chip_id']=key;save()
            chip_seeds=[r for r in (seed_bundle_provenance or {}).get('records',[])
                        if strictly_inside_outline(r['position_um'][:2],plan['scan_plan']['polygon_um'])]
            from .edge_focus_reuse import discovery_edge_focus_entries
            chip_seeds.extend(discovery_edge_focus_entries(discovery_folder))
            update_grid_prior()
            attempt_record['verified_focus_seed_count']=sum(r['purpose']=='verified' for r in chip_seeds)
            attempt_record['search_only_focus_seed_count']=sum(r['purpose']=='search_seed' for r in chip_seeds)
            scan_started=time.monotonic();in_full_chip=True
            try:
                manifest=run_full_chip(scope,plan,reference_frame,out/key,context=config.get('context'),prior_scan=None,
                                       seed_focus_results=chip_seeds or None,callback=progress)
            except FocusSupportError as exc:
                # This typed exception is emitted only after a successful
                # hardware STOP. Any other hardware/acquisition error aborts.
                chip=state['chip_states'][key]
                failed=json.loads((out/key/'manifest.json').read_text(encoding='utf-8'))
                chip.update(state='focus_unresolved',error=str(exc),completed_tiles=len(failed.get('completed',{})),
                    planned_area_acquired=False,focus_coverage_complete=False,analysis_state='pending_offline')
                attempt_record.update(state='focus_unresolved',error=str(exc))
                deferred_outlines.append((key,plan['scan_plan']['polygon_um']))
                layout['chips'][-1].update(status='focus_unresolved',boundary_status='estimated_deferred')
                state.update(current_chip=None,phase='continuing_after_unresolved_focus');save()
                # The successful STOP deliberately sets the adapter's cancel
                # latch. Check external STOP/time first, then rearm clears only
                # that known latch; checking scope.cancel beforehand would
                # accidentally end every recoverable focus deferral.
                in_full_chip=False;check_campaign_stop(out,deadline)
                if (out/key/'STOP').exists():raise Cancelled('Chip STOP requested during focus deferral.')
                arm(scope,config);validate_search_envelope(scope,bounds);check()
                continue
            finally:in_full_chip=False
            seconds_per_tile=(time.monotonic()-scan_started)/max(plan['tile_count'],1)
            state['measured_full_scan_seconds_per_tile_including_focus']=seconds_per_tile
            chip=state['chip_states'][key]
            chip.update(state=manifest['state'],completed_tiles=len(manifest['completed']),
                planned_area_acquired=manifest.get('planned_area_acquired',False),chip_extent_validated=manifest.get('chip_extent_validated',False),
                focus_coverage_complete=manifest.get('focus_coverage_complete',False),analysis_state='pending_offline')
            coverage=seed_connected_coverage(out/key/'coverage-evidence.npz',plan['seed_xy_um'],options.detectable_patch_diameter_um)
            observed_coverages.append((key,coverage))
            chip['observed_substrate_skip_evidence']={k:v for k,v in coverage.items() if k!='mask'}
            chip['observed_substrate_skip_evidence']['eligible_cells']=int(coverage['mask'].sum())
            np.savez_compressed(out/key/'seed-connected-substrate.npz',mask=coverage['mask'],cell_um=coverage['cell_um'],bounds_um=coverage['bounds_um'])
            if manifest.get('chip_extent_validated'):
                measured=manifest['boundary_validation']['acquired_raster_evidence']['outline_um']
                validated_outlines.append((key,measured));chip['boundary_status']='measured_enclosure'
                layout['chips'][-1].update(stage_polygon_um=measured,boundary_status='measured_enclosure')
            else:chip['boundary_status']='raster_extent_unresolved'
            update_grid_prior()
            state.update(current_chip=None,phase='continuing_seed_search');save()
        else:
            state.update(state='search_sampling_complete',phase='finished_search_lattice')
        state['sampling_complete']=not bool(pending or pending_discoveries)
        state['discovery_unresolved_count']=sum(r['state'] not in ('estimated',) for r in state['boundary_attempts'])
    except BaseException as exc:
        state.update(state='time_budget_reached' if isinstance(exc,CampaignBudgetReached) else 'stopped' if isinstance(exc,Cancelled) else 'failed',
            phase='stopped',error=str(exc))
        if scope is not None:scope.stop()
        if not isinstance(exc,Cancelled):raise
    finally:
        if scope is not None and original_check is not None:scope.check_cancel=original_check
        key=state.get('current_chip')
        if key and (out/key/'manifest.json').exists():
            manifest=json.loads((out/key/'manifest.json').read_text(encoding='utf-8'))
            state['chip_states'][key].update(state=manifest.get('state','stopped'),completed_tiles=len(manifest.get('completed',{})),
                total_tiles=manifest.get('tile_count'),planned_area_acquired=manifest.get('planned_area_acquired',False),
                chip_extent_validated=manifest.get('chip_extent_validated',False))
        try:
            if owned and scope is not None:scope.close()
        finally:save()
    return state
