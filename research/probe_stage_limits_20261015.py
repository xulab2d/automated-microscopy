"""Read-only leased stage diagnostic; no move, calibration or setting writes."""
from pathlib import Path
import sys, json
root=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(root/'research/software-next-20261009'))
sys.path.insert(0,str(root/'generated'))
from flakefinder.lease import HardwareLease
from flakefinder.mtb import MtbControls
from flakefinder.model import Position
import grpc
from zen_api.lm.hardware.v1 import stage_service_pb2 as pb, stage_service_pb2_grpc as pg
from zen_api.lm.hardware.v2 import focus_service_pb2 as fp, focus_service_pb2_grpc as fg
gateway=Path(r'C:\ProgramData\Carl Zeiss\ZenApiGateway')
lease=HardwareLease('localhost:5002',root/'research/software-next-20261009/data/locks')
lease.acquire()
channel=None; controls=None
try:
    channel=grpc.secure_channel('localhost:5002',grpc.ssl_channel_credentials((gateway/'Certificates/ZenApiPersonalSigningRootCA.pem').read_bytes()))
    metadata=[('control-token',(gateway/'GlobalControlToken.txt').read_text(encoding='utf-8-sig').strip())]
    stage=pg.StageServiceStub(channel); focus=fg.FocusServiceStub(channel)
    class ReadOnlyScope:
        def position(self):
            xy=stage.GetPosition(pb.StageServiceGetPositionRequest(),metadata=metadata,timeout=6)
            z=focus.GetPosition(fp.FocusServiceGetPositionRequest(),metadata=metadata,timeout=6)
            return Position(xy.x,xy.y,z.value*1e6)
    scope=ReadOnlyScope(); controls=MtbControls(scope)
    from ZEISS.MTB.Api import IMTBAxis
    result={'position':vars(scope.position()),'axes':{}}
    for name in ('x','y'):
        component=controls.root.GetComponent('MTBStageAxis'+name.upper())
        axis=IMTBAxis(component); continual=controls.Continual(component)
        row={'nominal_min_um':float(continual.GetMinPosition('µm')),'nominal_max_um':float(continual.GetMaxPosition('µm'))}
        for key in ('HasSWLimit','IsOnSWLimit','CalibrationMode','CoordinateDirection','IsOnHWLimit'):
            try:row[key]=str(getattr(axis,key))
            except Exception as exc:row[key]={'error':str(exc)}
        for upper in (False,True):
            side='upper' if upper else 'lower'
            for key,call in [('enabled',lambda:axis.IsSWLimitEnabled(upper)),('position_um',lambda:axis.GetSWLimit(upper,'µm'))]:
                try:row[side+'_'+key]=call()
                except Exception as exc:row[side+'_'+key]={'error':str(exc)}
        result['axes'][name]=row
    result['effective_range']=controls.stage_travel_range()
    (root/'research/stage-limits-corrected-live-20261015.json').write_text(json.dumps(result,indent=2),encoding='utf-8')
    print(json.dumps(result,indent=2))
finally:
    if controls:controls.close()
    if channel:channel.close()
    lease.close()
