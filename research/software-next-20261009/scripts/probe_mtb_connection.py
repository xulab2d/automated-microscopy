"""Query the running MTB connection; never initialize, home, or change configuration."""
from pathlib import Path
import sys
from pythonnet import load
base=Path(r'C:\Program Files\Carl Zeiss\ZEN core')
load('coreclr',runtime_config=str(base/'ZENcore.runtimeconfig.json'))
sys.path.insert(0,str(base))
import clr
clr.AddReference(str(base/'Microsoft.Extensions.Logging.Abstractions.dll'))
clr.AddReference(str(base/'MTBApi.dll'))
from ZEISS.MTB.Api import MTBConnection, IMTBComponent, IMTBChanger, IMTBContinual, MTBCmdSetModes
connection=MTBConnection()
client=None
try:
    print('Version',connection.MTBVersion,flush=True)
    print('Server',connection.MTBServerLocation,flush=True)
    print('Configuration',connection.ActiveConfigurationFile,flush=True)
    print('Changed',connection.ConfigurationHasChanged,flush=True)
    if connection.MTBVersion!='3.11.9.0' or connection.ConfigurationHasChanged:
        raise RuntimeError('Not the already initialized ZEN hardware server.')
    client=connection.Login('en','')
    print('Login established',bool(client),flush=True)
    root=connection.GetRoot(client)
    component=root.GetComponent('MTBObjectiveChanger')
    changer=IMTBChanger(component)
    print('Objective motorization',component.Motorization,flush=True)
    print('Objective accessibility',component.Accessibility,flush=True)
    print('Objective SetPosition modes',component.AvailableCmdSetModes('SetPosition'),flush=True)
    print('Objective Position modes',component.AvailableCmdSetModes('Position'),flush=True)
    print('Objective position',changer.Position,'range',changer.MinPosition,changer.MaxPosition,flush=True)
    print('Elements',changer.GetElementCount(),flush=True)
    print('Root methods',[str(m) for m in root.GetType().GetMethods() if 'Component' in m.Name],flush=True)
    from System import Enum
    print('Command modes',list(Enum.GetNames(MTBCmdSetModes)),flush=True)
    lamp=root.GetComponent('MTBRLHalogenLamp');control=IMTBContinual(lamp)
    print('Lamp motorization',lamp.Motorization,flush=True)
    for i in range(control.GetPositionUnitCount()):
        unit=control.GetPositionUnit(i)
        print('Lamp unit',unit,'position',control.GetPosition(unit),'limits',control.GetMinPosition(unit),control.GetMaxPosition(unit),flush=True)
finally:
    if client:connection.Logout(client)
    connection.Close()
