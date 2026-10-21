"""Profile handoff tests do not create Tk windows or connect to hardware."""
from copy import deepcopy
import pytest
from flakefinder.gui import configuration_for_objective_profile,describe_optics
from flakefinder.model import SafetyError


def fixtures():
    config={'schema':1,'backend':'zen','zen':{'job_template':'old','camera_resources':{'ff_exposure_ms':200},'expected_objective_position':1},
            'context':{'sample_id':'sample-a','objective':'10x'},'limits':{'z_min':100,'z_max':200},
            'initial_focus':{'low':110,'high':190},'calibration_quality':{'old':True},
            'camera_transform':{'matrix':[[.345,0],[0,.345]],'pixel_size_um':[.345,.345],'calibrated':True}}
    objective={'position':2,'name':'20x objective','magnification':20}
    profile={'schema':1,'objective':objective,'job_template':'new','camera_resources':{'ff_exposure_ms':320,'ff_white_balance':'.45,1,.57'},
             'camera_transform':{'matrix':[[.1725,0],[0,.1725]],'pixel_size_um':[.1725,.1725],'calibrated':True},
             'focus':{'half_range_um':12,'coarse_step_um':2,'fine_step_um':.5},'illumination_percent':93.4,
             'context':{'sample_id':'sample-a','material':'MoTe2','objective':'20x objective'},'calibration_position':{'z':9999}}
    return config,profile,{'position':2,'objectives':[objective]}


def test_handoff_replaces_all_optics_and_does_not_copy_historical_z():
    config,profile,state=fixtures();before=deepcopy(config)
    result=configuration_for_objective_profile(config,profile,state)
    assert result['camera_transform']==profile['camera_transform']
    assert result['zen']['camera_resources']==profile['camera_resources']
    assert result['zen']['expected_objective_position']==2
    assert result['context']['exposure_ms']==320
    assert result['context']['white_balance']=='.45,1,.57'
    assert result['initial_focus'] is None
    assert result['limits']==config['limits']
    assert 'calibration_quality' not in result
    assert config==before
    result['zen']['camera_resources']['ff_exposure_ms']=1
    assert profile['camera_resources']['ff_exposure_ms']==320


@pytest.mark.parametrize('position',[0,1,3])
def test_handoff_rejects_wrong_or_unseated_objective(position):
    config,profile,state=fixtures();state['position']=position
    with pytest.raises(SafetyError,match='Manually engage'):configuration_for_objective_profile(config,profile,state)


def test_handoff_rejects_another_sample_or_unmeasured_transform():
    config,profile,state=fixtures();profile['context']['sample_id']='another'
    with pytest.raises(SafetyError,match='another sample'):configuration_for_objective_profile(config,profile,state)
    profile['context']['sample_id']='sample-a';profile['camera_transform']['calibrated']=False
    with pytest.raises(SafetyError,match='measured'):configuration_for_objective_profile(config,profile,state)


def test_same_objective_preserves_current_search_range_and_unknown_is_explicit():
    config,profile,state=fixtures();config['zen']['expected_objective_position']=2
    result=configuration_for_objective_profile(config,profile,state)
    assert result['initial_focus']==config['initial_focus']
    state['position']=0
    description=describe_optics(result,state,-.0978)
    assert 'NO ENGAGED OBJECTIVE' in description and 'illumination unavailable' in description
