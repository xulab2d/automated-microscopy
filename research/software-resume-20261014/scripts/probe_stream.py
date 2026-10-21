"""Read one existing live frame without changing acquisition or moving hardware."""
from pathlib import Path
import sys, json
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'generated'))
import grpc, numpy as np
from PIL import Image
from google.protobuf.json_format import MessageToDict
from zen_api.acquisition.v1beta import experiment_streaming_service_pb2 as pb
from zen_api.acquisition.v1beta import experiment_streaming_service_pb2_grpc as rpc
r = Path(r'C:\ProgramData\Carl Zeiss\ZenApiGateway')
channel = grpc.secure_channel('localhost:5002',grpc.ssl_channel_credentials((r/'Certificates/ZenApiPersonalSigningRootCA.pem').read_bytes()),options=[('grpc.max_receive_message_length',128*1024*1024)])
meta=[('control-token',(r/'GlobalControlToken.txt').read_text(encoding='utf-8-sig').strip())]
call=rpc.ExperimentStreamingServiceStub(channel).MonitorAllExperiments(pb.ExperimentStreamingServiceMonitorAllExperimentsRequest(enable_raw_data=False),metadata=meta,timeout=15)
try:
    f=next(call).frame_data
    pixels=f.pixel_data
    a=np.frombuffer(pixels.raw_data, dtype=np.uint16 if pixels.pixel_type in (2,5) else np.uint8)
    a=a.reshape(pixels.size.height,pixels.size.width,-1)
    if a.shape[-1]==3: a=a[:,:,::-1]
    out=Path(__file__).resolve().parents[1]/'data'
    out.mkdir(exist_ok=True)
    np.save(out/'first-live-frame.npy',a)
    print('raw percentiles', np.percentile(a,[0,1,50,99,100]).tolist())
    lo,hi=np.percentile(a,[1,99.8])
    preview=np.uint8(np.clip((a.astype(float)-lo)/max(hi-lo,1)*255,0,255))
    Image.fromarray(preview.squeeze()).save(out/'first-live-frame.png')
    f.pixel_data.raw_data=b''
    d=MessageToDict(f,preserving_proto_field_name=True)
    (out/'first-live-frame.json').write_text(json.dumps(d,indent=2))
    print(json.dumps(d,indent=2))
except grpc.RpcError as e:
    print(e.code().name,e.details())
finally:
    call.cancel()
    channel.close()
