"""Pure fake MTB interfaces: never load or connect a hardware server."""
from pathlib import Path
from types import SimpleNamespace as N
import pytest

from flakefinder.model import AcquisitionError, Position
from flakefinder.mtb import MtbControls


class Axis:
    def __init__(self,low=-65000.,high=65000.,position=0.,available=3,enabled=(True,True),software=(0.,60000.),
                 calibration='OnCurrentPosition',direction='Positive',hardware_limit='None'):
        self.nominal=(low,high);self.position=position;self.HasSWLimit=available
        self.enabled=enabled;self.software=software;self.calls=[]
        self.CalibrationMode=calibration;self.CoordinateDirection=direction;self.IsOnHWLimit=hardware_limit
    def GetMinPosition(self,unit):assert unit=='µm';return self.nominal[0]
    def GetMaxPosition(self,unit):assert unit=='µm';return self.nominal[1]
    def GetPosition(self,unit):assert unit=='µm';return self.position
    def IsSWLimitEnabled(self,upper):self.calls.append(('enabled',upper));return self.enabled[int(upper)]
    def GetSWLimit(self,upper,unit):assert unit=='µm';self.calls.append(('limit',upper));return self.software[int(upper)]


def controls(y=None,x=None):
    c=MtbControls.__new__(MtbControls);c.config_path=Path('offline-active.xml')
    c.axes={'MTBStageAxisX':x or Axis(-85000.,85000.,43510.999,software=(0.,85000.)),
            'MTBStageAxisY':y or Axis()}
    c.root=N(GetComponent=lambda name:c.axes[name]);c.Continual=lambda value:value;c.Axis=lambda value:value
    c.AxisLimits=N(Lower=2,Upper=1)
    return c


def test_enabled_zero_y_limit_corrects_nominal_negative_range():
    result=controls().stage_travel_range()
    assert result['y_min_um']==0. and result['y_max_um']==60000.
    evidence=result['axis_limits']['y']
    assert evidence['nominal_min_um']==-65000.
    assert evidence['software_limits']['lower']=={'available':True,'enabled':True,'position_um':0.}
    assert evidence['settings_changed'] is False


def test_disabled_limit_never_clips_or_requests_unusable_position():
    axis=Axis(enabled=(False,True),software=(float('nan'),60000.))
    result=controls(y=axis).stage_travel_range()
    assert result['y_min_um']==-65000. and result['y_max_um']==60000.
    assert ('limit',False) not in axis.calls


def test_unavailable_limits_are_not_queried_and_nominal_is_retained():
    axis=Axis(available=0)
    result=controls(y=axis).stage_travel_range()
    assert (result['y_min_um'],result['y_max_um'])==axis.nominal
    assert axis.calls==[]


def test_one_available_limit_and_looser_software_bounds_do_not_expand_nominal():
    result=controls(y=Axis(available=2,software=(-70000.,0.))).stage_travel_range()
    assert (result['y_min_um'],result['y_max_um'])==(-65000.,65000.)


@pytest.mark.parametrize('software',[(float('nan'),60000.),(10000.,5000.),(0.,float('inf'))])
def test_invalid_enabled_limits_refuse_nominal_fallback(software):
    with pytest.raises(AcquisitionError):controls(y=Axis(software=software)).stage_travel_range()


def test_failed_enabled_limit_read_is_not_silently_ignored():
    axis=Axis()
    def fail(upper):raise RuntimeError('controller read failed')
    axis.IsSWLimitEnabled=fail
    with pytest.raises(AcquisitionError,match='software travel limits'):controls(y=axis).stage_travel_range()


def test_worker_search_bounds_and_backlash_stay_above_effective_zero():
    from flakefinder.scan_worker import resolve_grid_travel
    c=controls();scope=N(hardware_controls=lambda:c,position=lambda:Position(43510.999,0.,19074.218))
    config={'zen':{'xy_backlash_um':30.},'limits':{}}
    bounds,audit=resolve_grid_travel(scope,config,{'layout':'scattered'},[-6489.001,93510.999,-50000.,50000.])
    assert bounds==[31.,84999.,31.,50000.]
    assert config['limits']['y_min']==0.
    assert audit['controller_range']['axis_limits']['y']['software_limits']['lower']['position_um']==0.


def test_actual_live_origin_and_shifted_software_limits_exclude_negative_y():
    # Exact read-only leased live evidence 2026-10-15. The Y hardware switch
    # reports Upper at numeric zero; interpreting that as ymax would be wrong.
    y=Axis(software=(-39726.15,90273.85),calibration='OnLowerLimit',hardware_limit='Upper')
    x=Axis(-85000.,85000.,42743.769,software=(-34268.98,135731.02),calibration='OnLowerLimit')
    result=controls(y=y,x=x).stage_travel_range()
    assert (result['x_min_um'],result['x_max_um'])==(0.,85000.)
    assert (result['y_min_um'],result['y_max_um'])==(0.,65000.)
    evidence=result['axis_limits']['y']
    assert evidence['hardware_limit_state']=='Upper'
    assert evidence['software_limits']['lower']['position_um']==-39726.15
    assert evidence['calibration_origin_constraint']['min_um']==0.
    assert evidence['calibration_origin_constraint']['hardware_switch_label_used_as_coordinate_bound'] is False


@pytest.mark.parametrize('calibration,direction',[
    ('OnCurrentPosition','Positive'),('NotCalibrated','Positive'),('OnLowerLimit','Negative')])
def test_unqualified_origins_do_not_invent_zero_bound(calibration,direction):
    result=controls(y=Axis(available=0,calibration=calibration,direction=direction)).stage_travel_range()
    assert result['y_min_um']==-65000.
    assert 'calibration_origin_constraint' not in result['axis_limits']['y']
