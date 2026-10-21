import numpy as np
import pytest
from flakefinder.model import Frame,Position
from flakefinder.layout import _context
from flakefinder.sparse_edges import validated_coherent_edge
from flakefinder.photometric_edges import PROFILE_MODEL,MEASURED_PROFILE_MODEL

def fixture(outside,model=MEASURED_PROFILE_MODEL):
    rgb=np.full((320,400,3),1200,np.uint16)
    rgb[:,200:]=outside
    labels=np.ones((320,400),np.uint8);labels[:,200:]=2
    meta=dict(objective={'magnification':20},objective_position=2,camera_resources={'exposure':400},
        camera_job_template='synthetic',white_level=4095,illumination_percent=90)
    frame=Frame(rgb,Position(0,0,100),(.1725,.1725),0,0,meta)
    profile=dict(schema=1,profile_id='synthetic',holder_model=model,manual_objective=True,
        objective_position=2,objective_magnification=20,optical_context=_context(frame))
    return frame,labels,np.diag([.1725,.1725]),profile

@pytest.mark.parametrize('outside,polarity',[(200,'outside_darker'),(2400,'outside_brighter')])
def test_coherent_broadband_step_preserves_substrate_side(outside,polarity):
    args=fixture(outside);edge,report=validated_coherent_edge(*args)
    assert edge is not None and report['accepted']
    assert report['step_polarity']==polarity
    assert edge['normal'][0]>.99
    assert report['inlier_profiles']>=30
    assert abs(edge['offset_um'])<1

def test_old_contract_does_not_silently_accept_brighter_side():
    edge,report=validated_coherent_edge(*fixture(2400,PROFILE_MODEL))
    assert edge is None and not report['accepted']

def test_chromatic_change_without_broadband_step_is_rejected():
    edge,report=validated_coherent_edge(*fixture([2400,2400,200]))
    assert edge is None and not report['accepted']

def test_no_rgb_step_cannot_be_created_by_label_boundary():
    edge,report=validated_coherent_edge(*fixture(1200))
    assert edge is None and not report['accepted']

def test_local_debris_cannot_validate_full_field_label_boundary():
    frame,labels,matrix,profile=fixture(1200)
    frame.rgb[145:175,200:240]=2400
    edge,report=validated_coherent_edge(frame,labels,matrix,profile)
    assert edge is None and not report['accepted']

def test_alternating_polarity_cannot_pool_support():
    frame,labels,matrix,profile=fixture(1200)
    for y in range(320):frame.rgb[y,200:]=2400 if (y//8)%2 else 200
    edge,report=validated_coherent_edge(frame,labels,matrix,profile)
    assert edge is None and not report['accepted']
