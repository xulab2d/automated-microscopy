import pytest
from flakefinder.cpu_topology import select_processor_masks,processor_assignment


def test_separate_physical_cores_before_smt_siblings():
    assert select_processor_masks(0xff,2,[3,12,48,192])==[1,4]


def test_restricted_affinity_uses_only_allowed_processors():
    assert select_processor_masks(0b1010,2,[3,12])==[2,8]
    assert select_processor_masks(2,4,[3,12])==[2]


def test_smt_siblings_are_used_only_after_all_available_cores():
    assert select_processor_masks(15,4,[3,12])==[1,4,2,8]


def test_unknown_topology_preserves_bounded_logical_fallback(monkeypatch):
    def unavailable():raise OSError('test unavailable')
    monkeypatch.setattr('flakefinder.cpu_topology.windows_core_masks',unavailable)
    result=processor_assignment(12,2)
    assert result['processor_masks']==[4,8]
    assert result['affinity_mask']==12
    assert result['topology_source'].startswith('logical CPU fallback:')


@pytest.mark.parametrize('allowed,count',[(0,2),(-1,2),(3,0),(True,2),(3,True)])
def test_invalid_affinity_is_rejected(allowed,count):
    with pytest.raises(ValueError):select_processor_masks(allowed,count)
