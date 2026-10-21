"""Read-only capability probe. Never sends a move, initialization, or acquisition."""
from pathlib import Path
import sys, json, importlib
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'generated'))
import grpc
from google.protobuf.json_format import MessageToDict

root = Path(r'C:\ProgramData\Carl Zeiss\ZenApiGateway')
ca = (root / 'Certificates/ZenApiPersonalSigningRootCA.pem').read_bytes()
token = (root / 'GlobalControlToken.txt').read_text(encoding='utf-8-sig').strip()
channel = grpc.secure_channel('localhost:5002', grpc.ssl_channel_credentials(ca),
    options=[('grpc.max_receive_message_length', 128 * 1024 * 1024)])
metadata = [('control-token', token)]
result = {}
for module, service, method in [
    ('zen_api.lm.hardware.v2.stage_service', 'StageService', 'GetPosition'),
    ('zen_api.lm.hardware.v2.focus_service', 'FocusService', 'GetPosition'),
    ('zen_api.hardware.v1.stage_service', 'StageService', 'GetAvailableStageAxis'),
    ('zen_api.acquisition.v1beta.experiment_service', 'ExperimentService', 'GetAvailableExperiments'),
    ('zen_api.acquisition.v1beta.experiment_service', 'ExperimentService', 'GetStatus'),
]:
    key = module + '.' + method
    try:
        pb = importlib.import_module(module + '_pb2')
        stub = getattr(importlib.import_module(module + '_pb2_grpc'), service + 'Stub')(channel)
        response = getattr(stub, method)(getattr(pb, service + method + 'Request')(), timeout=6, metadata=metadata)
        result[key] = MessageToDict(response, preserving_proto_field_name=True)
    except grpc.RpcError as e:
        result[key] = {'error': e.code().name, 'detail': e.details()}
print(json.dumps(result, indent=2))
dest = Path(__file__).resolve().parents[1] / 'data'
dest.mkdir(exist_ok=True)
(dest / 'hardware-probe.json').write_text(json.dumps(result, indent=2))
channel.close()
