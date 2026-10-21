"""Compare the installed stage API contracts using a 10 µm Y displacement."""
from pathlib import Path
import sys
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from flakefinder.config import load_config,connect,arm
from flakefinder.model import Position
from google.protobuf.wrappers_pb2 import DoubleValue
cfg=load_config('configs/zen-validation.json');s=connect(cfg)
try:
    arm(s,cfg);origin=s.position()
    pb,stub=s._service('zen_api.lm.hardware.v1.stage_service','StageService')
    target=Position(origin.x,round(origin.y+10,3),origin.z);s.limits.check_move(origin,target)
    print('Before',origin,flush=True)
    s._motion_rpc(stub,pb.StageServiceMoveToRequest(x=DoubleValue(value=origin.x),y=DoubleValue(value=target.y)))
    print('Settled',s._settled_position(target),flush=True)
except BaseException:
    print('After error',s.position(),flush=True);s.stop();raise
finally:s.close()
