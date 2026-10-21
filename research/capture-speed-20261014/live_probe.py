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
 'policy':{'movement':False,'change_objective':False,'change_illumination':False,'capture_limit':0,'duration_s':180},
 'source_sha256':hashlib.sha256(Path(__file__).read_bytes()).hexdigest()}
atomic_json(out/'scan-request.json',request);request_hash=hashlib.sha256((out/'scan-request.json').read_bytes()).hexdigest()
nonce=secrets.token_hex(24);now=time.time()
atomic_json(out/'acquisition-authorization.json',{'schema':1,'run_id':run_id,'request_sha256':request_hash,
 'operator_start':True,'nonce':nonce,'issued_at':now,'expires_at':now+180,
 'holds':[{k:h[k] for k in ('path','sha256')} for h in operator_holds()]})
atomic_json(out/'worker-start.json',{'pid':os.getpid(),'nonce':nonce,'request_sha256':request_hash})
os.environ['FLAKEFINDER_RUN_AUTHORIZATION']=str(out/'acquisition-authorization.json')
scope=None;result={'run_id':run_id,'pid':os.getpid(),'state':'running','hardware_closed':False}
atomic_json(out/'scan-status.json',result);print(str(out),flush=True)
try:
 current=json.loads((project/'scan-control/current.json').read_text())['id']
 prior=json.loads((project/'data'/current/'scan-status.json').read_text())
 if not prior.get('hardware_closed') or process_alive(prior.get('pid')):raise RuntimeError('Previous acquisition owner not confirmed closed/absent')
 scope=ZenMicroscope(bindings_dir=ROOT/'generated',stage_api_version='v2',timeout_s=15,
  job_template='FlakeFinder Quantitative Camera',job_output_dir=project/'data/zen-captures',keep_vendor_results=False)
 scope.lease.acquire()
 pb,wf=scope._service('zen_api.workflows.v3beta.workflow_service','WorkflowService')
 from dataclasses import asdict
 for label,action in [('position',lambda:asdict(scope.position())),
   ('job_running',lambda:scope._rpc(wf,'IsJobRunning',pb.WorkflowServiceIsJobRunningRequest()).is_job_running),
   ('job_loaded',lambda:scope._rpc(wf,'IsJobTemplateLoaded',pb.WorkflowServiceIsJobTemplateLoadedRequest()).is_job_template_loaded),
   ('objective',lambda:scope.hardware_controls().objectives()),
   ('illumination_percent',lambda:scope.hardware_controls().lamp_percent())]:
  try:result[label]=action()
  except Exception as exc:result[label]={'error':str(exc)}
 if '--inventory' in sys.argv:
  if result['job_running'] is not False or result['job_loaded'] is not False:raise RuntimeError('Vendor not idle')
  session=uuid.uuid4().hex;transport=ROOT/'scripts/camera_fastpath/transport';sample=transport/session;sample.mkdir()
  atomic_json(transport/'active.json',{'schema':1,'session_id':session,'mode':'inventory','count':1,
    'deadline_unix_s':time.time()+90,'comparison_tiff':False})
  future=None;loaded=False
  try:
   scope._rpc(wf,'LoadJobTemplate',pb.WorkflowServiceLoadJobTemplateRequest(
    job_template_name='FlakeFinder Request Snap Prototype v1',result_path=str(out/'vendor-inventory-result')));loaded=True
   future=wf.RunJob.future(pb.WorkflowServiceRunJobRequest(),metadata=scope.metadata,timeout=95)
   try:future.result(timeout=100);result['inventory_vendor_completion']='success'
   except Exception as exc:result['inventory_vendor_completion']={'error':str(exc)}
   if (sample/'inventory.json').exists():
    inventory=json.loads((sample/'inventory.json').read_text());atomic_json(out/'inventory.json',inventory)
    result['inventory']={'path':str(sample/'inventory.json'),'camera':inventory['name'],'exposure_ms':inventory['exposure_ms']}
   else:raise RuntimeError('Inventory macro produced no readback')
  finally:
   if future is not None and not future.done():
    (sample/'STOP').write_text('Inventory deadline');future.cancel();scope._rpc(wf,'StopJob',pb.WorkflowServiceStopJobRequest())
   if loaded and scope._rpc(wf,'IsJobTemplateLoaded',pb.WorkflowServiceIsJobTemplateLoadedRequest()).is_job_template_loaded:
    scope._rpc(wf,'UnloadJobTemplate',pb.WorkflowServiceUnloadJobTemplateRequest())
 result['state']='complete';print(json.dumps(result),flush=True)
except BaseException as exc:
 result.update(state='failed',error=str(exc),error_type=type(exc).__name__);print(json.dumps(result),flush=True)
finally:
 if scope:scope.close()
 result.update(hardware_closed=True,phase='finished',finished_at=time.time());atomic_json(out/'scan-status.json',result)
