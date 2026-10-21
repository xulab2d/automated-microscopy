"""Focus-hull coverage, seed reuse, explicit recovery and local-AF fallback."""
from pathlib import Path
import json

import numpy as np
import pytest

import flakefinder.focus_surface as module
from flakefinder.autofocus import FocusResult
from flakefinder.model import AcquisitionError, Frame, Limits, Position, SafetyError, atomic_json


CENTRES=[[0,0],[100,0],[100,100],[0,100],[50,50]]
OPTIONS={'half_range_um':4,'coarse_step_um':2,'fine_step_um':.5}


class Stage:
    armed=True
    limits=Limits(-10,110,-10,110,0,30,150,3)
    def __init__(self):self.p=Position(50,50,10);self.travels=[];self.z_moves=[]
    def position(self):return self.p
    def check_cancel(self):pass
    def travel(self,x,y):
        self.p=Position(x,y,self.p.z);self.travels.append([x,y])
    def move_z(self,z):
        assert abs(z-self.p.z)<=self.limits.max_z_step_um+1e-8
        self.p=Position(self.p.x,self.p.y,z);self.z_moves.append(z)
    def capture(self):return Frame(np.full((40,50,3),2000,np.uint16),self.p,(.17,.17),0)


def mock_local_focus(monkeypatch,focus):
    """Surface tests isolate orchestration; quick AF has its own measured tests."""
    monkeypatch.setattr(module,'autofocus',focus)
    monkeypatch.setattr(module,'quick_autofocus',lambda scope,half,output,callback,**kw:
        focus(scope,kw['fallback_half_range_um'],kw['fallback_coarse_step_um'],kw['fallback_fine_step_um'],output,callback))


def install_success(monkeypatch,z_function=lambda x,y:10+.01*x+.02*y):
    calls=[]
    def focus(scope,half,coarse,fine,output,callback):
        calls.append([scope.p.x,scope.p.y])
        scope.p=Position(scope.p.x,scope.p.y,z_function(scope.p.x,scope.p.y))
        result=FocusResult(True,scope.p.z,1,.5,'Focused',[{'z_um':scope.p.z,'score':1}])
        atomic_json(Path(output)/'focus-curve.json',{'samples':result.samples})
        if callback:callback(scope.capture(),result.samples[0])
        return result
    mock_local_focus(monkeypatch,focus)
    return calls


def test_corner_anchors_reconstruct_tilt_and_forbid_extrapolation(tmp_path,monkeypatch):
    calls=install_success(monkeypatch);scope=Stage();events=[]
    surface,points=module.prepare_focus_surface(scope,CENTRES,OPTIONS,tmp_path,
                                               callback=lambda *args:events.append(args))
    assert set(map(tuple,calls))=={(0,0),(100,0),(100,100),(0,100)}
    assert len(points)==4 and surface.predict(30,60)==pytest.approx(11.5)
    with pytest.raises(AcquisitionError):surface.predict(101,50)
    assert len(list(tmp_path.glob('attempt-*/anchor_*/anchor.tiff')))==4
    assert all(event[0]=='focus_surface' and event[2]['search_phase']=='local' for event in events)
    saved=json.loads((tmp_path/'focus-surface.json').read_text())
    assert saved['mode']=='surface' and saved['measured_max_residual_um']<1e-10
    assert saved['extrapolation_allowed'] is False


def test_verified_seeds_skip_matching_corners_but_keep_interior_verification(tmp_path,monkeypatch):
    calls=install_success(monkeypatch)
    seeds=[[0,0,10],[100,0,11],[50,50,11.5]]
    surface,points=module.prepare_focus_surface(Stage(),CENTRES,OPTIONS,tmp_path,seed_points=seeds)
    assert set(map(tuple,calls))=={(0,100),(100,100)}
    assert len(points)==5 and surface.predict(50,70)==pytest.approx(11.9)
    saved=json.loads((tmp_path/'focus-surface.json').read_text())
    assert sum(a['status']=='reused_verified_seed' for a in saved['anchors'])==2


def test_inconsistent_corner_forces_local_af_without_dropping_outlier(tmp_path,monkeypatch):
    install_success(monkeypatch,lambda x,y:10+.01*x+.02*y+(6 if x==100 and y==100 else 0))
    surface,points=module.prepare_focus_surface(Stage(),CENTRES,OPTIONS,tmp_path)
    assert surface is None and len(points)==4
    assert [100,100,19] in points
    saved=json.loads((tmp_path/'focus-surface.json').read_text())
    assert saved['mode']=='per_tile_autofocus' and saved['measured_max_residual_um']>1
    assert len(saved['residuals_um'])==4


def test_collinear_scan_skips_surface_motion_and_falls_back_to_tile_af(tmp_path,monkeypatch):
    def forbidden(*args,**kwargs):raise AssertionError('No 2D prepass on a line')
    mock_local_focus(monkeypatch,forbidden);scope=Stage()
    surface,points=module.prepare_focus_surface(scope,[[0,10],[50,10],[100,10]],OPTIONS,tmp_path,
                                               seed_points=[[0,10,10.2]])
    assert surface is None and points==[[0,10,10.2]] and scope.travels==[]
    assert json.loads((tmp_path/'focus-surface.json').read_text())['mode']=='per_tile_autofocus'


@pytest.mark.parametrize('reason,explicit_bounds,expected_recoveries',[
    ('Peak at search boundary; extend range.',[2,20],4),
    ('Peak at search boundary; extend range.',None,0),
    ('Flat focus curve; no resolved focus peak.',[2,20],0)])
def test_recovery_requires_both_boundary_failure_and_explicit_absolute_bounds(tmp_path,monkeypatch,reason,explicit_bounds,expected_recoveries):
    paths=[];recoveries=[]
    def fail(scope,half,coarse,fine,output,callback):
        paths.append(Path(output));atomic_json(Path(output)/'focus-curve.json',{'samples':[{'failed':True}]})
        return FocusResult(False,scope.p.z,0,0,reason,[])
    def recover(scope,low,high,coarse,fine,output,callback):
        recoveries.append((low,high));paths.append(Path(output))
        scope.p=Position(scope.p.x,scope.p.y,10+.01*scope.p.x+.02*scope.p.y)
        atomic_json(Path(output)/'focus-curve.json',{'samples':[{'z_um':scope.p.z}]})
        return FocusResult(True,scope.p.z,1,.5,'Focused',[])
    mock_local_focus(monkeypatch,fail);monkeypatch.setattr(module,'autofocus_range',recover)
    options={**OPTIONS}
    if explicit_bounds is not None:options['search_bounds_um']=explicit_bounds
    surface,points=module.prepare_focus_surface(Stage(),CENTRES,options,tmp_path)
    assert len(recoveries)==expected_recoveries
    assert all(bounds==(2.,20.) for bounds in recoveries)
    assert len(paths)==len(set(paths)) and all((p/'focus-curve.json').exists() for p in paths)
    assert (surface is not None)==bool(expected_recoveries)


def test_invalid_recovery_bounds_are_rejected_before_any_motion(tmp_path,monkeypatch):
    scope=Stage()
    with pytest.raises(SafetyError,match='bounds'):
        module.prepare_focus_surface(scope,CENTRES,{**OPTIONS,'search_bounds_um':[-1,20]},tmp_path)
    assert scope.travels==[] and scope.z_moves==[]


def test_initial_flat_curve_uses_explicit_search_once_until_seed_exists(tmp_path,monkeypatch):
    recoveries=[]
    def fail(scope,*args):return FocusResult(False,scope.p.z,0,0,'Flat focus curve',[])
    def recover(scope,low,high,*args):
        recoveries.append((low,high));return FocusResult(True,scope.p.z,1,.5,'Focused',[])
    mock_local_focus(monkeypatch,fail);monkeypatch.setattr(module,'autofocus_range',recover)
    surface,points=module.prepare_focus_surface(Stage(),CENTRES,
        {**OPTIONS,'recover_initial_unresolved':True,'search_bounds_um':[2,20]},tmp_path)
    assert recoveries==[(2.,20.)] and len(points)==1 and surface is None


def test_focus_hull_accepts_encoder_rounding_only():
    from flakefinder.calibration import FocusMap
    surface=FocusMap([[25601.585,17568.206,10],[26398.415,17568.206,11],[26398.415,18431.794,12],[25601.585,18431.794,11]])
    assert surface.predict(25601.585387,17568.205989)==pytest.approx(10,abs=1e-5)
    with pytest.raises(AcquisitionError):surface.predict(25601.585,17568.19)


def test_provisional_tilt_plane_seeds_distant_anchors_but_measures_each(tmp_path,monkeypatch):
    starts=[]
    z=lambda x,y:7+.15*x+.03*y
    def focus(scope,half,coarse,fine,output,callback):
        starts.append([scope.p.x,scope.p.y,scope.p.z])
        assert scope.p.z==pytest.approx(z(scope.p.x,scope.p.y))
        return FocusResult(True,scope.p.z,1,.5,'Independently measured',[])
    mock_local_focus(monkeypatch,focus)
    seeds=[[x,y,z(x,y)] for x,y in [(20,20),(40,20),(20,40)]]
    surface,points=module.prepare_focus_surface(Stage(),CENTRES,OPTIONS,tmp_path,seed_points=seeds)
    assert len(starts)==4 and len(points)==7
    assert surface.predict(80,80)==pytest.approx(z(80,80))
    report=json.loads((tmp_path/'focus-surface.json').read_text())
    assert all(a['status']=='verified' for a in report['anchors'])
    assert all(a['search_seed']['method']=='provisional_measured_plane_search_seed' for a in report['anchors'])
    assert report['anchors'][0]['search_seed']['extrapolated_search_seed'] is True
    assert all(a['search_seed']['prediction_is_focus_evidence'] is False for a in report['anchors'])


def test_provisional_prediction_outside_limits_fails_before_anchor_travel(tmp_path,monkeypatch):
    scope=Stage();scope.p=Position(100,100,10)
    mock_local_focus(monkeypatch,lambda *args:pytest.fail('Unsafe seed must not trigger autofocus'))
    with pytest.raises(SafetyError,match='search seed'):
        module.prepare_focus_surface(scope,CENTRES,OPTIONS,tmp_path,
            seed_points=[[0,0,10],[10,0,11.5],[0,10,11.5]])
    assert scope.travels==[] and scope.z_moves==[]
    report=json.loads((tmp_path/'focus-surface.json').read_text())
    assert report['state']=='failed' and len(report['verified_points_um'])==3
    assert report['anchors'][0]['search_seed']['z_um']==pytest.approx(40)


def test_failed_local_focus_does_not_promote_plane_predictions(tmp_path,monkeypatch):
    mock_local_focus(monkeypatch,lambda scope,*args:FocusResult(False,scope.p.z,0,0,'Flat image',[]))
    seeds=[[20,20,10.6],[40,20,10.8],[20,40,11.0]]
    _,points=module.prepare_focus_surface(Stage(),CENTRES,OPTIONS,tmp_path,seed_points=seeds)
    assert sorted(points)==sorted(seeds)
    report=json.loads((tmp_path/'focus-surface.json').read_text())
    assert all(a['status']=='autofocus_failed' for a in report['anchors'])
    assert not list(tmp_path.glob('attempt-*/anchor_*/anchor.tiff'))


def test_provisional_plane_requires_every_residual_and_noncollinearity():
    with pytest.raises(AcquisitionError,match='noncollinear'):
        module.provisional_plane_seed([[0,0,10],[1,0,11],[2,0,12]],3,3)
    with pytest.raises(AcquisitionError,match='All verified'):
        module.provisional_plane_seed([[0,0,10],[100,0,10],[0,100,10],[100,100,20]],50,50)


def test_approximate_search_seeds_guide_only_first_three_new_roi_anchors(tmp_path,monkeypatch):
    starts=[];z=lambda x,y:7+.15*x+.03*y
    def focus(scope,*args):
        starts.append([scope.p.x,scope.p.y,scope.p.z])
        # Approximate full-frame XY causes a biased predicted height. Each
        # newly measured ROI returns its independently determined true height.
        scope.p=Position(scope.p.x,scope.p.y,z(scope.p.x,scope.p.y))
        return FocusResult(True,scope.p.z,1,.5,'ROI verified',[])
    mock_local_focus(monkeypatch,focus)
    approximate=[[x,y,z(x,y)+.5] for x,y in [(0,0),(100,0),(0,100)]]
    surface,points=module.prepare_focus_surface(Stage(),CENTRES,OPTIONS,tmp_path,search_seed_points=approximate)
    assert len(starts)==4 and len(points)==4
    for x,y,height in starts[:3]:assert height==pytest.approx(z(x,y)+.5)
    x,y,height=starts[3];assert height==pytest.approx(z(x,y))
    assert surface.predict(80,80)==pytest.approx(z(80,80))
    report=json.loads((tmp_path/'focus-surface.json').read_text())
    assert report['seed_points_um']==[] and len(report['search_seed_points_um'])==3
    assert all(a['status']=='verified' for a in report['anchors'])
    assert [a['search_seed']['source_role'] for a in report['anchors']]==['search_only_approximate_xy']*3+['verified_roi_focus']
    assert len(report['anchors'][3]['search_seed']['source_points_um'])==3
    assert not any(p in points for p in approximate)


def test_search_only_seeds_cannot_form_accepted_plane_when_autofocus_fails(tmp_path,monkeypatch):
    mock_local_focus(monkeypatch,lambda scope,*args:FocusResult(False,scope.p.z,0,0,'Unresolved',[]))
    surface,points=module.prepare_focus_surface(Stage(),CENTRES,OPTIONS,tmp_path,
        search_seed_points=[[0,0,10],[100,0,11],[0,100,12]])
    assert surface is None and points==[]
    report=json.loads((tmp_path/'focus-surface.json').read_text())
    assert report['verified_points_um']==[] and report['mode']=='per_tile_autofocus'
    assert all(a['status']=='autofocus_failed' for a in report['anchors'])


def test_quick_path_starts_only_after_plane_and_fresh_anchor_selection(tmp_path,monkeypatch):
    install_success(monkeypatch);base=module.autofocus;full_calls=[];quick_calls=[]
    scope=Stage();scope.selections=0
    def select():scope.selections+=1
    scope.begin_focus_anchor=select
    def full(*args):full_calls.append(scope.selections);return base(*args)
    def quick(adapter,half,output,callback,**fallback):
        quick_calls.append((scope.selections,half,fallback))
        return base(adapter,fallback['fallback_half_range_um'],fallback['fallback_coarse_step_um'],
                    fallback['fallback_fine_step_um'],output,callback)
    monkeypatch.setattr(module,'autofocus',full);monkeypatch.setattr(module,'quick_autofocus',quick)
    events=[]
    _,points=module.prepare_focus_surface(scope,CENTRES,OPTIONS,tmp_path,callback=lambda *args:events.append(args))
    assert len(points)==4 and full_calls==[1,2,3]
    assert quick_calls==[(4,2.,{'fallback_half_range_um':4.,'fallback_coarse_step_um':2.,'fallback_fine_step_um':.5})]
    assert len(events)==4 and all(e[0]=='focus_surface' for e in events)
    saved=json.loads((tmp_path/'focus-surface.json').read_text())
    assert 'local_search_strategy' not in saved['anchors'][0]
    assert saved['anchors'][3]['local_search_strategy']=='quick_plane_seeded_with_full_sweep_fallback'
