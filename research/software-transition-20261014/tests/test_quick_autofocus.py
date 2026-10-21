"""Measured edge-curve replay and bounded quick-path control contracts."""
import json
from pathlib import Path
import numpy as np
import pytest
import flakefinder.autofocus as module
from flakefinder.model import Frame,Position,Limits,Cancelled


CURVES=json.loads((Path(__file__).parent/'fixtures/measured_edge_focus_curves.json').read_text())['curves']


def test_five_actual_samples_replay_all_four_verified_edges():
    assert len(CURVES)==4
    for curve in CURVES:
        result=module.fit_quick_focus_curve(curve['five_measured_samples'])
        assert result.success
        assert abs(result.z_um-curve['dense_peak_z_um'])<.3


class Stage:
    armed=True
    limits=Limits(-10,10,-10,10,15000,16000,20,10)
    focus_target_tolerance_um=.235
    def __init__(self,z):self.p=Position(0,0,z);self.frames=0;self.targets=[];self.cancel_after=None;self.stopped=False
    def position(self):return self.p
    def check_cancel(self):
        if self.cancel_after is not None and self.frames>=self.cancel_after:raise Cancelled('Synthetic STOP')
    def move_z(self,z):
        self.check_cancel();self.limits.check(Position(0,0,z));assert abs(z-self.p.z)<=10
        self.targets.append(z);self.p=Position(0,0,z)
    def capture(self):
        self.check_cancel();self.frames+=1
        return Frame(np.full((20,20,3),self.p.z),self.p,(.17,.17),self.frames)
    def stop(self):self.stopped=True


def measured_scope(monkeypatch):
    samples=CURVES[0]['five_measured_samples'];z=np.array([s['z_um'] for s in samples]);scores=[s['score'] for s in samples]
    scope=Stage(float((z[0]+z[-1])/2))
    def metrics(frame,mask=None):
        return {'score':float(np.interp(frame.position.z,z,scores)),'texture':1.2,'noise':.008,'mean':1176.}
    monkeypatch.setattr(module,'focus_metrics_for_frame',metrics)
    return scope,float((z[-1]-z[0])/2),metrics


def test_quick_focus_has_five_samples_plus_independent_confirmation(tmp_path,monkeypatch):
    scope,half,_=measured_scope(monkeypatch)
    result=module.quick_autofocus(scope,half,tmp_path,fallback_half_range_um=None)
    assert result.success and scope.frames==6
    assert [s['phase'] for s in result.samples]==['quick']*5+['confirmation']
    assert abs(result.z_um-CURVES[0]['dense_peak_z_um'])<.4


def test_unresolved_quick_curve_calls_established_full_sweep(tmp_path,monkeypatch):
    scope=Stage(15376);calls=[]
    monkeypatch.setattr(module,'focus_metrics_for_frame',lambda *args:{'score':.001,'texture':.2,'noise':.008,'mean':1000.})
    def fallback(adapter,low,high,*args):
        calls.append((low,high));return module.FocusResult(False,adapter.position().z,.001,0,'Full sweep unresolved',[])
    monkeypatch.setattr(module,'autofocus_range',fallback)
    result=module.quick_autofocus(scope,2,tmp_path)
    assert not result.success and scope.frames==5 and calls==[(15364.,15388.)]
    assert json.loads((tmp_path/'quick-focus.json').read_text())['quick_failure']


def test_failed_confirmation_is_not_focus_success(tmp_path,monkeypatch):
    scope,half,metrics=measured_scope(monkeypatch)
    def degraded(frame,mask=None):
        result=metrics(frame,mask)
        if frame.sequence==6:result['score']*=.8
        return result
    monkeypatch.setattr(module,'focus_metrics_for_frame',degraded)
    result=module.quick_autofocus(scope,half,tmp_path,fallback_half_range_um=None)
    assert scope.frames==6 and not result.success and 'confirmation' in result.reason


def test_stop_interrupts_quick_search_without_return_or_fallback(tmp_path,monkeypatch):
    scope,half,_=measured_scope(monkeypatch);scope.cancel_after=3
    monkeypatch.setattr(module,'autofocus_range',lambda *args:pytest.fail('STOP cannot initiate fallback'))
    with pytest.raises(Cancelled):module.quick_autofocus(scope,half,tmp_path)
    assert scope.frames==3 and scope.position().z==scope.targets[-1]
    assert json.loads((tmp_path/'quick-focus.json').read_text())['state']=='stopped'
