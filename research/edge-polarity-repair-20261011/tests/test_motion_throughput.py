"""Reduce needless stage work without losing approach direction or verification."""
from types import SimpleNamespace as N
import pytest
from test_integration import bare_zen
from flakefinder.model import Position,Cancelled,AcquisitionError

def travel_scope():
    scope=bare_zen();scope.xy_backlash_um=30;scope.limits.max_xy_step_um=1000
    current=[Position(10000,20000,3000)];moves=[]
    scope.position=lambda:current[0]
    def move(target):
        scope.limits.check_move(current[0],target)
        moves.append((current[0],target));current[0]=target
        return target
    scope.move=move
    return scope,moves

@pytest.mark.parametrize('target',[(10300,20000),(9700,20000),(10000,20300),(10000,19700),(10010,20000),(9970,20020)])
def test_backlash_approach_preserves_unchanged_axes_and_positive_final_direction(target):
    scope,moves=travel_scope();start=scope.position()
    scope.travel(*target)
    assert scope.position()==Position(*target,3000)
    for axis,value in zip(('x','y'),target):
        steps=[getattr(b,axis)-getattr(a,axis) for a,b in moves if getattr(a,axis)!=getattr(b,axis)]
        if value==getattr(start,axis):assert steps==[]
        else:assert steps[-1]>0
    assert all(a.z==b.z==3000 for a,b in moves)

def test_positive_raster_stride_is_one_axis_move_and_reverse_requires_only_two():
    scope,moves=travel_scope();scope.travel(10090,20000)
    assert len(moves)==1
    moves.clear();scope.travel(10000,20000)
    assert len(moves)==2

def test_noop_travel_still_obeys_cancellation():
    scope,moves=travel_scope();scope.cancel.set()
    with pytest.raises(Cancelled):scope.travel(10000,20000)
    assert not moves

def test_intermediate_axes_are_verified_and_final_optical_dwell_is_retained():
    scope=bare_zen();state=[Position(10000,20000,3000)];events=[]
    scope.position=lambda:state[0]
    def rpc(stub,request):
        axis='x' if hasattr(request,'x') else 'y'
        values=dict(x=state[0].x,y=state[0].y,z=state[0].z)
        values[axis]=getattr(request,axis).value*1e6
        state[0]=Position(**values);events.append(('move',axis))
        return N(position_changed=True)
    scope._motion_rpc=rpc
    def settled(target,optical_dwell=True):
        assert scope._target_reached(state[0],target)
        events.append(('verify',optical_dwell));return state[0]
    scope._settled_position=settled
    scope.move(Position(10010,20020,3000))
    assert events==[('move','x'),('verify',False),('move','y'),('verify',False),('verify',True)]

def test_failed_intermediate_settling_prevents_next_axis():
    scope=bare_zen();scope.position=lambda:Position(10000,20000,3000);commands=[]
    scope._motion_rpc=lambda *args:commands.append(args)
    scope._rpc=lambda *args,**kwargs:None
    def failed(*args,**kwargs):raise AcquisitionError('axis still moving')
    scope._settled_position=failed
    with pytest.raises(AcquisitionError):scope.move(Position(10010,20020,3000))
    assert len(commands)==1 and not scope.armed
