"""Bounded motion planned from actual settled encoder positions."""
import math
from .model import AcquisitionError, Position, SafetyError


def move_z_bounded(scope, z):
    """Move Z with readback headroom; retain the strict adapter step limit.

    ZEN may settle within its reported precision of a requested target. Fixed waypoints
    near the maximum step can therefore exceed that limit on the next move.
    Re-read each position and reserve headroom for the adapter's tolerance.
    No waypoint is retried after a hardware error and XY never changes.
    """
    z=float(z)
    if not scope.armed or scope.limits is None:raise SafetyError('Motion is not armed.')
    scope.check_cancel();initial=scope.position()
    scope.limits.check(Position(initial.x,initial.y,z))
    maximum=scope.limits.max_z_step_um
    guard=max(min(.25,.1*maximum),getattr(scope,'focus_target_tolerance_um',0)+.001)
    if guard>=maximum/2:raise SafetyError('Z step limit is too small for the controller positioning precision.')
    step=maximum-guard
    budget=math.ceil(abs(z-initial.z)/max(step-guard,step/2))+8
    for _ in range(budget):
        scope.check_cancel();current=scope.position();delta=z-current.z
        if abs(delta)<1e-9:return current
        final=abs(delta)<=step
        target=z if final else current.z+math.copysign(step,delta)
        scope.move_z(target)
        scope.check_cancel();actual=scope.position()
        if final:return actual  # The adapter has already verified settling.
        if abs(actual.z-current.z)<min(.001,step/10):
            raise AcquisitionError('Z readback made no progress during bounded motion.')
    raise AcquisitionError('Z motion did not converge within its bounded waypoint budget.')
