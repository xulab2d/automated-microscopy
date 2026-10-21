"""Whole-envelope first-reference search, exclusively simulated hardware."""
from dataclasses import asdict
import json
import math

import numpy as np
import pytest

import flakefinder.autonomous_reference as bootstrap
from flakefinder.model import AcquisitionError,Cancelled,Frame,Limits,Position
from test_autonomous_reference import setup


def test_plan_complete_reproducible_and_allowances_follow_route():
    options=bootstrap.BootstrapOptions(minimum_chip_um=5000,coverage_search=True,max_seconds=7200)
    p=bootstrap.coverage_search_plan(Position(-45000,-45000,100),[-50000,50000,-50000,50000],options)
    assert p==bootstrap.coverage_search_plan(Position(-45000,-45000,100),[-50000,50000,-50000,50000],options)
    assert sorted(p['bootstrap_lattice_order'])==list(range(p['probe_count']))
    route=p['bootstrap_route_um'];work=p['work_allowance']
    assert work['survey_fields']>64 and work['max_captures']>128 and work['max_xy_travel_um']>200000
    assert work['max_seconds']==7200
    assert work['max_captures']==len(route)+sum(work['per_leg_bracket_captures'])+64
    for a,b,count in zip(route,route[1:],work['per_leg_bracket_captures']):
        assert math.dist(a,b)/(2**count)<=10+1e-8
    assert all(-50000<=x<=50000 and -50000<=y<=50000 for x,y in route)


def test_uniform_holder_exhausts_more_than_64_fields_without_focus(monkeypatch,tmp_path):
    scope,args,calls=setup(monkeypatch,tmp_path,uniform=True)
    # Deliberately tiny historical caps must be replaced only in coverage mode.
    options=bootstrap.BootstrapOptions(minimum_chip_um=2000,coverage_search=True,
        max_seconds=7200,max_search_fields=1,max_captures=1,max_xy_travel_um=1)
    with pytest.raises(AcquisitionError,match='no focusable'):
        bootstrap.acquire_reference(**args,options=options)
    proof=json.loads((tmp_path/'autonomous-reference/proof.json').read_text())
    plan=json.loads((tmp_path/'autonomous-reference/search-plan.json').read_text())
    assert scope.sequence==len(plan['bootstrap_route_um'])>64 and calls==[]
    assert proof['survey_complete'] and proof['survey_fields_completed']==scope.sequence
    assert proof['budget']['captures_started']==scope.sequence
    assert proof['xy_travel_um']<=proof['options']['max_xy_travel_um']


def test_remote_square_chip_found_without_local_spiral_limit(monkeypatch,tmp_path):
    scope,args,calls=setup(monkeypatch,tmp_path)
    scope.p=Position(-45000,-45000,100);scope.limits=Limits(-50000,50000,-50000,50000,0,200)
    def capture():
        scope.sequence+=1
        xx=np.arange(256)-127.5+scope.p.x;yy=np.arange(256)-127.5+scope.p.y
        bright=((xx>=40000)&(xx<45000))[None,:]&((yy>=40000)&(yy<45000))[:,None]
        rgb=np.where(bright[:,:,None],[145,105,155],[18,18,18]).astype(np.uint8)
        return Frame(rgb,scope.p,(1.,1.),scope.sequence,
            metadata={'white_level':255,'camera_resources':{'ff_exposure_ms':scope.exposure}})
    scope.capture=capture
    args.update(position=scope.position(),bounds=[-50000,50000,-50000,50000])
    frame,_,_,proof=bootstrap.acquire_reference(**args,options=bootstrap.BootstrapOptions(coverage_search=True,max_seconds=7200))
    assert proof['state']=='accepted' and calls==['focus','validate']
    assert 40000<frame.position.x<45000 and 40000<frame.position.y<45000
    assert proof['xy_travel_um']>200000 # Previously refused although inside safe envelope.
    assert proof['reference_controls']['accepted']


def test_coverage_mode_stop_before_capture_never_moves(monkeypatch,tmp_path):
    scope,args,calls=setup(monkeypatch,tmp_path)
    def stop():raise Cancelled('Existing absolute deadline or operator STOP')
    args['check']=stop
    with pytest.raises(Cancelled):bootstrap.acquire_reference(**args,options=bootstrap.BootstrapOptions(coverage_search=True))
    assert scope.sequence==0 and scope.moves==[] and calls==[]


def test_pure_route_planning_remains_cooperatively_cancellable():
    calls=[]
    def stop():
        calls.append(True)
        if len(calls)==2:raise Cancelled('cancel during finite plan construction')
    with pytest.raises(Cancelled):bootstrap.coverage_search_plan(Position(0,0,100),[-50000,50000,-50000,50000],
        bootstrap.BootstrapOptions(coverage_search=True),check=stop)
    assert len(calls)==2
