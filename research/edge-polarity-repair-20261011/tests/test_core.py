from dataclasses import asdict
import threading
import cv2, numpy as np, pytest
from flakefinder.model import Position,Limits,Frame,CameraTransform,SafetyError,AcquisitionError,Cancelled
from flakefinder.simulator import SimulatedMicroscope
from flakefinder.autofocus import autofocus,evaluate_curve
from flakefinder.calibration import calibrate_camera,FocusMap
from flakefinder.chips import detect_chips
from flakefinder.segmentation import segment_flakes,ContrastModel,train_contrast_model
from flakefinder.scan import ScanPlan,tile_centres,Mosaic,run_scan,identify_chips,deduplicate
from flakefinder.imaging import save_frame,load_frame

@pytest.fixture
def scope():
    s=SimulatedMicroscope()
    s.arm(Limits(-100,2400,-100,2400,60,150,1200,25))
    return s

def transform(): return CameraTransform([[1,0],[0,1]],(1,1),True)

def test_limits_reject_unbounded_or_combined_motion():
    with pytest.raises(SafetyError): Position(float('nan'),0,0)
    with pytest.raises(SafetyError): Limits(0,float('inf'),0,1,0,1)
    lim=Limits(0,100,0,100,0,10,10,2)
    for target in [Position(101,0,5),Position(15,0,5),Position(0,0,8),Position(1,1,6)]:
        with pytest.raises(SafetyError): lim.check_move(Position(0,0,5),target)

def test_unarmed_and_cancelled_scope_refuses_motion(scope):
    scope.stop()
    with pytest.raises(Cancelled): scope.move_z(99)
    scope.cancel.clear()
    with pytest.raises(SafetyError): scope.move_z(99)

def test_autofocus_finds_known_focus(scope,tmp_path):
    scope.move_z(104)
    result=autofocus(scope,10,2,0.5,tmp_path)
    assert result.success,result.reason
    assert abs(scope.position().z-100)<=0.6
    assert (tmp_path/'focus-result.json').exists()

def test_focus_rejects_flat_boundary_and_untextured_curves():
    def samples(scores,texture=.1): return [{'z_um':i,'score':s,'texture':texture} for i,s in enumerate(scores)]
    assert not evaluate_curve(samples([1,1.01,1])).success
    assert not evaluate_curve(samples([1,2,3])).success
    assert not evaluate_curve(samples([1,5,1],.001)).success

def test_autofocus_does_not_cross_limit(scope):
    before=scope.position()
    with pytest.raises(SafetyError): autofocus(scope,100,5,1)
    assert scope.position()==before

def test_cancel_does_not_return_to_start(scope):
    scope.move_z(104)
    def stop_after_first(f,m): scope.stop()
    with pytest.raises(Cancelled): autofocus(scope,8,2,0.5,callback=stop_after_first)
    assert scope.position().z==96

def test_calibration_recovers_axis_direction_and_scale(scope):
    t,quality=calibrate_camera(scope,20)
    assert np.allclose(t.matrix,np.eye(2),atol=.03)
    assert scope.position()==Position(250,250,100)
    assert min(quality['responses'])>.2

def test_chip_detection_preserves_missing_slot(scope):
    chips,mask=detect_chips(scope.world,min_area_px=30000,expected_count=15,grid_shape=(4,4))
    assert len(chips)==15
    assert 'C4' not in [c.id for c in chips]
    assert set(c.id for c in chips)=={f'{r}{c}' for r in 'ABCD' for c in range(1,5)}-{'C4'}

def test_count_mismatch_and_clipped_chip_rejected(scope):
    with pytest.raises(AcquisitionError): detect_chips(scope.world,expected_count=16,min_area_px=30000)
    with pytest.raises(AcquisitionError): detect_chips(scope.world[100:],min_area_px=30000)

def test_focus_plane_requires_interpolation():
    points=[[0,0,10],[100,0,11],[0,100,12],[100,100,13]]
    plane=FocusMap(points)
    assert plane.predict(50,50)==pytest.approx(11.5)
    with pytest.raises(AcquisitionError): plane.predict(300,300)
    with pytest.raises(ValueError): FocusMap([[0,0,1],[1,1,2],[2,2,3]])

def test_tiff_preserves_16bit_rgb_and_metadata(scope,tmp_path):
    f=scope.capture();save_frame(tmp_path,'frame',f)
    restored=load_frame(tmp_path/'frame.tiff')
    assert np.array_equal(restored.rgb,f.rgb)
    assert restored.position==f.position
    assert restored.pixel_size_um==f.pixel_size_um

def test_segmentation_returns_physical_measurements_and_no_invented_layers(scope):
    # Crop away substrate edges; flake algorithm is intentionally for chip interiors.
    frame=scope.capture()
    frame.rgb=frame.rgb[30:-30,40:-40].copy()
    flakes,mask,q=segment_flakes(frame,transform(),min_area_um2=10)
    assert len(flakes)>5
    assert all(f.label=='unclassified' for f in flakes)
    assert all(f.area_um2>0 and len(f.stage_xy_um)==2 for f in flakes)

def test_material_model_rejects_context_mismatch():
    context={'substrate_nm':285,'objective':'10x','illumination':'brightfield'}
    rng=np.random.default_rng(2)
    samples=[{'label':label,'contrast_rgb':(np.array(mean)+rng.normal(0,.002,3)).tolist()}
             for label,mean in [('monolayer_candidate',[.04,.08,.02]),('bilayer_candidate',[.08,.16,.04])] for _ in range(8)]
    model=ContrastModel(train_contrast_model(samples,context))
    assert model.classify([.04,.08,.02],context)[0]=='monolayer_candidate'
    assert model.classify([.04,.08,.02],{'substrate_nm':90})[0]=='calibration_mismatch'
    assert model.classify([.9,.9,.9],context)[0]=='out_of_distribution'

def test_transform_change_is_blocked(scope):
    f=scope.capture();f.pixel_size_um=(.5,.5)
    with pytest.raises(SafetyError): transform().stage_xy([10,10],f)

def test_scan_plan_covers_rotated_camera(scope):
    f=scope.capture();angle=.2
    t=CameraTransform([[np.cos(angle),-np.sin(angle)],[np.sin(angle),np.cos(angle)]],(1,1),True)
    plan=ScanPlan([0,1000,0,900],autofocus_every=0,mode='survey')
    mosaic=Mosaic(plan.bounds_um,5)
    for x,y in tile_centres(plan,f,t):
        scope.travel(x,y);mosaic.add(scope.capture(),t)
    assert np.mean(mosaic.weight>0)>.999

def test_survey_pipeline_and_resume(scope,tmp_path):
    plan=ScanPlan([0,2300,0,2300],overlap=.15,autofocus_every=0,mode='survey')
    manifest=run_scan(scope,plan,transform(),tmp_path)
    assert manifest['state']=='complete'
    assert manifest['tile_count']==len(manifest['completed'])
    chips=identify_chips(tmp_path)
    assert len(chips)==15
    before=scope.sequence
    again=run_scan(scope,plan,transform(),tmp_path,resume=True)
    assert scope.sequence==before+1 # only initial metadata validation frame
    assert again['state']=='complete'
    with pytest.raises(SafetyError):
        run_scan(scope,ScanPlan([0,2200,0,2300],autofocus_every=0,mode='survey'),transform(),tmp_path,resume=True)

def test_dedup_does_not_merge_neighbours_or_different_chips():
    base={'id':'one','chip_id':'A1','tile_id':'t1','touches_border':False,'area_um2':100,'contrast_rgb':[.1,.1,.1],'stage_xy_um':[10,10]}
    overlap={**base,'id':'two','tile_id':'t2','stage_xy_um':[10.2,10]}
    neighbour={**base,'id':'three','stage_xy_um':[15,10]}
    other_chip={**overlap,'id':'four','chip_id':'B1'}
    assert len(deduplicate([base,overlap,neighbour,other_chip]))==3
