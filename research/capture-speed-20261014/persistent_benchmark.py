"""Fresh scoped, stationary hardware inventory; no movement or camera setting writes."""
from pathlib import Path
import hashlib,json,os,secrets,sys,time,uuid
ROOT=Path(__file__).resolve().parents[2]
RELEASE=ROOT/'research/software-next-20261009'
sys.path.insert(0,str(RELEASE))
from flakefinder.storage import project_root,operator_holds
from flakefinder.model import atomic_json
from flakefinder.zen import ZenMicroscope
from flakefinder.analysis_lifecycle import process_alive

project=project_root();run_id='capture-benchmark-'+time.strftime('%Y%m%d-%H%M%S')+'-'+uuid.uuid4().hex[:8]
out=project/'data'/run_id;out.mkdir()
request={'id':run_id,'purpose':'User-authorized stationary empty-stage capture timing experiment',
 'authorization':'Explicit October 14 user instruction to experiment with microscope capture speed; no chips on stage.',
 'policy':{'movement':False,'change_objective':False,'change_illumination':False,'capture_limit':4,'duration_s':180},
 'source_sha256':hashlib.sha256(Path(__file__).read_bytes()).hexdigest()}
atomic_json(out/'scan-request.json',request);request_hash=hashlib.sha256((out/'scan-request.json').read_bytes()).hexdigest()
nonce=secrets.token_hex(24);now=time.time()
atomic_json(out/'acquisition-authorization.json',{'schema':1,'run_id':run_id,'request_sha256':request_hash,
 'operator_start':True,'nonce':nonce,'issued_at':now,'expires_at':now+180,
 'holds':[{k:h[k] for k in ('path','sha256')} for h in operator_holds()]})
atomic_json(out/'worker-start.json',{'pid':os.getpid(),'nonce':nonce,'request_sha256':request_hash})
os.environ['FLAKEFINDER_RUN_AUTHORIZATION']=str(out/'acquisition-authorization.json')

from dataclasses import asdict
from flakefinder.storage import assert_acquisition_allowed
import shutil
import czifile,numpy as np,xml.etree.ElementTree as ET
from flakefinder.model import Frame,Position
from flakefinder.imaging import save_frame,load_frame
scope=None;future=None;loaded=False;sample=None
result={'run_id':run_id,'pid':os.getpid(),'state':'running','hardware_closed':False,'frames':[],
 'qualification':'Experimental latency/freshness only. WB, auto exposure, auto WB and HDR live parameter bindings unavailable; not production qualified.'}
atomic_json(out/'scan-status.json',result);print(str(out),flush=True)
try:
 current=json.loads((project/'scan-control/current.json').read_text())['id']
 prior=json.loads((project/'data'/current/'scan-status.json').read_text())
 if not prior.get('hardware_closed') or process_alive(prior.get('pid')):raise RuntimeError('Old owner not closed')
 scope=ZenMicroscope(bindings_dir=ROOT/'generated',stage_api_version='v2',timeout_s=15,job_output_dir=project/'data/zen-captures')
 scope.lease.acquire();pb,wf=scope._service('zen_api.workflows.v3beta.workflow_service','WorkflowService')
 for method,attr in [('IsJobRunning','is_job_running'),('IsJobTemplateLoaded','is_job_template_loaded')]:
  if getattr(scope._rpc(wf,method,getattr(pb,'WorkflowService'+method+'Request')()),attr):raise RuntimeError('Existing job')
 origin=asdict(scope.position());mtb=scope.hardware_controls();optics=mtb.verify_optics();lamp=mtb.lamp_percent()
 result['before']={'position':origin,'objective':optics,'lamp':lamp}
 def check():
  scope.check_cancel();assert_acquisition_allowed()
  if (out/'STOP').exists() or time.time()>=deadline:raise RuntimeError('Experiment STOP/deadline')
 def stationary():
  check();pos=asdict(scope.position())
  if max(abs(pos[k]-origin[k]) for k in ('x','y','z'))>.5:raise RuntimeError('Position changed')
  if mtb.objectives()['position']!=optics['position'] or mtb.lamp_percent()!=lamp:raise RuntimeError('Optics changed')
  return pos
 session=uuid.uuid4().hex;transport=Path(__file__).parent/'persistent/transport';sample=transport/session;sample.mkdir()
 deadline=time.time()+120
 atomic_json(sample/'profile.json',{'enabled':True,'camera_name':'Axiocam 305','exposure_ms':140})
 atomic_json(transport/'active.json',{'session_id':session,'mode':'benchmark','count':4,'deadline_unix_s':deadline,'comparison_tiff':False})
 t=time.perf_counter();scope._rpc(wf,'LoadJobTemplate',pb.WorkflowServiceLoadJobTemplateRequest(job_template_name='FlakeFinder Experimental Stationary Snap 20261014',result_path=str(out/'vendor-result')));loaded=True
 result['load_s']=time.perf_counter()-t
 future=wf.RunJob.future(pb.WorkflowServiceRunJobRequest(),metadata=scope.metadata,timeout=125)
 def wait(name):
  while not (sample/name).exists():
   check()
   if (sample/'error.json').exists():raise RuntimeError((sample/'error.json').read_text())
   if future.done():future.result();raise RuntimeError('Macro ended before '+name)
   time.sleep(.01)
  return json.loads((sample/name).read_text())
 result['inventory']=wait('inventory.json');ready=wait('ready.json')
 if ready['session_id']!=session:raise RuntimeError('Session mismatch')
 result['ready_s']=time.perf_counter()-t
 for sequence in range(1,5):
  stationary();identifier=uuid.uuid4().hex;t=time.perf_counter()
  atomic_json(sample/f'request-{sequence:04d}.json',{'session_id':session,'sequence':sequence,'request_id':identifier,'deadline_unix_s':min(deadline,time.time()+25)})
  reply=wait(f'reply-{sequence:04d}.json');elapsed=time.perf_counter()-t
  if (reply['session_id'],reply['request_id'],reply['sequence'])!=(session,identifier,sequence):raise RuntimeError('Correlation mismatch')
  if reply['czi']!=identifier+'.czi':raise RuntimeError('Wrong CZI')
  path=sample/reply['czi']
  with path.open('rb') as f:
   if f.read(10)!=b'ZISRAWFILE':raise RuntimeError('Not CZI')
  reply.update(request_to_saved_s=elapsed,czi_sha256=hashlib.sha256(path.read_bytes()).hexdigest(),position=stationary())
  decode_started=time.perf_counter()
  with czifile.CziFile(path) as c:
   pixels=c.asarray();xml=c.metadata()
  md=ET.fromstring(xml);cal={e.attrib['Id']:float(e.findtext('Value'))*1e6 for e in md.findall('.//Scaling/Items/Distance')}
  if pixels.dtype!=np.uint16 or pixels.shape!=(2056,2464,3):raise RuntimeError('Unexpected raw CZI pixels')
  frame=Frame(pixels,Position(**origin),(cal['X'],cal['Y']),sequence,metadata={'white_level':4095,'experimental_capture':reply,'czi_metadata_sha256':hashlib.sha256(xml.encode()).hexdigest(),'qualification':result['qualification']})
  reply['decode_s']=time.perf_counter()-decode_started
  save_started=time.perf_counter();name=f'frame_{sequence:03d}';save_frame(out/'frames',name,frame)
  target=out/'frames'/(name+'.tiff')
  for durable in (target,target.with_suffix('.json')):
   with durable.open('rb+') as f:os.fsync(f.fileno())
  saved=load_frame(target)
  if not np.array_equal(saved.rgb,pixels):raise RuntimeError('Managed raw pixel mismatch')
  reply['pixel_sha256']=hashlib.sha256(pixels.tobytes()).hexdigest();reply['managed_tiff_sha256']=hashlib.sha256(target.read_bytes()).hexdigest()
  reply['managed_save_verify_s']=time.perf_counter()-save_started;reply['request_to_managed_verified_s']=time.perf_counter()-t
  result['frames'].append(reply);atomic_json(out/'scan-status.json',result)
  atomic_json(sample/f'ack-{sequence:04d}.json',{'session_id':session,'request_id':identifier})
  print(json.dumps({'sequence':sequence,'request_to_saved_s':elapsed,'snap_s':reply['snap_s'],'czi_save_s':reply['czi_save_s'],'request_to_managed_verified_s':reply['request_to_managed_verified_s'],'save_verify_s':reply['managed_save_verify_s']}),flush=True)
 result['done']=wait('done.json')
 try:future.result(timeout=max(.1,deadline-time.time()));result['vendor_finalization']='success'
 except Exception as exc:result['vendor_finalization']={'error':str(exc)}
 result['after']={'position':stationary(),'objective':mtb.objectives(),'lamp':mtb.lamp_percent()}
 result['state']='experiment_complete'
except BaseException as exc:
 result.update(state='failed',error=str(exc));print('FAILED '+str(exc),flush=True)
finally:
 try:
  if future is not None and not future.done():
   (sample/'STOP').write_text('Experiment closing')
   try:future.result(timeout=2)
   except Exception:scope._rpc(wf,'StopJob',pb.WorkflowServiceStopJobRequest())
  if loaded and scope._rpc(wf,'IsJobTemplateLoaded',pb.WorkflowServiceIsJobTemplateLoadedRequest()).is_job_template_loaded:scope._rpc(wf,'UnloadJobTemplate',pb.WorkflowServiceUnloadJobTemplateRequest())
 finally:
  if scope:scope.close()
  result.update(hardware_closed=True,phase='finished',finished_at=time.time(),transport=str(sample))
  atomic_json(out/'scan-status.json',result)
  if sample:
   shutil.copytree(sample,out/'persistent-artifacts')
   result['artifact_copy']='retained_original_and_managed_copy';atomic_json(out/'scan-status.json',result)
  print('CLOSED '+str(out),flush=True)
