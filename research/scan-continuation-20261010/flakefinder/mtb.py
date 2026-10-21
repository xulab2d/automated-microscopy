"""Optional ZEISS MTB interface for objective readback and reflected-light control.

Connects to the hardware server already used by ZEN. Never edits configuration,
initializes a different server, homes an axis, or changes a coded turret's label.
"""
from pathlib import Path
import sys
import xml.etree.ElementTree as ET
from .model import SafetyError,AcquisitionError

class MtbControls:
    def __init__(self,scope,expected_version='3.11.9.0',zen_dir=r'C:\Program Files\Carl Zeiss\ZEN core'):
        scope.position()  # ZEN must already have initialized the connected microscope.
        self.scope=scope;self.client=None;self.connection=None
        from pythonnet import load
        base=Path(zen_dir)
        load('coreclr',runtime_config=str(base/'ZENcore.runtimeconfig.json'))
        sys.path.insert(0,str(base))
        import clr
        clr.AddReference(str(base/'Microsoft.Extensions.Logging.Abstractions.dll'))
        clr.AddReference(str(base/'MTBApi.dll'))
        from ZEISS.MTB.Api import MTBConnection,IMTBChanger,IMTBContinual,MTBCmdSetModes
        self.Continual=IMTBContinual;self.mode=MTBCmdSetModes.Synchronous
        self.connection=MTBConnection()
        try:
            if self.connection.MTBVersion!=expected_version or self.connection.ConfigurationHasChanged:
                raise SafetyError('MTB server/configuration differs from the validated ZEN hardware connection.')
            self.config_path=Path(str(self.connection.ActiveConfigurationFile))
            self.client=self.connection.Login('en','');self.root=self.connection.GetRoot(self.client)
            self.component=self.root.GetComponent('MTBObjectiveChanger');self.changer=IMTBChanger(self.component)
        except BaseException:self.close();raise

    def objectives(self):
        node=ET.parse(self.config_path).getroot().find('.//Component[@MTBId="MTBObjectiveChanger"]')
        entries=[]
        for element in node.findall('./ChangerElements/Element'):
            if element.get('Name')=='Objective.none':continue
            entries.append({'position':int(element.get('Position'))+1,'name':element.get('DisplayName'),
                'magnification':float(element.findtext('Magnification')),
                'na':float(element.findtext('Aperture')),'working_distance_um':float(element.findtext('WorkingDistance'))})
        position=int(self.changer.Position)
        return {'position':position,'valid_position':position in [o['position'] for o in entries],
                'position_status':'engaged' if position in [o['position'] for o in entries] else 'between stops or unavailable',
                'motorization':str(self.component.Motorization),
                'accessibility':str(self.component.Accessibility),'objectives':entries}

    def verify_optics(self,expected_position=None):
        state=self.objectives()
        if not state['valid_position']:
            raise SafetyError('Objective readback is between stops or unavailable. Seat the manual nosepiece before acquisition or motion.')
        if expected_position is not None and state['position']!=expected_position:
            raise SafetyError(f"Objective position {state['position']} does not match this optical profile ({expected_position}).")
        lamp=self.lamp_percent()
        if not 0<lamp<=100:
            raise AcquisitionError('Reflected illumination is off or its readback is invalid.')
        return state

    def set_objective(self,position,clearance_z_um):
        if not self.scope.armed:raise SafetyError('Arm the microscope before objective changes.')
        state=self.objectives()
        if position not in [o['position'] for o in state['objectives']]:raise SafetyError('Requested objective position is empty or unconfigured.')
        if str(self.component.Motorization)!='Motorized':
            raise SafetyError('ZEISS reports a coded nosepiece and rejects electrical changes. Engage the objective physically, then verify its readback.')
        from .workflow import move_z_bounded
        move_z_bounded(self.scope,clearance_z_um);self.scope.check_cancel()
        try:
            if not self.changer.SetPosition(position,self.mode,10000) or int(self.changer.Position)!=position:
                raise AcquisitionError('Objective change was not verified; Z remains retracted.')
        except BaseException:self.scope.stop();raise
        self.scope.expected_scale=None
        return self.objectives()

    def lamp_percent(self):
        return float(self.Continual(self.root.GetComponent('MTBRLHalogenLamp')).GetPosition('%'))

    def focus_motion_spec(self):
        """Use the active controller's positioning specification, in micrometres."""
        import math
        control=self.Continual(self.root.GetComponent('MTBFocus'))
        step=float(control.StepWidth('µm'))
        deviation=float(control.MaxDeviation('µm'))
        # This acquisition adapter requires submicron positioning. Incompatible
        # metadata must fail explicitly rather than silently relax that contract.
        if not all(math.isfinite(v) and 0<v<.5 for v in (step,deviation)):
            raise AcquisitionError('Focus controller did not report valid submicron positioning precision.')
        return {'source':'active MTBFocus IMTBContinual','configuration':str(self.config_path),
                'units':'µm','step_width_um':step,'max_deviation_um':deviation,
                'readback_rounding_allowance_um':.001}

    def set_lamp_percent(self,value):
        import numpy as np
        if not self.scope.armed:raise SafetyError('Arm hardware before changing illumination.')
        if not np.isfinite(value) or not 0<value<=100:raise ValueError('Reflected light intensity must be in (0,100] percent.')
        self.scope.check_cancel();self.scope.lease.acquire()
        control=self.Continual(self.root.GetComponent('MTBRLHalogenLamp'))
        if abs(float(control.GetPosition('%'))-value)<.15:return float(control.GetPosition('%'))
        completed=control.SetPosition(float(value),'%',self.mode,5000)
        actual=float(control.GetPosition('%'))
        if abs(actual-value)>1:raise AcquisitionError(f'Reflected-light readback {actual:.3f}% differs from requested {value:.3f}% (command returned {completed}).')
        return actual

    def close(self):
        if self.connection:
            try:
                if self.client:self.connection.Logout(self.client)
            finally:
                self.client=None
                try:self.connection.Close()
                finally:self.connection=None
