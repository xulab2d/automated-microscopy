from types import SimpleNamespace
import pytest
from flakefinder.acquisition_resources import check_storage_headroom, StorageHeadroomError


def test_next_capture_checks_actual_output_volume_without_creating_it(tmp_path):
    output=tmp_path/'new'/'chip001';seen=[]
    def usage(path):
        seen.append(path);return SimpleNamespace(free=150)
    result=check_storage_headroom(output,reserve_bytes=100,next_capture_bytes=50,disk_usage=usage)
    assert seen==[tmp_path] and not output.exists()
    assert result['required_free_bytes']==150
    with pytest.raises(StorageHeadroomError,match='149 bytes free; 150 required'):
        check_storage_headroom(output,reserve_bytes=100,next_capture_bytes=50,
                               disk_usage=lambda p:SimpleNamespace(free=149))


@pytest.mark.parametrize('value',[-1,True,1.5,float('nan')])
def test_invalid_storage_allowance_is_rejected(tmp_path,value):
    with pytest.raises(ValueError):check_storage_headroom(tmp_path,reserve_bytes=value)
