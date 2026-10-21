"""Explicit focus evidence must be verified before it becomes a search seed."""
from dataclasses import asdict
import json

import numpy as np
import pytest

from flakefinder.full_chip import load_seed_focus_points,_camera_context
from flakefinder.imaging import save_frame
from flakefinder.model import Frame,Position,CameraTransform,Limits,SafetyError,atomic_json


@pytest.fixture
def evidence(tmp_path):
    transform=CameraTransform([[1,0],[0,1]],(1,1),True)
    metadata={'camera_job_template':'Camera','camera_resources':{'exposure':200},'objective_position':2,'illumination_percent':90}
    pairs=[]
    for index,(x,y,z) in enumerate(((20,20,10),(80,20,12),(20,80,16))):
        frame=Frame(np.ones((16,16,3),np.uint16),Position(x,y,z),(1,1),index,metadata=metadata)
        save_frame(tmp_path,str(index),frame)
        result=tmp_path/(str(index)+'-result.json')
        atomic_json(result,{'success':True,'z_um':z,'samples':[{'z_um':z,'score':1}]})
        pairs.append({'result':str(result),'frame':str(tmp_path/(str(index)+'.tiff'))})
    plan={'transform':asdict(transform),'camera_shape':[16,16,3],'acquisition_settings':_camera_context(frame),
          'scan_plan':{'polygon_um':[[0,0],[100,0],[100,100],[0,100]]}}
    return pairs,plan,Limits(0,100,0,100,0,50)


def test_three_verified_points_and_all_source_hashes_are_retained(evidence):
    pairs,plan,limits=evidence
    points,provenance=load_seed_focus_points(pairs,plan,limits=limits)
    assert points==[[20,20,10],[80,20,12],[20,80,16]]
    for record in provenance['records']:
        assert all(len(record[key])==64 for key in ('result_sha256','frame_sha256','metadata_sha256'))
        assert record['result_success'] and record['result_xy_available'] is False
    assert load_seed_focus_points(provenance['records'],plan,limits=limits)[0]==points


@pytest.mark.parametrize('change,match',[
    ('failed','successful'),('height','Z differs'),('xy','XY differs'),('profile','optical profile'),
    ('outside','inside the estimated'),('hash','evidence changed')])
def test_inconsistent_or_changed_evidence_rejected(evidence,change,match):
    pairs,plan,limits=evidence
    pair=pairs[0]
    if change in ('failed','height','xy'):
        result={'success':change!='failed','z_um':11 if change=='height' else 10}
        if change=='xy':result['position_um']={'x':70,'y':20,'z':10}
        atomic_json(pair['result'],result)
    elif change=='profile':plan['acquisition_settings']['objective_position']=3
    elif change=='outside':plan['scan_plan']['polygon_um']=[[30,30],[100,30],[100,100],[30,100]]
    elif change=='hash':pair['frame_sha256']='0'*64
    with pytest.raises(SafetyError,match=match):load_seed_focus_points([pair],plan,limits=limits)


def test_recovery_report_checks_physical_xy(evidence):
    pairs,plan,limits=evidence
    atomic_json(pairs[0]['result'],{'initial_position_um':{'x':20,'y':20,'z':5},
                                  'result':{'success':True,'z_um':10}})
    _,provenance=load_seed_focus_points([pairs[0]],plan,limits=limits)
    assert provenance['records'][0]['result_xy_available']


def test_search_only_bundle_never_supplies_verified_plane_points(evidence):
    pairs,plan,limits=evidence
    for pair in pairs:pair['purpose']='search_seed'
    points,provenance=load_seed_focus_points(pairs,plan,limits=limits)
    assert points==[] and provenance['verified_points_um']==[]
    assert provenance['search_seed_points_um']==[[20,20,10],[80,20,12],[20,80,16]]
    assert all(r['purpose']=='search_seed' and not r['local_focus_verified'] for r in provenance['records'])
    assert load_seed_focus_points(provenance['records'],plan,limits=limits)[0]==[]


def test_mixed_bundle_keeps_purposes_separate(evidence):
    pairs,plan,limits=evidence;pairs[0]['purpose']='search_seed'
    points,provenance=load_seed_focus_points(pairs,plan,limits=limits)
    assert points==[[80,20,12],[20,80,16]]
    assert provenance['search_seed_points_um']==[[20,20,10]]
    pairs[0]['purpose']='almost_verified'
    with pytest.raises(ValueError,match='purpose'):load_seed_focus_points(pairs,plan,limits=limits)
