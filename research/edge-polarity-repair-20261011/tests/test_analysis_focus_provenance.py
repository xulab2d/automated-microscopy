import json
import numpy as np

from flakefinder.analysis import focus_provenance
from flakefinder.model import Frame,Position


def setup_surface(tmp_path,xy=(5,5),prediction=12.5,z=12.5):
    folder=tmp_path/'focus-surface';folder.mkdir()
    points=[[0,0,10],[10,0,11],[0,10,14],[10,10,15]]
    (folder/'focus-surface.json').write_text(json.dumps({'state':'complete','mode':'surface',
        'verified_points_um':points,'max_residual_um':1}))
    item={'path':tmp_path/'tiles/tile.tiff','tile_id':'tile','manifest':{'completed':{'tile':{
        'focus':None,'focus_method':'measured_surface','focus_prediction_um':prediction}}}}
    frame=Frame(np.zeros((20,20,3),np.uint16),Position(*xy,z),(.5,.5),0)
    return item,frame,folder


def test_verified_surface_is_not_mislabeled_as_failed_local_autofocus(tmp_path):
    item,frame,_=setup_surface(tmp_path)
    status,limited=focus_provenance(item,frame)
    assert not limited and status['status']=='verified_surface_interpolation'
    assert status['local_autofocus'] is False and not status['thin_flake_focus_validated']


def test_unmatched_surface_and_extrapolation_remain_focus_limited(tmp_path):
    item,frame,_=setup_surface(tmp_path,xy=(15,5),prediction=13.5,z=13.5)
    assert focus_provenance(item,frame)[1]
    frame.position=Position(5,5,12.5)
    assert focus_provenance(item,frame)[1]


def test_capture_height_disagreement_remains_focus_limited(tmp_path):
    item,frame,_=setup_surface(tmp_path,z=14)
    status,limited=focus_provenance(item,frame)
    assert limited and 'Captured Z' in status['reason']


def test_bounded_plane_focus_replays_per_tile_anchors_and_controller_tolerance(tmp_path):
    from dataclasses import asdict
    from flakefinder.full_chip import FullChipOptions,bounded_plane_prediction
    points=[[0,0,10],[100,0,11],[0,100,14],[100,100,15]]
    options=FullChipOptions()
    detail=bounded_plane_prediction(points,110,50,options)
    detail['capture_z_target_tolerance_um']=.235
    item={'path':tmp_path/'tiles/tile.tiff','tile_id':'tile','manifest':{
        'full_chip_options':asdict(options),'completed':{'tile':{
            'focus':None,'focus_method':detail['method'],'focus_prediction_um':detail['predicted_z_um'],
            'full_chip_focus':detail}}}}
    frame=Frame(np.zeros((20,20,3),np.uint16),Position(110,50,detail['predicted_z_um']+.22),(.5,.5),0)
    status,limited=focus_provenance(item,frame)
    assert not limited and status['extrapolated'] and status['plane_prediction_accepted']
    assert not status['local_focus_verified'] and not status['thin_flake_focus_validated']
    frame.position=Position(110,50,detail['predicted_z_um']+.3)
    assert focus_provenance(item,frame)[1]
