"""Offline boundary checks for the simplified operator contract."""
import pytest
from flakefinder.scan_service import DEFAULTS, validate_settings


def request(**changes):
    return dict(DEFAULTS, sample_id='Fresh graphene', operator_name='Lab operator',
                width_mm=100, height_mm=100, expected_chips=16, layout='grid',
                min_chip_mm=5, max_chips=100, analysis_after_scan=True, **changes)


def test_operator_contract_preserves_target_separate_from_attempt_budget():
    settings=validate_settings(request())
    assert settings['expected_chips']==16 and settings['max_chips']==100
    assert settings['analysis_after_scan'] and not settings['operator_ready']
    assert settings['substrate']=='SiO2/Si'


@pytest.mark.parametrize('changes',[
    {'operator_name':''}, {'operator_name':'A\nB'}, {'expected_chips':0},
    {'expected_chips':True}, {'expected_chips':2.5}, {'expected_chips':17},
    {'grid_rows':0}, {'grid_columns':1.5}, {'substrate':'glass'},
    {'analysis_after_scan':'yes'}, {'material':'MoTe2'}, {'oxide_nm':285},
    {'max_chips':5}, {'layout':'random unbounded'},
])
def test_invalid_workflow_is_rejected_before_hardware(changes):
    value=request();value.update(changes)
    with pytest.raises(ValueError):validate_settings(value)


def test_scattered_count_does_not_require_grid_shape_and_scan_only_is_explicit():
    value=request();value.update(layout='scattered',expected_chips=20,material='MoTe2',analysis_after_scan=False)
    assert validate_settings(value)['expected_chips']==20


def test_historical_request_does_not_opt_into_new_pipeline():
    value={k:v for k,v in DEFAULTS.items() if k not in ('layout','expected_chips','analysis_after_scan','operator_name','substrate','grid_rows','grid_columns')}
    value.update(sample_id='Old run',width_mm=8,height_mm=8)
    result=validate_settings(value)
    assert result['layout']=='legacy' and result['expected_chips'] is None
    assert result['analysis_after_scan'] is False
