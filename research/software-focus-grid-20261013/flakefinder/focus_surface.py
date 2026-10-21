"""Measure a chip's focus surface at scan-hull anchors before acquiring tiles."""
from dataclasses import asdict
from pathlib import Path
import uuid

import numpy as np
from scipy.spatial import ConvexHull

from .autofocus import autofocus, autofocus_range, quick_autofocus
from .focus_recovery import recover_focus
from .calibration import FocusMap
from .imaging import save_frame
from .vendor_lifecycle import checkpoint_observation,acknowledge_raw
from .acquisition_budget import AcquisitionBudget,BudgetScope
from .model import AcquisitionError, Position, SafetyError, atomic_json
from .motion import move_z_bounded


def _points(values,columns,name):
    array=np.asarray(values,dtype=float)
    if array.size==0:return np.empty((0,columns),float)
    if array.ndim!=2 or array.shape[1]!=columns or not np.isfinite(array).all():
        raise ValueError(f'{name} must be finite {columns}-column coordinates.')
    return np.unique(array,axis=0)


def _move_z(scope,z):
    move_z_bounded(scope,z)


def provisional_plane_seed(points,x,y,max_residual_um=1.):
    """Predict a search start, never verified focus, from every measured point.

    Unlike FocusMap.predict this may extrapolate beyond the measured XY hull.
    Its only permitted use is seeding a new, independently measured autofocus.
    The caller must check the prediction against its active motion limits.
    """
    array=_points(points,3,'Verified seed points')
    if len(array)<3 or np.linalg.matrix_rank(array[:,:2]-array[:,:2].mean(axis=0))<2:
        raise AcquisitionError('Three noncollinear verified points are required for a provisional search plane.')
    origin=array[:,:2].mean(axis=0)
    design=np.column_stack([array[:,:2]-origin,np.ones(len(array))])
    coefficients=np.linalg.lstsq(design,array[:,2],rcond=None)[0]
    residual=array[:,2]-design@coefficients
    if np.max(np.abs(residual))>max_residual_um:
        raise AcquisitionError('All verified seed points do not agree with one provisional search plane.')
    query=np.array([float(x)-origin[0],float(y)-origin[1],1.])
    predicted=float(query@coefficients)
    if not np.isfinite(predicted):raise AcquisitionError('Provisional focus seed is not finite.')
    hull=ConvexHull(array[:,:2])
    outside=bool(np.any(hull.equations[:,:2]@[float(x),float(y)]+hull.equations[:,2]>.001))
    return {'method':'provisional_measured_plane_search_seed','z_um':predicted,
            'source_points_um':array.tolist(),'fit_origin_xy_um':origin.tolist(),
            'fit_coefficients':coefficients.tolist(),'residuals_um':residual.tolist(),
            'max_residual_um':float(np.max(np.abs(residual))),'extrapolated_search_seed':outside,
            'prediction_is_focus_evidence':False,'requires_local_autofocus':True}


def prepare_focus_surface(scope,centres,focus_options,output,callback=None,seed_points=None,*,search_seed_points=None):
    """Return ``(FocusMap | None, verified_xyz_points)`` for one chip/profile.

    ``centres`` are the physical XY centres of the tiles that will be acquired.
    Hull vertices are the anchors, not the holder or the chip's physical edge.
    The caller must validate that optional seed XYZ points came from successful
    autofocus on this same chip with the same optical profile.
    Three noncollinear verified points also seed subsequent searches with a
    provisional plane, including outside their hull. Predictions never enter
    the verified-point list; the next anchor still needs successful autofocus.
    Optional ``search_seed_points`` have approximate XY registration (for
    example full-frame focus dominated by an off-centre feature). They guide
    search heights only, never skip an anchor or enter the accepted plane.
    Once three noncollinear ROI anchors have been newly verified here, only
    verified points guide subsequent searches.

    Options are ``half_range_um``, ``coarse_step_um``, ``fine_step_um``, optional
    absolute ``search_bounds_um``, ``max_residual_um`` (default 1.0), and
    ``seed_match_tolerance_um`` (default 0.5). Absolute recovery is attempted
    after a local curve fails at a search boundary. With explicit
    ``recover_initial_unresolved=True``, an unresolved first anchor also uses
    that same interval until a measured seed exists. No range is expanded.

    A collinear scan or inconsistent plane returns None: the caller must run
    per-tile autofocus. A returned FocusMap still forbids extrapolation outside
    the hull of its verified points. Missing anchors may therefore also require
    per-tile autofocus. Inconsistent anchors are never silently discarded.

    Callback follows scan's ``callback(phase, frame, metadata)`` convention.
    This helper leaves the microscope at the final measured anchor; it does
    not return automatically or attempt motion after a hardware exception.
    """
    centres=_points(centres,2,'Tile centres')
    seeds=_points([] if seed_points is None else seed_points,3,'Seed points')
    search_seeds=_points([] if search_seed_points is None else search_seed_points,3,'Search-only seed points')
    if not len(centres):raise ValueError('At least one tile centre is required.')
    if not getattr(scope,'armed',False) or getattr(scope,'limits',None) is None:
        raise SafetyError('Arm a travel envelope before preparing a focus surface.')
    options=dict(focus_options or {})
    half=float(options.get('half_range_um',8));coarse=float(options.get('coarse_step_um',2))
    fine=float(options.get('fine_step_um',.5));residual_limit=float(options.get('max_residual_um',1.0))
    seed_tolerance=float(options.get('seed_match_tolerance_um',.5))
    if not np.isfinite([half,coarse,fine,residual_limit,seed_tolerance]).all() or half<=0 or coarse<=0 or not 0<fine<=coarse or residual_limit<=0 or seed_tolerance<0:
        raise ValueError('Invalid focus surface sampling or verification options.')
    if coarse>scope.limits.max_z_step_um:raise SafetyError('Focus coarse step exceeds the permitted Z increment.')
    recovery=options.get('search_bounds_um')
    if recovery is not None:
        recovery=np.asarray(recovery,dtype=float)
        if recovery.shape!=(2,) or not np.isfinite(recovery).all() or recovery[0]>=recovery[1]:
            raise ValueError('search_bounds_um must be an explicit increasing absolute Z interval.')
        if recovery[0]<scope.limits.z_min or recovery[1]>scope.limits.z_max:
            raise SafetyError('Explicit focus recovery bounds cross the armed Z limits.')
    position=scope.position()
    headroom=float(getattr(scope,'focus_target_tolerance_um',.25))+.001
    if not np.isfinite(headroom) or headroom<=0:raise SafetyError('Invalid controller focus positioning precision.')
    allowed=(scope.limits.z_min+headroom,scope.limits.z_max-headroom)
    # Validate every intended anchor/seed before any travel.
    for x,y in centres:scope.limits.check(Position(float(x),float(y),position.z))
    for x,y,z in seeds:scope.limits.check(Position(float(x),float(y),float(z)))
    for x,y,z in search_seeds:scope.limits.check(Position(float(x),float(y),float(z)))
    root=Path(output);root.mkdir(parents=True,exist_ok=True)
    attempt=root/('attempt-'+uuid.uuid4().hex);attempt.mkdir()
    points=seeds.tolist();newly_verified=[]
    record={'schema':1,'state':'preparing','mode':'surface','centres_um':centres.tolist(),
            'seed_points_um':seeds.tolist(),'anchors':[],'verified_points_um':points,
            'search_seed_points_um':search_seeds.tolist(),'search_seeds_are_focus_evidence':False,
            'max_residual_um':residual_limit,'search_bounds_um':None if recovery is None else recovery.tolist(),
            'attempt_folder':attempt.name,'extrapolation_allowed':False}
    def save():
        atomic_json(attempt/'focus-surface.json',record)
        atomic_json(root/'focus-surface.json',record)
    if len(centres)<3 or np.linalg.matrix_rank(centres-centres.mean(axis=0))<2:
        record.update(state='complete',mode='per_tile_autofocus',reason='Tile centres are collinear; a two-dimensional focus surface cannot be fitted.')
        save();return None,points
    hull=centres[ConvexHull(centres).vertices]
    # Start with the closest physical anchor and traverse its hull consistently.
    first=int(np.argmin(np.linalg.norm(hull-[position.x,position.y],axis=1)))
    hull=np.roll(hull,-first,axis=0)
    record['anchor_centres_um']=hull.tolist();save()
    try:
        for index,(x,y) in enumerate(hull):
            scope.check_cancel()
            anchor={'index':index,'requested_xy_um':[float(x),float(y)]}
            record['anchors'].append(anchor)
            existing=np.asarray(points,dtype=float).reshape(-1,3)
            distances=np.linalg.norm(existing[:,:2]-[x,y],axis=1) if len(existing) else np.empty(0)
            if len(distances) and float(distances.min())<=seed_tolerance:
                point=existing[int(np.argmin(distances))].tolist()
                anchor.update(status='reused_verified_seed',position_um=point)
                save();continue
            seed={'method':'current_measured_height_search_seed','z_um':float(scope.position().z),
                  'prediction_is_focus_evidence':False,'requires_local_autofocus':True}
            new_array=np.asarray(newly_verified,dtype=float).reshape(-1,3)
            have_new_plane=len(new_array)>=3 and np.linalg.matrix_rank(new_array[:,:2]-new_array[:,:2].mean(axis=0))==2
            use_search_only=bool(len(search_seeds) and not have_new_plane)
            source=search_seeds if use_search_only else existing
            if len(source):
                nearest=source[int(np.argmin(np.linalg.norm(source[:,:2]-[x,y],axis=1)))]
                role='search_only_approximate_xy' if use_search_only else 'verified_roi_focus'
                seed.update(method='nearest_search_only_height_seed' if use_search_only else 'nearest_verified_height_search_seed',
                            z_um=float(nearest[2]),source_point_um=nearest.tolist(),source_role=role)
                if len(source)>=3:
                    try:
                        seed=provisional_plane_seed(source,x,y,residual_limit)
                        seed['source_role']=role
                        if use_search_only:seed['method']='provisional_search_only_plane_seed'
                    except AcquisitionError as exc:seed['provisional_plane_rejection']=str(exc)
            anchor['search_seed']=seed;save()
            if not allowed[0]<seed['z_um']<allowed[1]:
                raise SafetyError(f"Focus search seed {seed['z_um']:.3f} um for ({x:.3f}, {y:.3f}) lies outside controller-safe Z bounds {allowed}; no travel to this anchor was attempted.")
            scope.travel(float(x),float(y))
            if len(source):_move_z(scope,seed['z_um'])
            folder=attempt/f'anchor_{index:03d}'
            # An adapter may choose a textured in-field region once, then hold
            # that exact region fixed over the entire Z curve.
            if hasattr(scope,'begin_focus_anchor'):
                try:scope.begin_focus_anchor()
                except AcquisitionError as exc:
                    if hasattr(scope,'save_focus_selection'):scope.save_focus_selection(folder/'selection')
                    anchor.update(status='autofocus_failed',reason=str(exc));save();continue
                if hasattr(scope,'save_focus_selection'):scope.save_focus_selection(folder/'selection')
            def progress(phase):
                if callback is None:return None
                return lambda frame,sample:callback('focus_surface',frame,
                    {**sample,'anchor_index':index,'anchor_xy_um':[float(x),float(y)],'search_phase':phase})
            anchor_budget=AcquisitionBudget(float(options.get('max_anchor_elapsed_s',180.)),int(options.get('max_anchor_captures',48)))
            anchor_scope=BudgetScope(scope,anchor_budget)
            current_z=scope.position().z
            lo=max(allowed[0],current_z-half);hi=min(allowed[1],current_z+half)
            anchor['local_search_bounds_um']=[lo,hi];save()
            if hi-lo<2*coarse:raise SafetyError('Insufficient controller-safe Z interval for local focus verification.')
            quick_half=min(2.,half)
            if (seed['method'] in ('provisional_measured_plane_search_seed','provisional_search_only_plane_seed') and
                current_z-quick_half>=allowed[0] and current_z+quick_half<=allowed[1]):
                # Six-image autofocus is conditional on a resolved prior
                # plane. Saved-curve replay supports it but is not live
                # validation; its independent confirmation and full local
                # sweep fallback remain mandatory for each new anchor.
                anchor['local_search_strategy']='quick_plane_seeded_with_full_sweep_fallback'
                anchor['quick_half_range_um']=quick_half;save()
                local=quick_autofocus(anchor_scope,quick_half,folder/'local',progress('local'),
                    fallback_half_range_um=half,fallback_coarse_step_um=coarse,fallback_fine_step_um=fine)
            elif lo==current_z-half and hi==current_z+half:
                local=autofocus(anchor_scope,half,coarse,fine,folder/'local',progress('local'))
            else:
                local=autofocus_range(anchor_scope,lo,hi,coarse,fine,folder/'local',progress('local'))
            anchor['focus_budget']=anchor_budget.report()
            anchor['local_result']=asdict(local)
            selected=local
            needs_initial_search=bool(options.get('recover_initial_unresolved',False)) and len(existing)==0
            if not local.success and recovery is not None and ('Peak at search boundary' in local.reason or needs_initial_search):
                if options.get('adaptive_recovery',False):
                    selected=recover_focus(anchor_scope,float(recovery[0]),float(recovery[1]),folder/'recovery',callback=progress('recovery'))
                else:
                    selected=autofocus_range(anchor_scope,float(recovery[0]),float(recovery[1]),coarse,fine,
                                             folder/'recovery',progress('recovery'))
                anchor['recovery_result']=asdict(selected)
            anchor['focus_budget']=anchor_budget.report()
            if not selected.success:
                anchor.update(status='autofocus_failed',reason=selected.reason)
                save();continue
            frame=scope.capture()
            physical=frame.metadata.get('physical_stage_position',asdict(frame.position))
            if np.linalg.norm([physical['x']-x,physical['y']-y])>.5 or abs(frame.position.z-selected.z_um)>.15:
                raise AcquisitionError('Focus anchor capture disagrees with its measured XYZ position.')
            save_frame(folder,'anchor',frame)
            point=[frame.position.x,frame.position.y,frame.position.z]
            points.append(point);newly_verified.append(point)
            anchor.update(status='verified',position_um=point,image=(folder/'anchor.tiff').relative_to(root).as_posix())
            save()
            checkpoint_observation(scope,frame,folder,'verified_focus_anchor',anchor,selected_raw=folder/'anchor.tiff')
        array=np.asarray(points,dtype=float).reshape(-1,3)
        if len(array)<3 or np.linalg.matrix_rank(array[:,:2]-array[:,:2].mean(axis=0))<2:
            record.update(state='complete',mode='per_tile_autofocus',reason='Fewer than three noncollinear verified focus anchors.')
            save();return None,points
        origin=array[:,:2].mean(axis=0)
        design=np.column_stack([array[:,:2]-origin,np.ones(len(array))])
        coefficients=np.linalg.lstsq(design,array[:,2],rcond=None)[0]
        residual=array[:,2]-design@coefficients
        record.update(fit_origin_xy_um=origin.tolist(),fit_coefficients=coefficients.tolist(),
                      residuals_um=residual.tolist(),measured_max_residual_um=float(np.max(np.abs(residual))))
        if np.max(np.abs(residual))>residual_limit:
            record.update(state='complete',mode='per_tile_autofocus',reason='Verified anchors disagree with a plane; retain every anchor and autofocus locally.')
            save();return None,points
        # Prechecking ALL residuals prevents FocusMap's robust fitter from
        # silently excluding an inconsistent corner and shrinking coverage.
        surface=FocusMap(points,max_residual_um=residual_limit)
        if len(surface.points)!=len(points):raise AcquisitionError('Focus surface unexpectedly discarded verified anchors.')
        record.update(state='complete',mode='surface',rms_um=surface.rms_um,
                      verified_hull_xy_um=surface.points[ConvexHull(surface.points[:,:2]).vertices,:2].tolist())
        save();return surface,points
    except BaseException as exc:
        record.update(state='failed',error_type=type(exc).__name__,reason=str(exc));save();raise
