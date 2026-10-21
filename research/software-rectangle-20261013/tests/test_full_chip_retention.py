import numpy as np
import pytest

import flakefinder.full_chip as full
from flakefinder.imaging import load_frame
from flakefinder.model import FocusSupportError
from test_full_chip import planned,fast_focus,resign


def test_vendor_acknowledgement_receives_persisted_full_raw_frame(planned,fast_focus):
    scope,reference,plan,folder=planned
    plan['tile_centres_um']=plan['tile_centres_um'][:2]
    plan['tile_count']=2
    resign(plan)
    acknowledged=[]
    def acknowledge(frame,path):
        saved=load_frame(path)
        assert saved.rgb.shape==reference.rgb.shape
        assert np.array_equal(saved.rgb,frame.rgb)
        acknowledged.append(path)
        return {'status':'retained'}
    scope.ack_persisted_frame=acknowledge
    result=full.run_full_chip(scope,plan,reference,folder)
    assert len(acknowledged)==2
    assert all(r['vendor_result_retention']=={'status':'retained'} for r in result['completed'].values())


def test_missing_edge_support_stops_before_raster_instead_of_unbounded_autofocus(planned,monkeypatch):
    scope,reference,plan,folder=planned
    plan['focus_anchor_mode']='chip_edges'
    resign(plan)
    points=[[*xy,scope.position().z] for xy in plan['anchor_xy_um'][:3]]
    monkeypatch.setattr(full,'prepare_focus_surface',lambda *a,**kw:(None,points))
    with pytest.raises(FocusSupportError,match='unresolved'):
        full.run_full_chip(scope,plan,reference,folder)
    assert not list((folder/'tiles').glob('*.tiff'))
