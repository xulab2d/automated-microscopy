"""Disabled-by-default stationary macro benchmark. --execute is required for RPCs.

This is isolated from the production camera backend. No movement method is
implemented here. The macro job must already be registered in ZEN API Mode.
"""
from pathlib import Path
import argparse
import hashlib
import json
import sys
import time
import uuid

HERE=Path(__file__).resolve().parent
ROOT=HERE.parents[1]
TITLE='FlakeFinder Request Snap Prototype v1'


def write_once(path,value):
    if path.exists():raise RuntimeError('Refusing to overwrite '+str(path))
    temporary=path.with_suffix(path.suffix+'.tmp')
    temporary.write_text(json.dumps(value,indent=2),encoding='utf-8')
    temporary.replace(path)


def validate_profile(profile):
    if profile.get('enabled') is not True:raise ValueError('Profile is disabled. Complete runtime readback bindings before enabling a stationary trial.')
    if not profile.get('camera_name'):raise ValueError('Camera name must come from inventory.json.')
    for role in ('white_balance','auto_exposure','auto_white_balance','hdr'):
        gate=profile.get('readback_gates',{}).get(role,{})
        if not gate.get('parameter_name'):raise ValueError('Missing verified parameter binding: '+role)
    if not 1<=float(profile['exposure_ms'])<=10000:raise ValueError('Invalid exposure.')


def run(args):
    profile=json.loads(args.profile.read_text()) if args.mode=='benchmark' else None
    if profile is not None:validate_profile(profile)
    if not args.execute:
        print('DRY RUN: no hardware connection. Register the generated macro job and use --execute only for an idle, stationary comparison.')
        return
    sys.path.insert(0,str(ROOT));sys.path.insert(0,str(ROOT/'generated'))
    import grpc
    from flakefinder.lease import HardwareLease
    from zen_api.workflows.v3beta import workflow_service_pb2 as wp,workflow_service_pb2_grpc as wg
    from zen_api.lm.hardware.v2 import stage_service_pb2 as sp,stage_service_pb2_grpc as sg
    from zen_api.lm.hardware.v2 import focus_service_pb2 as fp,focus_service_pb2_grpc as fg
    lease=HardwareLease('localhost:5002',ROOT/'data/locks')
    # Acquire the same exclusive lease BEFORE opening a channel or writing a
    # session, so this cannot run alongside the overnight acquisition client.
    lease.acquire()
    channel=None;future=None;owns_loaded_job=False;folder=None
    results=[];started=time.perf_counter()
    try:
        gateway=Path(r'C:\ProgramData\Carl Zeiss\ZenApiGateway')
        ca=(gateway/'Certificates/ZenApiPersonalSigningRootCA.pem').read_bytes()
        token=(gateway/'GlobalControlToken.txt').read_text(encoding='utf-8-sig').strip()
        if not token:raise RuntimeError('No control token.')
        metadata=(('control-token',token),)
        channel=grpc.secure_channel('localhost:5002',grpc.ssl_channel_credentials(ca))
        workflow=wg.WorkflowServiceStub(channel);stage=sg.StageServiceStub(channel);focus=fg.FocusServiceStub(channel)
        def rpc(stub,name,message,timeout=10):
            return getattr(stub,name)(message,metadata=metadata,timeout=timeout)
        if rpc(workflow,'IsJobRunning',wp.WorkflowServiceIsJobRunningRequest()).is_job_running:
            raise RuntimeError('An existing ZEN job is running; refusing to interfere.')
        if rpc(workflow,'IsJobTemplateLoaded',wp.WorkflowServiceIsJobTemplateLoadedRequest()).is_job_template_loaded:
            raise RuntimeError('An existing ZEN job is loaded; refusing to replace it.')
        def position():
            xy=rpc(stage,'GetPosition',sp.StageServiceGetPositionRequest())
            z=rpc(focus,'GetPosition',fp.FocusServiceGetPositionRequest())
            return [float(xy.x)*1e6,float(xy.y)*1e6,float(z.value)*1e6]
        origin=position()
        def stationary():
            current=position()
            if max(abs(x-y) for x,y in zip(origin,current))>.3:
                raise RuntimeError('Stage/focus moved during the stationary benchmark.')
            return current
        session_id=uuid.uuid4().hex;transport=HERE/'transport'
        transport.mkdir(exist_ok=True);folder=transport/session_id;folder.mkdir()
        deadline=time.time()+120
        if profile is not None:write_once(folder/'profile.json',profile)
        active={'schema':1,'session_id':session_id,'mode':args.mode,'count':args.count,
            'deadline_unix_s':deadline,'comparison_tiff':bool(args.comparison_tiff or args.baseline)}
        # This pointer is private to this prototype and replaced only under the lease.
        pointer=transport/'active.tmp';pointer.write_text(json.dumps(active),encoding='utf-8');pointer.replace(transport/'active.json')
        rpc(workflow,'LoadJobTemplate',wp.WorkflowServiceLoadJobTemplateRequest(
            job_template_name=TITLE,result_path=str(folder/'vendor-result')))
        owns_loaded_job=True
        future=workflow.RunJob.future(wp.WorkflowServiceRunJobRequest(),metadata=metadata,timeout=125)
        def wait_json(name):
            path=folder/name
            while not path.exists():
                error=folder/'error.json'
                if error.exists():raise RuntimeError(error.read_text())
                if time.time()>=deadline:raise TimeoutError('Prototype session deadline.')
                if future.done():
                    future.result()
                    if not path.exists():raise RuntimeError('Macro ended before '+name)
                time.sleep(.01)
            return json.loads(path.read_text())
        inventory=wait_json('inventory.json')
        if args.mode=='inventory':
            wait_json('done.json');future.result(timeout=max(.1,deadline-time.time()))
            print('Read-only camera inventory: '+str(folder/'inventory.json'))
            return
        ready=wait_json('ready.json')
        if ready.get('session_id')!=session_id:raise RuntimeError('Ready session mismatch.')
        for sequence in range(1,args.count+1):
            stationary();request_id=uuid.uuid4().hex;t=time.perf_counter()
            write_once(folder/f'request-{sequence:04d}.json',{'session_id':session_id,
                'sequence':sequence,'request_id':request_id,'deadline_unix_s':min(deadline,time.time()+20)})
            reply=wait_json(f'reply-{sequence:04d}.json')
            elapsed=time.perf_counter()-t
            if (reply.get('session_id'),reply.get('request_id'),reply.get('sequence'))!=(session_id,request_id,sequence):
                raise RuntimeError('Snapshot response identity mismatch.')
            if reply.get('czi')!=request_id+'.czi':raise RuntimeError('Unexpected output filename.')
            czi=folder/reply['czi']
            with czi.open('rb') as stream:
                if stream.read(10)!=b'ZISRAWFILE':raise RuntimeError('Output is not a CZI file.')
            reply.update({'request_to_saved_image_s':elapsed,'czi_bytes':czi.stat().st_size,
                          'czi_sha256':hashlib.sha256(czi.read_bytes()).hexdigest(),'position_um':stationary()})
            if reply.get('comparison_tiff'):
                if reply['comparison_tiff']!=request_id+'.tiff':raise RuntimeError('Unexpected comparison TIFF filename.')
                import cv2,numpy as np
                image=cv2.imread(str(folder/reply['comparison_tiff']),cv2.IMREAD_UNCHANGED)
                if image is None or list(image.shape)!=profile['expected_shape'] or str(image.dtype)!=profile['expected_dtype']:
                    raise RuntimeError('TIFF export changed dimensions/dtype. Raw CZI retained; export is not qualified.')
                image=image[:,:,::-1]
                reply['comparison']={'dtype':str(image.dtype),'shape':list(image.shape),
                    'rgb_median':np.median(image.reshape(-1,3),axis=0).tolist(),'max_adc':int(image.max())}
                if int(image.max())>profile['expected_white_level']:
                    raise RuntimeError('TIFF export exceeds expected ADC range; export is not qualified.')
                if args.baseline:
                    from flakefinder.imaging import load_frame
                    baseline=load_frame(args.baseline)
                    if max(abs(x-y) for x,y in zip(origin,[baseline.position.x,baseline.position.y,baseline.position.z]))>.3:
                        raise RuntimeError('Baseline belongs to a different stationary position.')
                    if baseline.rgb.shape!=image.shape or baseline.rgb.dtype!=image.dtype:
                        raise RuntimeError('Baseline shape/dtype differs.')
                    actual=np.median(image.reshape(-1,3),axis=0);reference=np.median(baseline.rgb.reshape(-1,3),axis=0)
                    reply['comparison']['baseline']=str(args.baseline.resolve())
                    reply['comparison']['median_rgb_ratio_to_baseline']=(actual/np.maximum(reference,1)).tolist()
                    reply['comparison']['qualification']='Descriptive stationary comparison only; color/pixel equivalence is not automatically accepted.'
            results.append(reply)
            write_once(folder/f'ack-{sequence:04d}.json',{'session_id':session_id,'request_id':request_id})
        wait_json('done.json');future.result(timeout=max(.1,deadline-time.time()))
        write_once(folder/'benchmark.json',{'state':'complete','total_session_s':time.perf_counter()-started,
            'profile':profile,'inventory':inventory,'frames':results,
            'qualification':'Isolated experimental path. CZI is primary; optional TIFF export and camera-profile equivalence require review. No production backend change.'})
        print(folder/'benchmark.json')
    except BaseException as error:
        if folder is not None:
            (folder/'STOP').write_text('cancel',encoding='utf-8')
            write_once(folder/'client-error.json',{'error':str(error),'frames_completed':len(results)})
        raise
    finally:
        try:
            if owns_loaded_job:
                if future is not None and not future.done():
                    if folder is not None:(folder/'STOP').write_text('cancel',encoding='utf-8')
                    try:future.result(timeout=2)
                    except Exception:rpc(workflow,'StopJob',wp.WorkflowServiceStopJobRequest(),timeout=5)
                loaded=rpc(workflow,'IsJobTemplateLoaded',wp.WorkflowServiceIsJobTemplateLoadedRequest(),timeout=5)
                if loaded.is_job_template_loaded:
                    rpc(workflow,'UnloadJobTemplate',wp.WorkflowServiceUnloadJobTemplateRequest(),timeout=5)
        finally:
            if future is not None and not future.done():future.cancel()
            if channel is not None:channel.close()
            lease.close()


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('mode',choices=['inventory','benchmark'])
    parser.add_argument('--execute',action='store_true',help='Explicitly run a registered prototype while the microscope is idle and stationary.')
    parser.add_argument('--profile',type=Path,default=HERE/'profile.example.json')
    parser.add_argument('--count',type=int,choices=range(1,13),default=5)
    parser.add_argument('--comparison-tiff',action='store_true')
    parser.add_argument('--baseline',type=Path,help='Same-position TIFF+JSON from the proven camera backend.')
    run(parser.parse_args())
