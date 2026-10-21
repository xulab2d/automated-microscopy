"""Bounded analysis affinity must use different physical cores when available."""
import ctypes
import sys
from pathlib import Path
from types import SimpleNamespace
import pytest
sys.path.insert(0,str(Path(__file__).parents[1]/'scripts'))
import analyze_graphene_run as driver
from flakefinder import cpu_topology


class Function:
    def __init__(self,fn):self.fn=fn
    def __call__(self,*args):return self.fn(*args)


@pytest.fixture
def affinity(monkeypatch):
    if driver.os.name!='nt':pytest.skip('Windows affinity wrapper')
    calls=[]
    def get_mask(process,allowed,system):
        allowed._obj.value=255;system._obj.value=255;return 1
    kernel=SimpleNamespace(GetCurrentProcess=Function(lambda:1),
        GetProcessAffinityMask=Function(get_mask),
        SetProcessAffinityMask=Function(lambda process,mask:calls.append(mask) or 1))
    monkeypatch.setattr(ctypes,'WinDLL',lambda *a,**k:kernel)
    monkeypatch.setattr(driver.watcher,'background_priority',lambda:'test')
    import threadpoolctl
    monkeypatch.setattr(threadpoolctl,'threadpool_limits',lambda n:None)
    return calls


def test_two_workers_choose_separate_physical_cores(affinity,monkeypatch):
    seen=[]
    def assignment(allowed,count):
        seen.append((allowed,count))
        return {'processor_masks':[1,4],'affinity_mask':5,'topology_source':'test physical cores'}
    monkeypatch.setattr(cpu_topology,'processor_assignment',assignment)
    result=driver.cpu_policy(allowed_cpu_count=2)
    assert seen==[(255,2)] and affinity==[5]
    assert result['cpu_masks']==[1,4]
    assert result['cpu_topology']['topology_source']=='test physical cores'


def test_explicit_worker_assignment_is_preserved(affinity,monkeypatch):
    monkeypatch.setattr(cpu_topology,'processor_assignment',lambda *a:pytest.fail('Explicit assignment must remain fixed'))
    result=driver.cpu_policy(cpu_mask=4)
    assert affinity==[4] and result['cpu_masks']==[4]


def test_invalid_explicit_mask_is_rejected(affinity):
    with pytest.raises(ValueError,match='CPU mask'):
        driver.cpu_policy(cpu_mask=0)
    assert affinity==[]


def test_restricted_host_reuses_available_cpu_without_expanding_mask(affinity,monkeypatch):
    monkeypatch.setattr(cpu_topology,'processor_assignment',lambda allowed,count:{
        'processor_masks':[4],'affinity_mask':4,'topology_source':'restricted host'})
    result=driver.cpu_policy(allowed_cpu_count=2)
    assert affinity==[4] and result['cpu_masks']==[4,4]
    assert result['cpu_topology']['worker_masks_reused'] is True
