"""Measured rectangle support replaces only the unrelated hull-distance cap."""
from copy import deepcopy
from pathlib import Path
import os,json
import numpy as np
import pytest
from flakefinder.full_chip import FullChipOptions,bounded_plane_prediction
from flakefinder.model import AcquisitionError,SafetyError,Limits,atomic_json


def rectangle_plan():
    polygon=[[-5100,-5100],[5100,-5100],[5100,5100],[-5100,5100]]
    return {'scan_plan':{'polygon_um':polygon},'camera_shape':[300,400,3],
        'transform':{'matrix':[[1,0],[0,1]]},'fingerprint':'test-plan','discovery_sha256':'test-discovery',
        'boundary_estimate':{'status':'estimated','method':'measured_sides_shared_rectangle_orientation',
            'polygon_um':polygon,'lines':[{'source_images':[str(i)],'edge_point_count':20} for i in range(4)]}}


def focus_points():
    return [[x,y,10+.001*x+.002*y] for x,y in [(-5000,0),(5000,0),(0,-5000),(0,5000)]]


def test_rectangle_corners_use_four_measured_plane_points_with_uncertainty_limit():
    options=FullChipOptions();points=focus_points()
    with pytest.raises(AcquisitionError,match='distance'):bounded_plane_prediction(points,5000,5000,options)
    accepted=bounded_plane_prediction(points,5000,5000,options,plan=rectangle_plan())
    assert accepted['predicted_z_um']==pytest.approx(25.)
    assert accepted['extrapolation_distance_um']>3000 and accepted['extrapolation_limit_um'] is None
    assert accepted['conditional_prediction_sigma_um']<options.focus_max_prediction_sigma_um
    assert accepted['focus_support_domain']['kind']=='same_measured_rectangle_with_camera_footprint'
    assert not accepted['focus_verified'] and not accepted['local_focus_verified']
    # A tile centre just outside the outline is allowed only for its footprint.
    assert bounded_plane_prediction(points,5299,0,options,plan=rectangle_plan())['plane_prediction_accepted']
    with pytest.raises(AcquisitionError,match='outside this measured rectangle'):
        bounded_plane_prediction(points,5301,0,options,plan=rectangle_plan())


def test_rectangle_domain_retains_all_scientific_and_controller_checks():
    options=FullChipOptions();plan=rectangle_plan();points=focus_points()
    bad=deepcopy(points);bad[0][2]+=10
    with pytest.raises(AcquisitionError,match='residual'):bounded_plane_prediction(bad,5000,5000,options,plan=plan)
    with pytest.raises(AcquisitionError,match='uncertainty'):
        bounded_plane_prediction(points,5000,5000,FullChipOptions(focus_max_prediction_sigma_um=.1),plan=plan)
    with pytest.raises(SafetyError):
        bounded_plane_prediction(points,5000,5000,options,plan=plan,limits=Limits(-6000,6000,-6000,6000,0,20))
    badplan=deepcopy(plan);badplan['boundary_estimate']['lines'].pop()
    with pytest.raises(AcquisitionError,match='four measured sides'):
        bounded_plane_prediction(points,5000,5000,options,plan=badplan)
    other=deepcopy(plan);other['boundary_estimate']['method']='legacy_convex_outline'
    with pytest.raises(AcquisitionError,match='distance'):bounded_plane_prediction(points,5000,5000,options,plan=other)


def test_actual_all_planned_tiles_pass_unchanged_sigma_and_residual_limits(tmp_path):
    run=os.environ.get('FLAKEFINDER_FOCUS_REPLAY')
    if not run:pytest.skip('No saved lab replay requested')
    folder=Path(run)/'chip001';plan=json.loads((folder/'coverage-plan.json').read_text());manifest=json.loads((folder/'manifest.json').read_text())
    options=FullChipOptions(**plan['options']);points=manifest['focus_points_um'];limits=Limits(**manifest['limits'])
    details=[bounded_plane_prediction(points,x,y,options,plan=plan,limits=limits) for x,y in plan['tile_centres_um']]
    assert len(details)==999
    assert max(d['conditional_prediction_sigma_um'] for d in details)==pytest.approx(.6817301992)
    assert max(d['max_anchor_residual_um'] for d in details)==pytest.approx(.1051755820)
    assert any(d['extrapolation_distance_um']>4000 for d in details)
    assert all(not d['focus_verified'] and d['plane_prediction_accepted'] for d in details)
    polygon=np.asarray(plan['scan_plan']['polygon_um'])
    with pytest.raises(AcquisitionError,match='outside this measured rectangle'):
        bounded_plane_prediction(points,float(polygon[:,0].max()+1000),float(polygon[:,1].mean()),options,plan=plan)
    atomic_json(tmp_path/'actual-rectangle-plane-replay.json',{'tile_count':len(details),
        'maximum_conditional_sigma_um':max(d['conditional_prediction_sigma_um'] for d in details),
        'maximum_anchor_residual_um':max(d['max_anchor_residual_um'] for d in details),
        'maximum_hull_extrapolation_um':max(d['extrapolation_distance_um'] for d in details),
        'sigma_limit_um':options.focus_max_prediction_sigma_um,'rejections':0,'hardware_access':False})
