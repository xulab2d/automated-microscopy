"""Actual blurred edge pixels exercise the dedicated edge-focus path."""
import cv2
import numpy as np
import pytest
from flakefinder.autofocus import autofocus_range,focus_mask_for_frame,focus_metrics_for_frame,focus_metrics,make_focus_mask
from flakefinder.focus_recovery import recover_focus
from flakefinder.model import Frame,Limits,Position,SafetyError


def edge_frame(z,axis=1,policy=True):
    plane=np.full((192,192),100,np.float32)
    if axis:plane[:,96:]=3000
    else:plane[96:,:]=3000
    sigma=float(np.sqrt(.7**2+(.8*(z-100))**2))
    image=cv2.GaussianBlur(plane,(0,0),sigma)
    return Frame(np.repeat(image[:,:,None],3,axis=2),Position(0,0,z),(.17,.17),0,
                 metadata={'white_level':4095,**({'focus_mask_policy':'include_chip_edge'} if policy else {})})


@pytest.mark.parametrize('axis',[0,1])
def test_edge_mask_includes_both_sides_and_metric_resolves_blur(axis):
    sharp=edge_frame(100,axis);blurred=edge_frame(110,axis)
    assert focus_mask_for_frame(sharp).all()
    a=focus_metrics_for_frame(sharp,focus_mask_for_frame(sharp))
    b=focus_metrics_for_frame(blurred,focus_mask_for_frame(blurred))
    assert a['score']>2*b['score'] and a['texture']>.015
    assert a['focus_metric']=='chip_edge_mean_gradient_energy'
    normal=edge_frame(100,axis,False)
    assert np.array_equal(focus_mask_for_frame(normal),make_focus_mask(normal.rgb))
    assert focus_metrics_for_frame(normal)==focus_metrics(normal.rgb)


class EdgeStage:
    armed=True
    limits=Limits(-1,1,-1,1,40,160,1,10)
    focus_target_tolerance_um=.235
    def __init__(self,z=106):self.p=Position(0,0,z);self.frames=0;self.change_policy=False;self.stopped=False
    def position(self):return self.p
    def check_cancel(self):pass
    def move_z(self,z):
        assert abs(z-self.p.z)<=10+1e-8
        self.limits.check(Position(0,0,z));self.p=Position(0,0,z)
    def capture(self):
        self.frames+=1
        return edge_frame(self.p.z,policy=not (self.change_policy and self.frames>1))
    def stop(self):self.stopped=True


def test_edge_autofocus_and_recovery_share_metric_and_find_focus(tmp_path):
    scope=EdgeStage()
    result=autofocus_range(scope,94,106,2,.5,tmp_path/'direct')
    assert result.success and abs(result.z_um-100)<.6
    assert all(s['focus_metric']=='chip_edge_mean_gradient_energy' for s in result.samples)
    scope=EdgeStage(112)
    result=recover_focus(scope,60,145,tmp_path/'recovery')
    assert result.success and abs(result.z_um-100)<.6
    assert all(s['focus_metric']=='chip_edge_mean_gradient_energy' for s in result.samples)


def test_policy_change_during_sweep_refuses_mixed_metrics(tmp_path):
    scope=EdgeStage();scope.change_policy=True
    with pytest.raises(SafetyError,match='policy changed'):
        autofocus_range(scope,94,106,2,.5,tmp_path)
    assert scope.stopped
