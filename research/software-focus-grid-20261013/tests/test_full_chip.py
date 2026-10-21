"""Complete coverage and focus provenance regressions without hardware access."""
from dataclasses import asdict
import copy,hashlib,json,math

import cv2,numpy as np,pytest

import flakefinder.full_chip as full
from flakefinder.autofocus import FocusResult
from flakefinder.imaging import load_frame,save_frame
from flakefinder.model import Position,SafetyError,AcquisitionError,Cancelled,atomic_json
from test_chip_discovery import ImageStage,RECT,IRREGULAR,run_boundary


class FocusStage(ImageStage):
    def move_z(self,z):
        self.check_cancel();self.limits.check(Position(self.p.x,self.p.y,z))
        assert abs(z-self.p.z)<=self.limits.max_z_step_um
        self.p=Position(self.p.x,self.p.y,z)


@pytest.fixture
def planned(tmp_path):
    boundary=tmp_path/'boundary';old,reference,discovery=run_boundary(boundary)
    scope=FocusStage([RECT]);scope.sequence=old.sequence
    options=full.FullChipOptions(focus_roi_um=40,focus_half_range_um=5,focus_step_um=2,max_local_focus_offset_um=150,
                                focus_max_extrapolation_um=0,max_local_focus_attempts=100)
    plan=full.build_full_chip_plan(boundary,reference,scope.transform,options,'arbitrary-chip')
    return scope,reference,plan,tmp_path/'full'


@pytest.fixture
def fast_focus(monkeypatch):
    calls=[]
    def focus(adapter,*args,**kwargs):
        frame=adapter.capture();calls.append(frame)
        return FocusResult(True,adapter.position().z,1.,.2,'Synthetic measured focus',[])
    def surface(adapter,centres,options,output,callback=None,seed_points=None):
        points=list(seed_points or [])
        for x,y in centres:
            adapter.travel(x,y);result=focus(adapter)
            points.append([x,y,result.z_um])
        return full._strict_surface(points,options['max_residual_um']),points
    monkeypatch.setattr(full,'autofocus',focus);monkeypatch.setattr(full,'autofocus_range',focus)
    monkeypatch.setattr(full,'prepare_focus_surface',surface)
    return calls


def resign(plan):
    plan['fingerprint']=hashlib.sha256(json.dumps({k:v for k,v in plan.items() if k!='fingerprint'},sort_keys=True).encode()).hexdigest()


def test_full_plan_covers_chip_interior_and_edges_with_substrate_supported_anchors(planned):
    scope,reference,plan,_=planned
    assert plan['tile_count']>20
    h,w=reference.rgb.shape[:2];centres=np.asarray(plan['tile_centres_um'])
    # Every dense truth sample, including each physical corner, lies in a field.
    truth=np.array([(x,y) for x in np.linspace(200,800,61) for y in np.linspace(200,800,61)])
    offsets=(truth[:,None,:]-centres[None,:,:])@np.linalg.inv(scope.matrix).T
    assert np.all(((abs(offsets[:,:,0])<=(w-1)/2)&(abs(offsets[:,:,1])<=(h-1)/2)).any(axis=1))
    for x,y in plan['anchor_xy_um']:
        assert 225<x<775 and 225<y<775
    assert plan['focus_policy']['nearby_height_image_focus_verified'] is False


def test_rotated_focus_roi_has_physical_coordinates_not_full_field_centre():
    theta=.21;matrix=[[math.cos(theta),-math.sin(theta)],[math.sin(theta),math.cos(theta)]]
    scope=FocusStage([RECT],matrix=matrix);reference=scope.capture()
    central=full.FocusROIAdapter(scope,40,scope.transform).capture()
    assert central.position==scope.position()
    cropped=full.FocusROIAdapter(scope,40,scope.transform,[10,20,50,60],reference).capture()
    expected=scope.transform.stage_xy([29.5,39.5],reference)
    assert np.allclose([cropped.position.x,cropped.position.y],expected)
    assert cropped.metadata['physical_stage_position']==asdict(scope.position())
    assert cropped.rgb.shape==(40,40,3)


def test_full_runner_saves_full_frames_not_focus_crops_and_complete_provenance(planned,fast_focus):
    scope,reference,plan,folder=planned
    result=full.run_full_chip(scope,plan,reference,folder)
    assert result['raw_coverage_complete'] and len(result['completed'])==plan['tile_count']
    assert fast_focus and all(max(f.rgb.shape[:2])<=40 for f in fast_focus)
    for name,row in result['completed'].items():
        assert load_frame(folder/'tiles'/(name+'.tiff')).rgb.shape==reference.rgb.shape
        assert row['full_chip_focus']['extrapolated'] is False
        assert row['full_chip_focus']['thin_flake_focus_validated'] is False
    assert json.loads((folder/'coverage-plan.json').read_text())['fingerprint']==plan['fingerprint']


def test_no_roi_edge_uses_nearby_measured_height_but_never_claims_field_focus(planned,fast_focus,monkeypatch):
    scope,reference,plan,folder=planned
    monkeypatch.setattr(full,'supported_roi',lambda *args:None)
    result=full.run_full_chip(scope,plan,reference,folder)
    edge=[r for r in result['completed'].values() if r['focus_method']=='nearby_measured_height']
    assert edge and result['state']=='complete_with_focus_limits'
    for row in edge:
        assert row['focus'] is None and row['focus_limited']
        assert not row['full_chip_focus']['focus_verified']
        assert row['full_chip_focus']['offset_um']<=150


def test_focus_offset_limit_blocks_unsupported_field_before_capture(planned,fast_focus,monkeypatch):
    scope,reference,plan,folder=planned
    plan['options']['max_local_focus_offset_um']=0;resign(plan)
    monkeypatch.setattr(full,'supported_roi',lambda *args:None)
    with pytest.raises(AcquisitionError,match='bounded local focus'):
        full.run_full_chip(scope,plan,reference,folder)
    manifest=json.loads((folder/'manifest.json').read_text())
    assert manifest['state']=='failed' and scope.stopped
    assert not manifest.get('raw_coverage_complete')


def test_stop_preserves_evidence_and_resume_skips_completed_fields(planned,fast_focus):
    scope,reference,plan,folder=planned
    def stop(phase,frame,info):
        if phase=='tile':(folder/'STOP').write_text('stop')
    with pytest.raises(Cancelled):full.run_full_chip(scope,plan,reference,folder,callback=stop)
    manifest=json.loads((folder/'manifest.json').read_text());assert len(manifest['completed'])==1
    name=next(iter(manifest['completed']));raw=folder/'tiles'/(name+'.tiff');before=raw.read_bytes()
    last_xy=scope.moves[-1];assert last_xy==plan['tile_centres_um'][0]
    (folder/'STOP').unlink();scope.stopped=False;scope.armed=True;old_moves=len(scope.moves)
    result=full.run_full_chip(scope,plan,reference,folder,resume=True)
    assert result['raw_coverage_complete'] and raw.read_bytes()==before
    assert scope.moves[old_moves]==plan['tile_centres_um'][1]


def test_changed_plan_reference_and_live_profile_are_rejected_before_travel(planned,fast_focus):
    scope,reference,plan,folder=planned
    bad=copy.deepcopy(plan);bad['tile_centres_um'][0][0]+=1
    with pytest.raises(SafetyError,match='altered'):full.run_full_chip(scope,bad,reference,folder)
    modified=copy.deepcopy(reference);modified.rgb[0,0,0]+=1
    with pytest.raises(SafetyError,match='reference image differs'):full.run_full_chip(scope,plan,modified,folder)
    capture=scope.capture
    def changed():
        frame=capture();frame.metadata['objective_position']=3;return frame
    scope.capture=changed
    with pytest.raises(SafetyError,match='Current camera'):full.run_full_chip(scope,plan,reference,folder)
    assert not scope.moves


def test_corrupted_complete_field_blocks_resume(planned,fast_focus):
    scope,reference,plan,folder=planned
    full.run_full_chip(scope,plan,reference,folder)
    with (folder/'tiles/tile_000000.tiff').open('ab') as stream:stream.write(b'bad')
    before=len(scope.moves)
    with pytest.raises(AcquisitionError,match='evidence changed'):full.run_full_chip(scope,plan,reference,folder,resume=True)
    assert len(scope.moves)==before


def test_partial_anchor_checkpoint_survives_stop_and_is_verified_on_resume(planned,fast_focus,monkeypatch):
    scope,reference,plan,folder=planned
    def partial(adapter,centres,options,output,callback=None,seed_points=None):
        x,y=centres[0];adapter.travel(x,y);frame=adapter.capture();save_frame(output,'anchor',frame)
        atomic_json(output/'focus-surface.json',{'anchors':[{'status':'verified','position_um':[x,y,frame.position.z],'image':'anchor.tiff'}]})
        raise Cancelled('stop during anchor measurement')
    monkeypatch.setattr(full,'prepare_focus_surface',partial)
    with pytest.raises(Cancelled):full.run_full_chip(scope,plan,reference,folder)
    result=json.loads((folder/'manifest.json').read_text())
    assert len(result['focus_points_um'])==1 and len(result['focus_anchor_evidence'])==2
    assert not result.get('anchors_prepared')
    scope.stopped=False;scope.armed=True
    (folder/'focus-surface/anchor.json').write_text('{}')
    with pytest.raises(AcquisitionError,match='focus-anchor evidence changed'):
        full.run_full_chip(scope,plan,reference,folder,resume=True)


def test_inconsistent_focus_points_force_local_focus_instead_of_dropping_corner():
    assert full._strict_surface([[0,0,100],[100,0,100],[100,100,100],[0,100,110]],1) is None
    surface=full._strict_surface([[0,0,100],[100,0,100],[100,100,100],[0,100,100]],1)
    assert surface.predict(50,50)==pytest.approx(100)
    with pytest.raises(AcquisitionError):surface.predict(101,50)


def test_estimated_outline_accepts_stopped_evidence_and_validates_during_scientific_raster(planned,fast_focus):
    scope,reference,old,folder=planned
    source=folder.parent/'boundary/discovery.json';discovery=json.loads(source.read_text())
    discovery.update(state='stopped');discovery.pop('enclosure',None);atomic_json(source,discovery)
    estimate={'polygon_um':[[170,170],[830,170],[830,830],[170,830]],'uncertainty_um':30,'method':'synthetic sparse edge brackets'}
    plan=full.build_full_chip_plan(source.parent,reference,scope.transform,old['options'],estimated_outline=estimate)
    assert plan['boundary_estimate']['status']=='estimated'
    result=full.run_full_chip(scope,plan,reference,folder)
    assert result['planned_area_acquired'] and result['chip_extent_validated']
    assert result['boundary_validation']['acquired_raster_evidence']['interior_fully_acquired']


def test_underestimated_outline_never_claims_entire_chip_covered(planned,fast_focus):
    scope,reference,old,folder=planned
    estimate={'polygon_um':[[220,220],[780,220],[780,780],[220,780]],'uncertainty_um':10,'method':'deliberately wrong test estimate'}
    plan=full.build_full_chip_plan(folder.parent/'boundary',reference,scope.transform,old['options'],estimated_outline=estimate)
    result=full.run_full_chip(scope,plan,reference,folder)
    assert result['planned_area_acquired'] and not result['chip_extent_validated']
    assert result['state']=='complete_with_boundary_limits'


def test_bounded_plane_uses_all_residuals_and_reports_conditional_uncertainty():
    options=full.FullChipOptions()
    points=[[0,0,100],[6000,0,101],[6000,6000,103],[0,6000,102]]
    result=full.bounded_plane_prediction(points,7000,3000,options)
    assert result['predicted_z_um']==pytest.approx(102+1/6)
    assert result['extrapolation_distance_um']==pytest.approx(1000)
    assert result['plane_prediction_accepted'] and not result['local_focus_verified']
    assert result['sample_rms_um']<1e-9 and result['measurement_floor_um']==.5
    with pytest.raises(AcquisitionError,match='distance'):full.bounded_plane_prediction(points,7600,3000,options)
    with pytest.raises(AcquisitionError,match='all measured anchors'):
        full.bounded_plane_prediction([*points,[3000,3000,120]],7000,3000,options)
    options.focus_max_prediction_sigma_um=.01
    with pytest.raises(AcquisitionError,match='uncertainty'):full.bounded_plane_prediction(points,3000,3000,options)
