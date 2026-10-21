"""Recovery uses the existing owner and never treats a missed move as success."""
from types import SimpleNamespace as N
import time

import pytest

from test_integration import bare_zen
from flakefinder.model import Position, Limits, AcquisitionError, Cancelled, SafetyError
from flakefinder.zen import PositionToleranceError


def scope_for_failure(*,axis='y',failures=1,displacement=0.,error_type=PositionToleranceError):
    scope=bare_zen()
    scope.limits=Limits(-27660.48,28339.52,-36344.36,19655.64,15484,15759.218,1000,10)
    start=Position(1783.216,-15856.125,15664.453)
    state=[start];commands=[];events=[];stops=[]
    scope.position=lambda:state[0]
    scope._record_motion=lambda event,p,target,error=None,details=None:events.append((event,details))
    scope._rpc=lambda *a,**k:stops.append(a)
    target=Position(start.x+(-4.75 if axis=='x' else 0),start.y+(-4.75 if axis=='y' else 0),start.z+(-1 if axis=='z' else 0))
    def rpc(stub,request):
        commands.append(request)
        if len(commands)<=failures:
            v=dict(x=start.x,y=start.y,z=start.z);v[axis]+=displacement
            state[0]=Position(**v)
            raise error_type('ZEISS MoveTo: Position could not be reached.')
        state[0]=target
        return N(position_changed=True)
    scope._motion_rpc=rpc
    return scope,start,target,state,commands,events,stops


@pytest.mark.parametrize('axis',['x','y'])
def test_exact_stationary_failure_repeats_identical_absolute_command_once(axis):
    s,start,target,state,commands,events,stops=scope_for_failure(axis=axis)
    assert s.move(target)==target
    assert len(commands)==2 and commands[0] is commands[1]
    assert s.armed and not s.cancel.is_set() and not stops
    audit=next(d for e,d in events if e=='stationary_xy_retry')
    assert audit['axis']==axis and len(audit['stationary_readbacks'])==5
    assert all(p['z']==start.z for p in audit['stationary_readbacks'])
    assert events[-1][0]=='settled'


def test_repeat_rejection_stops_after_two_commands_without_accepting_wrong_position():
    s,start,target,state,commands,events,stops=scope_for_failure(failures=2)
    with pytest.raises(PositionToleranceError):s.move(target)
    assert len(commands)==2 and state[0]==start
    assert not s.armed and s.cancel.is_set() and len(stops)==2
    assert events[-1][0]=='failed'


@pytest.mark.parametrize('axis,displacement,error_type',[
    ('y',-.2,PositionToleranceError),('z',0.,PositionToleranceError),
    ('y',0.,AcquisitionError)])
def test_partial_motion_z_errors_and_generic_transport_errors_are_not_reissued(axis,displacement,error_type):
    s,_,target,_,commands,_,_=scope_for_failure(axis=axis,displacement=displacement,error_type=error_type)
    with pytest.raises(error_type):s.move(target)
    assert len(commands)==1 and not s.armed


def test_slow_failed_rpc_is_not_retried(monkeypatch):
    import flakefinder.zen as zen
    s,_,target,_,commands,_,_=scope_for_failure()
    times=iter([10.,11.1]);monkeypatch.setattr(zen.time,'monotonic',lambda:next(times))
    with pytest.raises(PositionToleranceError):s.move(target)
    assert len(commands)==1


def test_cancel_during_stationary_check_prevents_retry():
    s,start,target,state,commands,events,stops=scope_for_failure()
    def read():
        if commands:s.cancel.set()
        return state[0]
    s.position=read
    with pytest.raises(Cancelled):s.move(target)
    assert len(commands)==1


@pytest.mark.parametrize('mode',['drift','other_axis','outside'])
def test_changed_readback_blocks_recovery(mode):
    s,start,target,state,commands,events,stops=scope_for_failure()
    reads=[0]
    def read():
        if commands:
            reads[0]+=1
            if reads[0]>=2:
                if mode=='drift':return Position(start.x,start.y+.06,start.z)
                if mode=='other_axis':return Position(start.x,start.y,start.z+.06)
                return Position(start.x,start.y,s.limits.z_max+1)
        return state[0]
    s.position=read
    with pytest.raises((PositionToleranceError,SafetyError)):s.move(target)
    assert len(commands)==1


def test_recovery_burst_limit_does_not_loop_across_moves():
    s,_,target,_,commands,_,_=scope_for_failure()
    s._xy_retry_times=[time.monotonic()]*3
    with pytest.raises(PositionToleranceError):s.move(target)
    assert len(commands)==1


def test_old_recovery_entries_expire():
    s,_,target,_,commands,_,_=scope_for_failure()
    s._xy_retry_times=[time.monotonic()-61]*3
    assert s.move(target)==target and len(commands)==2
    assert len(s._xy_retry_times)==1


def test_changed_objective_prevents_retry():
    s,_,target,_,commands,_,_=scope_for_failure()
    s.expected_objective_position=2
    def verify(_):
        if commands:raise SafetyError('objective changed')
    s.hardware_controls=lambda:N(verify_optics=verify)
    with pytest.raises(SafetyError,match='objective changed'):s.move(target)
    assert len(commands)==1


def test_success_after_rejection_still_requires_final_position_verification():
    s,_,target,_,commands,_,_=scope_for_failure()
    rpc=s._motion_rpc
    def changed(stub,request):
        if commands:
            commands.append(request)
            return N(position_changed=True)
        return rpc(stub,request)
    s._motion_rpc=changed;s.timeout_s=.16
    with pytest.raises(AcquisitionError,match='did not settle'):s.move(target)
    assert len(commands)==2 and not s.armed
