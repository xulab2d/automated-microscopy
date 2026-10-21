"""Small deterministic image simulation; no microscope connection."""
import numpy as np,pytest
from flakefinder.sparse_acquisition import SparseEdgeOptions,discover_sparse_outline
from flakefinder.full_chip import _anchor_points
from test_chip_discovery import RECT
from sparse_profile_fixture import ImageStage,profile_for


def test_sparse_brackets_find_four_edges_with_budget_and_no_frontier(tmp_path):
    scope=ImageStage([RECT]);reference=scope.capture()
    options=SparseEdgeOptions([20,1200,20,1100],coarse_step_um=400,cell_um=5,margin_um=20)
    result=discover_sparse_outline(scope,[500,500],scope.transform,reference,tmp_path,options,edge_validation_profile=profile_for(reference))
    assert result['state']=='estimated' and len(result['records'])<=17
    assert len(result['estimated_outline']['lines'])==4
    assert any(r['phase'].startswith('bisect_') for r in result['records'])
    assert not any('frontier' in r['phase'] for r in result['records'])
    assert result['all_chips_found'] is False
    before=scope.sequence
    replay=discover_sparse_outline(scope,[500,500],scope.transform,reference,tmp_path,options,edge_validation_profile=profile_for(reference),resume=True)
    assert replay['state']=='estimated' and scope.sequence==before


def test_sparse_budget_and_spread_anchor_selection():
    with pytest.raises(ValueError,match='32'):SparseEdgeOptions([0,100,0,100],max_probes=33)
    points=np.array([[0,0],[20,0],[30,5],[0,1000],[1000,0],[980,0],[1000,1000],[980,990]])
    selected=_anchor_points(points,4)
    distances=np.linalg.norm(selected[:,None,:]-selected[None,:,:],axis=2)+np.eye(4)*10000
    assert distances.min()>500


def test_triangular_hull_retains_measured_interior_fourth_anchor():
    points=np.array([[0,0],[1000,0],[0,1000],[300,300]])
    selected=_anchor_points(points,4)
    assert len(selected)==4
    assert any(np.array_equal(p,[300,300]) for p in selected)


def test_only_three_observations_cannot_plan_four_anchor_plane():
    from flakefinder.model import AcquisitionError
    with pytest.raises(AcquisitionError,match='four'):
        _anchor_points(np.array([[0,0],[1000,0],[0,1000]]),4)


@pytest.mark.parametrize('matrix,direction',[
    ([[1,0],[0,1]],[1,0]),([[-1,0],[0,1]],[-1,0]),
    ([[0,-1],[1,0]],[0,1]),([[0,1],[-1,0]],[0,-1])])
def test_offcenter_transition_is_recentered_along_normal_inside_bracket(matrix,direction):
    from flakefinder.sparse_acquisition import _normal_recenter_target
    labels=np.ones((100,100),np.uint8);labels[:,85:]=2
    labels[20:35,20:35]=2  # Disconnected contamination must not move the boundary proposal.
    direction=np.asarray(direction);position=direction*500
    target=_normal_recenter_target(labels,(100,100,3),matrix,position,direction,[0,0],0,1000)
    assert target is not None
    shift=target-position
    assert 25<shift@direction<40
    assert abs(shift[0]*direction[1]-shift[1]*direction[0])<1e-8
    assert _normal_recenter_target(labels,(100,100,3),matrix,position,direction,[0,0],0,510) is None
    assert _normal_recenter_target(3-labels,(100,100,3),matrix,position,direction,[0,0],0,1000) is None


def test_corner_seed_uses_local_support_not_whole_field_fraction(tmp_path):
    scope=ImageStage([RECT]);reference=scope.capture()
    options=SparseEdgeOptions([20,1200,20,1100],coarse_step_um=400,cell_um=5,margin_um=20)
    result=discover_sparse_outline(scope,[220,220],scope.transform,reference,tmp_path,options,edge_validation_profile=profile_for(reference))
    first=result['records'][0]
    assert first['fractions']['substrate']<.55 and first['classification']=='unknown'
    assert result['seed_verification']['classification']=='substrate_seed'
    assert result['seed_verification']['patch_substrate_fraction']>=.9
    assert result['rays'] and not scope.stopped


def test_unverified_seed_is_unresolved_and_does_not_stop_hardware(tmp_path):
    scope=ImageStage([RECT]);reference=scope.capture()
    result=discover_sparse_outline(scope,[100,100],scope.transform,reference,tmp_path,
        SparseEdgeOptions([20,1200,20,1100],coarse_step_um=400,cell_um=5,margin_um=20),edge_validation_profile=profile_for(reference))
    assert result['state']=='unresolved' and result['reason']=='seed_not_verified'
    assert result['estimated_outline']['status']=='unresolved'
    assert len(result['records'])==1 and not result['rays']
    assert not scope.stopped and scope.armed


def test_actual_capture_failure_still_aborts_and_stops(tmp_path,monkeypatch):
    from flakefinder.model import AcquisitionError
    scope=ImageStage([RECT]);reference=scope.capture()
    def fail():raise AcquisitionError('camera transport failure')
    monkeypatch.setattr(scope,'capture',fail)
    with pytest.raises(AcquisitionError,match='camera transport failure'):
        discover_sparse_outline(scope,[500,500],scope.transform,reference,tmp_path,
            SparseEdgeOptions([20,1200,20,1100],coarse_step_um=400,cell_um=5,margin_um=20),edge_validation_profile=profile_for(reference))
    assert scope.stopped

@pytest.mark.parametrize('old_reason',['geometric_probe_budget_exhausted','edge_focus_unresolved','time_budget_exhausted','capture_budget_exhausted'])
def test_rectangle_replays_saved_rejected_side_without_motion(tmp_path,monkeypatch,old_reason):
    import json
    import flakefinder.rectangle_edges as rectangle
    from flakefinder.model import atomic_json
    scope=ImageStage([RECT]);reference=scope.capture()
    base=SparseEdgeOptions([20,1200,20,1100],coarse_step_um=400,cell_um=5,margin_um=20)
    result=discover_sparse_outline(scope,[500,500],scope.transform,reference,tmp_path,base,edge_validation_profile=profile_for(reference))
    donor=json.loads(json.dumps(result))
    east=next(r for r in donor['records'] if r.get('edge') and r['edge']['normal'][0]>.9)
    measured=east.pop('edge');east['edge']=None;east['phase']='focused_bisect_east'
    donor['state']='unresolved';donor['reason']=old_reason;donor['budget_exhausted']=True
    donor['rays']['east']['status']='unresolved';donor['rays']['east']['edge_image']=None
    donor['algorithm_source_sha256']='legacy-source';donor['fingerprint']='legacy-fingerprint'
    donor['options']={k:v for k,v in donor['options'].items() if not k.startswith('rectangle_')}
    atomic_json(tmp_path/'discovery.json',donor)
    count=[]
    def refit(frame,matrix,normal,**kwargs):
        count.append(frame.position);return measured.copy(),{'accepted':True,'focus_needed':False}
    monkeypatch.setattr(rectangle,'measure_rectangle_side',refit)
    def forbidden(*a,**k):raise AssertionError('Saved rectangle must require no capture, motion, or AF')
    monkeypatch.setattr(scope,'capture',forbidden);monkeypatch.setattr(scope,'travel',forbidden)
    upgraded=SparseEdgeOptions(**donor['options'],rectangle_mode=True,rectangle_additional_probes=4)
    replay=discover_sparse_outline(scope,[500,500],scope.transform,reference,tmp_path,upgraded,
        edge_validation_profile=profile_for(reference),resume=True,continue_unresolved=True,allow_source_change=True)
    assert replay['state']=='estimated' and len(replay['estimated_outline']['lines'])==4
    assert replay['rectangle_replay_closed'] and len(count)==1
    assert replay['previous_operation_outcomes'][-1]['reason']==old_reason
    assert not replay.get('reason') and not replay.get('budget_exhausted')
    assert replay['records']==donor['records']
    assert replay['operation_budget']['captures_started']==0
    assert replay['execution_changes'][-1]['previous_algorithm_source_sha256']=='legacy-source'
    assert replay['rectangle_probe_allowance']['maximum_total_frames']==max(32,len(donor['records']))+4
    # Cached raw reanalysis is pinned to the model and must not be repeated.
    replay2=discover_sparse_outline(scope,[500,500],scope.transform,reference,tmp_path,upgraded,
        edge_validation_profile=profile_for(reference),resume=True,continue_unresolved=True,allow_source_change=True)
    assert len(count)==1 and replay2['state']=='estimated'
    assert replay2['rectangle_probe_allowance']['maximum_total_frames']==replay['rectangle_probe_allowance']['maximum_total_frames']


def test_rectangle_upgrade_does_not_allow_reference_or_bounds_change(tmp_path):
    from flakefinder.model import SafetyError
    scope=ImageStage([RECT]);reference=scope.capture()
    base=SparseEdgeOptions([20,1200,20,1100],coarse_step_um=400,cell_um=5,margin_um=20)
    discover_sparse_outline(scope,[500,500],scope.transform,reference,tmp_path,base,edge_validation_profile=profile_for(reference))
    changed=SparseEdgeOptions([20,1190,20,1100],coarse_step_um=400,cell_um=5,margin_um=20,rectangle_mode=True,rectangle_additional_probes=4)
    with pytest.raises(SafetyError,match='configuration/reference'):
        discover_sparse_outline(scope,[500,500],scope.transform,reference,tmp_path,changed,
            edge_validation_profile=profile_for(reference),resume=True,continue_unresolved=True,allow_source_change=True)


def test_rectangle_geometry_rejection_never_triggers_autofocus(tmp_path,monkeypatch):
    import flakefinder.sparse_acquisition as sparse
    import flakefinder.rectangle_edges as rectangle
    import flakefinder.focus_recovery as recovery
    scope=ImageStage([RECT]);reference=scope.capture()
    monkeypatch.setattr(sparse,'validated_coherent_edge',lambda *a:(None,{'accepted':False}))
    monkeypatch.setattr(rectangle,'measure_rectangle_side',lambda *a,**k:(None,{'accepted':False,'focus_needed':False,'reason':'insufficient span'}))
    def forbidden(*a,**k):raise AssertionError('Geometry rejection must not trigger AF')
    monkeypatch.setattr(recovery,'recover_focus',forbidden)
    result=discover_sparse_outline(scope,[500,500],scope.transform,reference,tmp_path,
        SparseEdgeOptions([20,1200,20,1100],coarse_step_um=400,cell_um=5,margin_um=20,max_probes=8,rectangle_mode=True),
        edge_validation_profile=profile_for(reference),focus_search_bounds_um=[60,140])
    assert result['state']=='unresolved' and not result.get('edge_focus_attempts')
    assert result['operation_budget']['captures_started']<=8


def test_rectangle_saved_lab_candidate_replay(tmp_path):
    """Optional actual raw replay, read-only donor; set FLAKEFINDER_RECTANGLE_REPLAY to run folder."""
    import os,json
    from pathlib import Path
    from flakefinder.model import Limits,CameraTransform,Position,atomic_json
    from flakefinder.imaging import load_frame
    from flakefinder.chip_discovery import file_hash
    run=os.environ.get('FLAKEFINDER_RECTANGLE_REPLAY')
    if not run:pytest.skip('No saved lab replay requested')
    run=Path(run);source=run/'discovery'/'candidate001';path=source/'discovery.json'
    original_hash=file_hash(path);state=json.loads(path.read_text())
    for record in state['records']:
        record['image']=str(source/record['image']);record['labels_file']=str(source/record['labels_file'])
        record['hashes']={str(source/k):v for k,v in record['hashes'].items()}
    atomic_json(tmp_path/'discovery.json',state)
    class NoHardware:
        armed=True;limits=Limits(**state['limits'])
        def check_cancel(self):pass
        def position(self):return Position(*state['records'][0]['position_um'])
        def __getattr__(self,name):
            if name in ('lease','focus_target_tolerance_um'):return None
            if name in ('capture','travel','move_z','stop'):raise AssertionError('Offline replay attempted hardware: '+name)
            raise AttributeError(name)
    result=discover_sparse_outline(NoHardware(),state['seed_xy_um'],CameraTransform(**state['transform']),
        load_frame(run/'calibration'/'substrate.tiff'),tmp_path,
        SparseEdgeOptions(**{**state['options'],'rectangle_mode':True,'rectangle_additional_probes':4}),
        context=state['context'],edge_validation_profile=state['edge_validation_profile'],
        focus_search_bounds_um=state.get('edge_focus_policy',{}).get('search_bounds_um'),
        resume=True,continue_unresolved=True,allow_source_change=True)
    assert result['state']=='estimated' and len(result['estimated_outline']['lines'])==4
    assert result['operation_budget']['captures_started']==0 and result['rectangle_replay_closed']
    assert len(result['rectangle_reanalysis'])==1
    assert next(iter(result['rectangle_reanalysis'])).endswith('probe_000006.tiff')
    assert result['records']==state['records'] and file_hash(path)==original_hash

    from flakefinder.full_chip import build_full_chip_plan,FullChipOptions
    plan=build_full_chip_plan(tmp_path,load_frame(run/'calibration'/'substrate.tiff'),CameraTransform(**state['transform']),
        FullChipOptions(),estimated_outline=result['estimated_outline'])
    assert plan['tile_count']>0 and len(plan['anchor_xy_um'])==4
    assert plan['focus_anchor_mode']=='chip_edges'
    assert any(a['source_image'].endswith('probe_000006.tiff') for a in plan['edge_anchor_evidence']['anchors'])


def test_rectangle_fresh_five_mm_chip_uses_bounded_adaptive_probes(tmp_path):
    from flakefinder.model import Limits,Position
    scope=ImageStage([[[2500,2500],[7500,2500],[7500,7500],[2500,7500]]],shape=(400,500))
    scope.p=Position(5000,5000,100);scope.limits=Limits(0,11000,0,11000,50,150,3000,10)
    reference=scope.capture()
    result=discover_sparse_outline(scope,[5000,5000],scope.transform,reference,tmp_path,
        SparseEdgeOptions([20,10980,20,10980],rectangle_mode=True,cell_um=20),
        edge_validation_profile=profile_for(reference),focus_search_bounds_um=[60,140])
    assert result['state']=='estimated' and len(result['estimated_outline']['lines'])==4
    assert len(result['records'])<=17 and not result.get('edge_focus_attempts')
    east=[r['requested_xy_um'][0]-5000 for r in result['records'] if r['phase']=='coarse_east']
    assert east[:2]==[1500,3750]
