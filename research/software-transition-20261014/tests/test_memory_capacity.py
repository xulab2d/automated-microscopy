import pytest
from flakefinder import memory_capacity as m,acquisition_resources as a,scan_worker as worker

def state(physical=64*m.MIB,commit=8*1024**3,address=1024**4):
    return dict(physical_available_bytes=physical,commit_available_bytes=commit,address_available_bytes=address)

def test_low_physical_ram_is_not_allocation_exhaustion(monkeypatch):
    monkeypatch.setattr(m,'snapshot',lambda:state())
    result=a.check_memory_headroom(m.capture_working_bytes(2464*2056*3*2))
    assert result['adequate'] and result['physical_free_floor_bytes']==0
    assert result['working_allowance_bytes']==6*2464*2056*3*2+64*m.MIB

@pytest.mark.parametrize('s',[state(commit=1),state(address=1)])
def test_real_commit_or_address_exhaustion_blocks_before_camera(s,monkeypatch):
    monkeypatch.setattr(m,'snapshot',lambda:s)
    with pytest.raises(a.MemoryHeadroomError,match='allocation capacity'):a.check_memory_headroom()

def test_unreadable_capacity_is_not_claimed_as_adequate(monkeypatch):
    monkeypatch.setattr(m,'snapshot',lambda:dict(physical_available_bytes=10**12))
    with pytest.raises(a.AcquisitionError,match='Missing'):a.check_memory_headroom()

def test_watchdog_accepts_paging_and_rejects_real_control_capacity_loss():
    value={'free_disk_bytes':{'cache':100*1024**3},'available_ram_bytes':64*m.MIB,'memory_capacity':state()}
    worker.check_resources(value)
    value['memory_capacity']=state(commit=1)
    with pytest.raises(MemoryError):worker.check_resources(value)

def test_large_frames_budget_more_and_bad_estimates_fail():
    assert m.capture_working_bytes(128*m.MIB)>m.capture_working_bytes(30*m.MIB)
    for value in (0,-1,129*m.MIB,1.5):
        with pytest.raises(ValueError):m.capture_working_bytes(value)
