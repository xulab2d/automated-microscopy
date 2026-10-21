"""Encoder-quantization regression for the real batch's Z-step failure."""
import math
import pytest
from flakefinder.model import Limits,Position,SafetyError,Cancelled,AcquisitionError
from flakefinder.motion import move_z_bounded


class QuantizedStage:
    armed=True
    limits=Limits(0,100000,0,60000,15400,15900,3000,10)
    def __init__(self,z=15603.984):
        self.p=Position(49601.585,18431.794,z);self.moves=[];self.cancelled=False
    def check_cancel(self):
        if self.cancelled:raise Cancelled('stop')
    def position(self):return self.p
    def move_z(self,z):
        target=Position(self.p.x,self.p.y,z)
        self.limits.check_move(self.p,target)
        self.moves.append((self.p.z,z))
        self.p=Position(self.p.x,self.p.y,math.ceil(z/.078125)*.078125)


@pytest.mark.parametrize('start,target',[(15603.984,15455),(15455.078125,15755),(15600,15590),(15600,15610)])
def test_quantized_readback_never_breaks_the_unchanged_step_limit(start,target):
    s=QuantizedStage(start);move_z_bounded(s,target)
    assert abs(s.p.z-target)<.079
    assert all(abs(b-a)<=9.75+1e-8 for a,b in s.moves)
    assert s.p.x==49601.585 and s.p.y==18431.794
    assert s.limits.max_z_step_um==10


def test_move_rejects_envelope_violation_before_any_command():
    s=QuantizedStage()
    with pytest.raises(SafetyError):move_z_bounded(s,15901)
    assert not s.moves


def test_cancellation_between_waypoints_does_not_return_or_continue():
    s=QuantizedStage();move=s.move_z
    def cancel(z):move(z);s.cancelled=True
    s.move_z=cancel
    with pytest.raises(Cancelled):move_z_bounded(s,15455)
    assert len(s.moves)==1


def test_stalled_encoder_does_not_loop_or_retry_forever():
    s=QuantizedStage();s.move_z=lambda z:s.moves.append((s.p.z,z))
    with pytest.raises(AcquisitionError,match='no progress'):move_z_bounded(s,15455)
    assert len(s.moves)==1


def test_hardware_exception_is_not_retried():
    s=QuantizedStage()
    def fail(z):s.moves.append((s.p.z,z));raise AcquisitionError('readback lost')
    s.move_z=fail
    with pytest.raises(AcquisitionError,match='readback lost'):move_z_bounded(s,15455)
    assert len(s.moves)==1
