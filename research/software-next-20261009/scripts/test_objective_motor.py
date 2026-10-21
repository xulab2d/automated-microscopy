"""One vendor-supported objective command; verify physical readback, no configuration edits."""
from pathlib import Path
import sys,json,time
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from flakefinder.config import load_config,connect,arm
from flakefinder.model import atomic_json
from flakefinder.workflow import move_z_bounded
from pythonnet import load
base=Path(r'C:\Program Files\Carl Zeiss\ZEN core')
load('coreclr',runtime_config=str(base/'ZENcore.runtimeconfig.json'))
sys.path.insert(0,str(base))
import clr
clr.AddReference(str(base/'Microsoft.Extensions.Logging.Abstractions.dll'))
clr.AddReference(str(base/'MTBApi.dll'))
from ZEISS.MTB.Api import MTBConnection,IMTBChanger,MTBCmdSetModes
cfg=load_config('configs/zen-validation.json');cfg['limits']['z_min']=15400
scope=connect(cfg);connection=None;client=None
result={}
try:
    arm(scope,cfg);origin=scope.position()
    connection=MTBConnection()
    if connection.MTBVersion!='3.11.9.0' or connection.ConfigurationHasChanged:raise RuntimeError('Unexpected hardware server state')
    client=connection.Login('en','');root=connection.GetRoot(client)
    component=root.GetComponent('MTBObjectiveChanger');changer=IMTBChanger(component)
    if changer.Position!=1:raise RuntimeError('The microscope is no longer on the expected 10x objective.')
    result={'before_position':int(changer.Position),'motorization':str(component.Motorization),'requested_position':2,
            'original_z_um':origin.z,'retracted_z_um':origin.z-150}
    move_z_bounded(scope,origin.z-150)
    try:
        result['command_return']=bool(changer.SetPosition(2,MTBCmdSetModes.Synchronous,5000))
    except Exception as exc:
        result['command_error']=str(exc)
    time.sleep(.5);result['after_position']=int(changer.Position)
    result['electrical_change_verified']=result['after_position']==2
    # Restore focus only if the original objective is still positively identified.
    if result['after_position']==1:move_z_bounded(scope,origin.z)
    result['final_z_um']=scope.position().z
    atomic_json('data/objective-motor-test.json',result);print(json.dumps(result,indent=2),flush=True)
except BaseException:
    scope.stop();raise
finally:
    if connection:
        if client:connection.Logout(client)
        connection.Close()
    scope.close()
