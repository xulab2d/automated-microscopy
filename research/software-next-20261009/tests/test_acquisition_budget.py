import json
import numpy as np
import pytest
from flakefinder.model import Frame,Position,Cancelled,atomic_json
from flakefinder.autofocus import FocusResult
from flakefinder.acquisition_budget import AcquisitionBudget,BudgetScope,bounded_focus

class Clock:
    def __init__(self):self.now=0.
    def __call__(self):return self.now
class Scope:
    def __init__(self,clock):self.clock=clock;self.n=0;self.cancel=False
    def position(self):return Position(0,0,10)
    def check_cancel(self):
        if self.cancel:raise Cancelled('operator STOP')
    def capture(self):
        self.n+=1;self.clock.now+=2
        return Frame(np.zeros((3,3,3),np.uint16),self.position(),(1,1),self.n)

@bounded_focus(60,10)
def one_last_frame(scope,output=None):
    f=scope.capture();sample={'z_um':f.position.z,'score':1,'phase':'confirmation'}
    if output is not None:atomic_json(output/'focus-curve.json',{'samples':[sample]})
    return FocusResult(True,10,1,1,'Focused',[sample])

def test_slow_last_frame_is_saved_but_never_accepted(tmp_path,monkeypatch):
    import flakefinder.storage as storage
    monkeypatch.setattr(storage,'load_policy',lambda:None)
    clock=Clock();scope=Scope(clock);wrapped=BudgetScope(scope,AcquisitionBudget(1,10,clock=clock))
    result=one_last_frame(wrapped,tmp_path)
    assert not result.success and result.outcome=='time_budget_exhausted' and len(result.samples)==1
    assert json.loads((tmp_path/'focus-result.json').read_text())['success'] is False
    assert json.loads((tmp_path/'focus-curve.json').read_text())['samples']==result.samples

def test_nested_capture_limit_has_no_hidden_reset(tmp_path,monkeypatch):
    import flakefinder.storage as storage
    monkeypatch.setattr(storage,'load_policy',lambda:None)
    clock=Clock();scope=Scope(clock);wrapped=BudgetScope(scope,AcquisitionBudget(60,1,clock=clock))
    assert one_last_frame(wrapped,tmp_path).success
    result=one_last_frame(wrapped,tmp_path)
    assert not result.success and result.outcome=='capture_budget_exhausted'
    assert scope.n==1 and len(result.samples)==1

def test_operator_stop_has_priority_over_budget():
    clock=Clock();scope=Scope(clock);wrapped=BudgetScope(scope,AcquisitionBudget(1,1,clock=clock))
    clock.now=5;scope.cancel=True
    with pytest.raises(Cancelled):one_last_frame(wrapped)

def test_budgeted_processed_capture_is_counted_once():
    clock=Clock();scope=Scope(clock);wrapped=BudgetScope(scope,AcquisitionBudget(20,2,clock=clock))
    _,number=wrapped.capture_with_processing(lambda f:f.sequence)
    assert number==1 and wrapped.budget.captures==1 and scope.n==1
