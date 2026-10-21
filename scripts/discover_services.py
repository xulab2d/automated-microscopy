from pathlib import Path
import sys,json
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from flakefinder.zen import ZenMicroscope
from grpc_reflection.v1alpha import reflection_pb2, reflection_pb2_grpc
s=ZenMicroscope()
stub=reflection_pb2_grpc.ServerReflectionStub(s.channel)
try:
    r=next(stub.ServerReflectionInfo(iter([reflection_pb2.ServerReflectionRequest(list_services='')]),timeout=10,metadata=s.metadata))
    names=[v.name for v in r.list_services_response.service]
    print(json.dumps(names,indent=2))
    Path('data/services.json').write_text(json.dumps(names,indent=2))
finally: s.close()
