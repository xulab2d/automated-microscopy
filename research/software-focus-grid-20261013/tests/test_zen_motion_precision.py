"""Hardware-free regressions for vendor precision and tiny Z no-op moves."""
from pathlib import Path
from types import SimpleNamespace as N
import math
import pytest

from test_integration import bare_zen
from flakefinder.model import Position,Limits,SafetyError,AcquisitionError
from flakefinder.mtb import MtbControls
from flakefinder.motion import move_z_bounded


def precision_scope():
    scope=bare_zen();scope.focus_target_tolerance_um=.235;scope.timeout_s=.18
    scope._rpc=lambda *args,**kwargs:None
    return scope


def test_exact_failed_plane_correction_does_not_force_subprecision_motion():
    scope=precision_scope()
    scope.limits=Limits(23500,76500,3500,56500,15400,15900,3000,10)
    actual=Position(37867.195,5784.103,15628.125);scope.position=lambda:actual
    commands=[]
    scope._motion_rpc=lambda *args:commands.append(args)
    result=scope.move_z(15627.96850003893)
    assert result==actual and commands==[] and scope.armed
    assert scope._target_reached(actual,Position(actual.x,actual.y,15627.96850003893))


def test_vendor_false_response_outside_precision_is_not_accepted_or_retried():
    scope=precision_scope();scope.position=lambda:Position(10000,20000,3000)
    commands=[]
    def unchanged(*args):commands.append(args);return N(position_changed=False)
    scope._motion_rpc=unchanged
    with pytest.raises(AcquisitionError,match='Z tolerance 0.235'):scope.move_z(3000.3)
    assert len(commands)==1 and not scope.armed and scope.cancel.is_set()


def test_in_tolerance_oscillation_is_not_stationary():
    scope=precision_scope();count=[0]
    def oscillating():
        count[0]+=1
        return Position(10000,20000,3000+(.04 if count[0]%2 else -.04))
    scope.position=oscillating
    with pytest.raises(AcquisitionError,match='did not settle'):scope._settled_position(Position(10000,20000,3000))
    assert count[0]>=3


def test_three_stationary_samples_do_not_hide_final_drift():
    scope=precision_scope()
    positions=iter([Position(10000,20000,3000)]*3+[Position(10000,20000,3000.3)])
    scope.position=lambda:next(positions)
    with pytest.raises(AcquisitionError,match='drifted'):scope._settled_position(Position(10000,20000,3000))


@pytest.mark.parametrize('when',['first_sample','final_sample'])
def test_precision_never_expands_actual_travel_envelope(when):
    scope=precision_scope();target=Position(10000,20000,3999.95)
    outside=Position(10000,20000,4000.05)  # In target tolerance, outside safe Z.
    positions=iter(([target]*3 if when=='final_sample' else [])+[outside])
    scope.position=lambda:next(positions)
    with pytest.raises(SafetyError,match='travel envelope'):scope._settled_position(target)


def fake_mtb(step=.156,deviation=.234):
    controls=MtbControls.__new__(MtbControls);controls.config_path=Path('active-test.xml')
    requested=[]
    def value(number,unit):requested.append(unit);return number
    axis=N(StepWidth=lambda unit:value(step,unit),MaxDeviation=lambda unit:value(deviation,unit))
    controls.root=N(GetComponent=lambda name:axis if name=='MTBFocus' else None)
    controls.Continual=lambda component:component
    return controls,requested


@pytest.mark.parametrize('field',['step','deviation'])
@pytest.mark.parametrize('bad',[float('nan'),float('inf'),0,-.1,.5,156])
def test_invalid_controller_precision_fails_closed(field,bad):
    controls,_=fake_mtb(**{field:bad})
    with pytest.raises(AcquisitionError,match='submicron'):controls.focus_motion_spec()


def test_runtime_precision_uses_declared_micrometre_unit():
    controls,units=fake_mtb();spec=controls.focus_motion_spec()
    assert units==['µm','µm']
    assert spec['max_deviation_um']==pytest.approx(.234)
    assert spec['readback_rounding_allowance_um']==pytest.approx(.001)


def test_hardware_precision_is_cached_and_not_overwritten_by_default(monkeypatch):
    import flakefinder.mtb as mtb_module
    scope=bare_zen();scope._mtb=None
    controls,units=fake_mtb();created=[]
    monkeypatch.setattr(mtb_module,'MtbControls',lambda owner:(created.append(owner) or controls))
    assert scope.hardware_controls() is controls
    assert scope.hardware_controls() is controls
    assert len(created)==1 and len(units)==2
    assert scope._axis_tolerance('z')==pytest.approx(.235)
    assert scope._axis_tolerance('x')==pytest.approx(.3)


def test_invalid_precision_does_not_cache_half_initialized_controls(monkeypatch):
    import flakefinder.mtb as mtb_module
    scope=bare_zen();scope._mtb=None
    controls,_=fake_mtb(deviation=float('nan'));closed=[];controls.close=lambda:closed.append(True)
    monkeypatch.setattr(mtb_module,'MtbControls',lambda owner:controls)
    with pytest.raises(AcquisitionError):scope.hardware_controls()
    assert closed==[True] and scope._mtb is None
    assert not hasattr(scope,'focus_target_tolerance_um')


@pytest.mark.parametrize('target',[3040.,2960.])
def test_waypoint_headroom_tracks_larger_vendor_tolerance(target):
    scope=precision_scope();scope.focus_target_tolerance_um=.4
    state=[Position(10000,20000,3000)];moves=[];scope.position=lambda:state[0]
    def move(z):
        start=state[0];requested=Position(start.x,start.y,z)
        scope.limits.check_move(start,requested)
        if abs(z-start.z)<=scope.focus_target_tolerance_um:return start
        actual=Position(start.x,start.y,z+math.copysign(.4,z-start.z))
        scope.limits.check_move(start,actual)
        moves.append((start,requested,actual));state[0]=actual
        return actual
    scope.move_z=move
    result=move_z_bounded(scope,target)
    assert moves and abs(result.z-target)<=.4+1e-8
    assert all(abs(actual.z-start.z)<=10+1e-8 for start,_,actual in moves)
    assert all(abs(request.z-start.z)<=9.599+1e-8 for start,request,_ in moves)
    assert all(actual.x==10000 and actual.y==20000 for _,_,actual in moves)


def test_precision_incompatible_with_step_limit_is_rejected_before_motion():
    scope=precision_scope();scope.focus_target_tolerance_um=.4;scope.limits.max_z_step_um=.8
    scope.position=lambda:Position(10000,20000,3000);moves=[];scope.move_z=lambda z:moves.append(z)
    with pytest.raises(SafetyError,match='too small'):move_z_bounded(scope,3010)
    assert moves==[]
