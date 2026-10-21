"""Cooperative image/time budgets shared by nested focus operations.

Never converts STOP or hardware exceptions into a successful focus result.
Checks happen around motion and before capture. A returned image may finish its
evidence checkpoint before the next/final deadline check; a late peak is never
accepted. A vendor RPC can overrun by its timeout and shutdown allowance. This
is not a hard realtime motion watchdog.
"""
from contextlib import contextmanager
from dataclasses import asdict
from functools import wraps
import inspect
import json
import math
from pathlib import Path
import time

from .model import AcquisitionError,atomic_json

class BudgetExhausted(AcquisitionError):
    def __init__(self,kind):
        self.outcome=kind
        super().__init__(kind.replace('_',' '))

class AcquisitionBudget:
    def __init__(self,max_seconds,max_captures,*,clock=time.monotonic):
        if not math.isfinite(max_seconds) or max_seconds<=0 or type(max_captures) is not int or max_captures<1:
            raise ValueError('Acquisition budget requires positive seconds and integer capture count.')
        self.clock=clock;self.started=clock();self.max_seconds=float(max_seconds);self.max_captures=max_captures
        self.captures=0;self.last_position=None;self.completed_actions=0
    def check(self):
        if self.clock()-self.started>=self.max_seconds:raise BudgetExhausted('time_budget_exhausted')
    def capture(self):
        self.check()
        if self.captures>=self.max_captures:raise BudgetExhausted('capture_budget_exhausted')
        self.captures+=1
    def report(self):
        return {'max_seconds':self.max_seconds,'max_captures':self.max_captures,'captures_started':self.captures,
                'elapsed_seconds':self.clock()-self.started,'completed_actions':self.completed_actions,
                'deadline_kind':'cooperative; bounded vendor call/cleanup may finish after deadline'}

class BudgetScope:
    def __init__(self,scope,budget):self.scope=scope;self.budget=budget
    def __getattr__(self,name):return getattr(self.scope,name)
    def __setattr__(self,name,value):
        if name in ('scope','budget','check_cancel') or 'scope' not in self.__dict__:object.__setattr__(self,name,value)
        else:setattr(self.scope,name,value)
    def check_cancel(self):self.scope.check_cancel();self.budget.check()
    def position(self):
        value=self.scope.position();self.budget.last_position=value;return value
    def capture(self):
        self.check_cancel();self.budget.capture();frame=self.scope.capture()
        self.budget.last_position=frame.position;self.budget.completed_actions+=1;self.scope.check_cancel();return frame
    def capture_with_processing(self,processor):
        self.check_cancel();self.budget.capture()
        if hasattr(self.scope,'capture_with_processing'):frame,result=self.scope.capture_with_processing(processor)
        else:frame=self.scope.capture();result=processor(frame)
        self.budget.last_position=frame.position;self.budget.completed_actions+=1;self.scope.check_cancel()
        return frame,result
    def move_z(self,*args,**kwargs):
        self.check_cancel();value=self.scope.move_z(*args,**kwargs);self.budget.completed_actions+=1;self.check_cancel();return value
    def travel(self,*args,**kwargs):
        self.check_cancel();value=self.scope.travel(*args,**kwargs);self.budget.completed_actions+=1;self.check_cancel();return value

def scope_budget(scope):
    return scope.budget if isinstance(scope,BudgetScope) else None

@contextmanager
def budget_scope(scope,max_seconds,max_captures):
    existing=scope_budget(scope)
    yield scope if existing else BudgetScope(scope,AcquisitionBudget(max_seconds,max_captures))

def saved_samples(output):
    if output is None:return []
    from .storage import managed_output_path
    folder=managed_output_path(output);candidates=[]
    for name in ('focus-curve.json','recovery.json','quick-focus.json'):
        path=folder/name
        if not path.exists():continue
        data=json.loads(path.read_text(encoding='utf-8'))
        rows=data.get('samples',data.get('quick_samples',[]))
        if data.get('confirmation'):rows=[*rows,data['confirmation']]
        candidates.append(rows)
    return max(candidates,key=len,default=[])

def bounded_focus(default_seconds,default_captures):
    """Add explicit max_elapsed_s/max_captures keywords, shared by fallbacks."""
    def decorate(function):
        signature=inspect.signature(function)
        @wraps(function)
        def run(scope,*args,**kwargs):
            seconds=kwargs.pop('max_elapsed_s',default_seconds);captures=kwargs.pop('max_captures',default_captures)
            output=signature.bind_partial(scope,*args,**kwargs).arguments.get('output')
            parent=scope_budget(scope)
            with budget_scope(scope,seconds,captures) as bounded:
                result=None
                try:
                    result=function(bounded,*args,**kwargs)
                    bounded.check_cancel()  # Never accept a late last capture.
                except BudgetExhausted as exc:
                    from .autofocus import FocusResult
                    position=bounded.budget.last_position
                    samples=result.samples if result is not None else saved_samples(output)
                    result=FocusResult(False,position.z if position else 0.,0.,0.,str(exc),samples,outcome=exc.outcome)
                result.budget=bounded.budget.report()
                if result.outcome not in ('capture_budget_exhausted','time_budget_exhausted'):
                    result.outcome=focus_outcome(result.success,result.reason)
                if output is not None:
                    from .storage import managed_output_path
                    folder=managed_output_path(output)
                    atomic_json(folder/'focus-result.json',asdict(result))
                    if result.outcome in ('capture_budget_exhausted','time_budget_exhausted'):
                        for name in ('recovery.json','quick-focus.json'):
                            path=folder/name
                            if path.exists():
                                data=json.loads(path.read_text(encoding='utf-8'))
                                data.update(state='unresolved',outcome=result.outcome,result=asdict(result));atomic_json(path,data)
                    atomic_json(folder/'budget.json',{'outcome':result.outcome,**result.budget,
                        'shared_with_parent':parent is not None})
                return result
        return run
    return decorate

def focus_outcome(success,reason):
    if success:return 'resolved_peak'
    text=reason.lower()
    if 'travel limit' in text:return 'boundary_limited'
    if 'budget' in text:return 'budget_exhausted'
    if 'boundary' in text:return 'peak_outside_interval'
    if 'texture' in text or 'weak' in text:return 'insufficient_signal'
    if 'confirmation' in text:return 'confirmation_failed'
    return 'non_peak'
