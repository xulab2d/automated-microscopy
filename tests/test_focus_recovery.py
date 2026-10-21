import json

import numpy as np
import pytest

import flakefinder.autofocus as autofocus_module
import flakefinder.focus_recovery as recovery
from flakefinder.model import AcquisitionError,Cancelled,Frame,Limits,Position,SafetyError


class Stage:
    armed=True
    limits=Limits(-10,10,-10,10,0,200,20,10)
    focus_target_tolerance_um=.235
    def __init__(self):
        self.p=Position(0,0,100);self.targets=[];self.frames=0;self.cancelled=False
        self.stop_after=None;self.fail_after=None;self.stopped=False;self.offset=0.
    def position(self):return self.p
    def check_cancel(self):
        if self.cancelled:raise Cancelled('Synthetic STOP')
    def move_z(self,z):
        self.check_cancel()
        assert abs(z-self.p.z)<=10+1e-7
        assert self.limits.z_min<=z<=self.limits.z_max
        self.targets.append(z);self.p=Position(0,0,z+self.offset);self.limits.check(self.p)
    def capture(self):
        self.check_cancel();self.frames+=1
        if self.fail_after==self.frames:raise AcquisitionError('Synthetic camera failure')
        frame=Frame(np.full((40,50,3),self.p.z,float),self.p,(.17,.17),self.frames)
        if self.stop_after==self.frames:self.cancelled=True
        return frame
    def stop(self):self.stopped=True;self.armed=False


def curve(monkeypatch,centre=None,sigma=12.):
    def metrics(rgb,mask=None):
        z=float(rgb[0,0,0]);score=.00017 if centre is None else .00017+.0015*np.exp(-.5*((z-centre)/sigma)**2)
        return {'score':float(score),'texture':.1,'noise':.001,'mean':1000.}
    monkeypatch.setattr(recovery,'focus_metrics',metrics)
    monkeypatch.setattr(autofocus_module,'focus_metrics',metrics)


@pytest.mark.parametrize('centre',[60.,140.])
def test_fresh_directional_recovery_brackets_and_verifies_small_window(tmp_path,monkeypatch,centre):
    curve(monkeypatch,centre);scope=Stage()
    result=recovery.recover_focus(scope,0,200,tmp_path)
    assert result.success and abs(result.z_um-centre)<.8
    report=json.loads((tmp_path/'recovery.json').read_text())
    assert report['state']=='focused' and report['direction']==(1 if centre>100 else -1)
    assert report['bracket_um'][1]-report['bracket_um'][0]<=6
    assert scope.frames<=40 and report['exploratory_images']<=24
    assert (tmp_path/'verified_focus.tiff').exists()
    assert all(.236-1e-8<=z<=199.764+1e-8 for z in scope.targets)


def test_flat_sensor_background_fails_with_bounded_fallback_and_rounding_headroom(tmp_path,monkeypatch):
    curve(monkeypatch);scope=Stage();scope.offset=.078
    def forbidden(*args,**kwargs):raise AssertionError('Flat data cannot trigger final autofocus.')
    monkeypatch.setattr(recovery,'autofocus_range',forbidden)
    result=recovery.recover_focus(scope,0,200,tmp_path)
    assert not result.success and scope.frames<=24
    report=json.loads((tmp_path/'recovery.json').read_text())
    assert report['state']=='unresolved' and report['fallback_used']
    assert report['requested_search_bounds_um']==[0.,200.]
    assert report['search_bounds_um']==pytest.approx([.236,199.764])
    assert min(scope.targets)>=.236-1e-8 and max(scope.targets)<=199.764+1e-8


def test_stop_never_returns_and_restores_callback(tmp_path,monkeypatch):
    curve(monkeypatch,140);scope=Stage();original=scope.check_cancel;scope.stop_after=3
    with pytest.raises(Cancelled):recovery.recover_focus(scope,0,200,tmp_path)
    assert scope.frames==3 and scope.position().z==scope.targets[-1]
    assert scope.check_cancel==original
    assert json.loads((tmp_path/'recovery.json').read_text())['state']=='stopped'


def test_camera_failure_records_atomic_failure_without_return(tmp_path,monkeypatch):
    curve(monkeypatch,140);scope=Stage();scope.fail_after=3
    with pytest.raises(AcquisitionError,match='camera failure'):
        recovery.recover_focus(scope,0,200,tmp_path)
    assert scope.stopped and scope.position().z==scope.targets[-1]
    assert json.loads((tmp_path/'recovery.json').read_text())['state']=='failed'


def test_invalid_range_refuses_before_motion_or_capture(tmp_path):
    scope=Stage()
    with pytest.raises(SafetyError):recovery.recover_focus(scope,-1,200,tmp_path)
    assert not scope.targets and scope.frames==0
