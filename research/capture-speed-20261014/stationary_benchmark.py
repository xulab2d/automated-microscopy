"""Explicit bounded camera-only experiment; never arms movement or edits holds."""
from pathlib import Path
from dataclasses import asdict
import argparse,hashlib,json,os,secrets,sys,time,uuid,xml.etree.ElementTree as ET
ROOT=Path(__file__).resolve().parents[2];RELEASE=ROOT/'research/software-next-20261009'
sys.path.insert(0,str(RELEASE))
from flakefinder.storage import project_root,operator_holds,assert_acquisition_allowed
from flakefinder.model import atomic_json
from flakefinder.zen import ZenMicroscope
from flakefinder.imaging import save_frame
from flakefinder.analysis_lifecycle import process_alive


def run(count,inventory_path,locked_reviews=False,paired=False):
 if not 1<=count<=6:raise ValueError('Only one to six stationary captures permitted')
 project=project_root();run_id='capture-benchmark-'+time.strftime('%Y%m%d-%H%M%S')+'-'+uuid.uuid4().hex[:8]
 out=project/'data'/run_id;out.mkdir()
 inventory=json.loads(inventory_path.read_text())
 saved_camera=Path.home()/'Documents/Carl Zeiss/ZENCore/Documents/Camera Settings/Axiocam305c_S1329/AfterShutdown.xml'
 settings=ET.parse(saved_camera).getroot()
 values={e.tag:e.text for e in settings.iter()}
 if float(values['ExposureTime'])!=float(inventory['exposure_ms']):raise ValueError('Saved/restored camera exposure differs from fresh inventory')
 resources={'ff_exposure_ms':float(inventory['exposure_ms']),'ff_white_balance':values['WhiteBalance']}
 request={'id':run_id,'purpose':'User-authorized stationary empty-stage capture latency benchmark',
  'authorization':'Explicit October 14 user instruction to experiment with capture speed, no chips on stage',
  'policy':{'movement':False,'change_objective':False,'change_illumination':False,
    'camera_resources':resources,'profile_evidence':{'live_inventory':str(inventory_path),
      'live_inventory_sha256':hashlib.sha256(inventory_path.read_bytes()).hexdigest(),
      'white_balance_source':'Saved camera shutdown profile restored by fresh ZEN startup; live parameter enumeration unavailable',
      'saved_camera_sha256':hashlib.sha256(saved_camera.read_bytes()).hexdigest()},
    'capture_limit':count,'duration_s':300,'locked_review_hashes':locked_reviews,'paired_cache':paired},
  'source_sha256':{str(p):hashlib.sha256(p.read_bytes()).hexdigest() for p in [Path(__file__),RELEASE/'flakefinder/zen.py',RELEASE/'flakefinder/vendor_lifecycle.py',RELEASE/'flakefinder/locked_hashes.py']}}
 atomic_json(out/'scan-request.json',request);pin=hashlib.sha256((out/'scan-request.json').read_bytes()).hexdigest()
 nonce=secrets.token_hex(24);now=time.time()
 auth={'schema':1,'run_id':run_id,'request_sha256':pin,'operator_start':True,'nonce':nonce,
  'issued_at':now,'expires_at':now+300,'holds':[{k:h[k] for k in ('path','sha256')} for h in operator_holds()]}
 atomic_json(out/'acquisition-authorization.json',auth)
 atomic_json(out/'worker-start.json',{'pid':os.getpid(),'nonce':nonce,'request_sha256':pin})
 os.environ['FLAKEFINDER_RUN_AUTHORIZATION']=str(out/'acquisition-authorization.json')
 cache=None
 result={'run_id':run_id,'pid':os.getpid(),'state':'running','hardware_closed':False,'frames':[]};scope=None
 atomic_json(out/'scan-status.json',result);print('EXPERIMENT '+str(out),flush=True)
 try:
  current=json.loads((project/'scan-control/current.json').read_text())['id']
  prior=json.loads((project/'data'/current/'scan-status.json').read_text())
  if not prior.get('hardware_closed') or process_alive(prior.get('pid')):raise RuntimeError('Prior acquisition not confirmed closed/absent')
  scope=ZenMicroscope(bindings_dir=ROOT/'generated',stage_api_version='v2',timeout_s=30,
   job_template='FlakeFinder Quantitative Camera',job_output_dir=project/'data/zen-captures',keep_vendor_results=False,
   camera_resources=resources)
  scope.lease.acquire();original_check=scope.check_cancel
  if locked_reviews or paired:
   from flakefinder.locked_hashes import LockedHashes
   cache=LockedHashes();scope._vendor_review_hashes=cache
  def check():
   original_check();assert_acquisition_allowed()
   if (out/'STOP').exists():raise RuntimeError('Independent experiment STOP')
  scope.check_cancel=check
  pb,wf=scope._service('zen_api.workflows.v3beta.workflow_service','WorkflowService')
  if scope._rpc(wf,'IsJobRunning',pb.WorkflowServiceIsJobRunningRequest()).is_job_running:raise RuntimeError('Existing vendor job running')
  if scope._rpc(wf,'IsJobTemplateLoaded',pb.WorkflowServiceIsJobTemplateLoadedRequest()).is_job_template_loaded:raise RuntimeError('Existing vendor job loaded')
  mtb=scope.hardware_controls();optics=mtb.verify_optics();origin=scope.position();lamp=mtb.lamp_percent()
  scope.expected_objective_position=optics['position']
  result['before']={'position':asdict(origin),'objective':optics,'illumination_percent':lamp}
  atomic_json(out/'scan-status.json',result)
  # Instrument existing calls without replacing their behavior or acceptance.
  phases={};rpc_records=[];original_rpc=scope._rpc
  def rpc(stub,name,*args,**kwargs):
   start=time.perf_counter()
   try:return original_rpc(stub,name,*args,**kwargs)
   finally:rpc_records.append({'name':name,'seconds':time.perf_counter()-start})
  scope._rpc=rpc
  for method in ('_check_pending_vendor_output','_check_capture_storage','_capture_memory_headroom'):
   original=getattr(scope,method)
   def instrument(original=original,method=method):
    start=time.perf_counter()
    try:return original()
    finally:phases[method]=time.perf_counter()-start
   setattr(scope,method,instrument)
  identifiers=set();hashes=set()
  for i in range(count):
   if paired:scope._vendor_review_hashes=cache if i%2 else None
   for source,expected in request['source_sha256'].items():
    if hashlib.sha256(Path(source).read_bytes()).hexdigest()!=expected:raise RuntimeError('Experiment source changed')
   check();phases.clear();rpc_records.clear();start=time.perf_counter();frame=scope.capture();elapsed=time.perf_counter()-start
   name=f'frame_{i:03d}';save_frame(out/'frames',name,frame)
   receipt=scope.ack_persisted_frame(frame,out/'frames'/(name+'.tiff'))
   capture_to_ack=time.perf_counter()-start
   pixel_hash=hashlib.sha256(frame.rgb.tobytes()).hexdigest();identifier=frame.metadata['experiment_id']
   if identifier in identifiers:raise RuntimeError('Experiment ID repeated; freshness unproved')
   identifiers.add(identifier);hashes.add(pixel_hash)
   if max(abs(getattr(frame.position,k)-getattr(origin,k)) for k in ('x','y','z'))>.5:raise RuntimeError('Stationary coordinates changed')
   if mtb.objectives()['position']!=optics['position'] or mtb.lamp_percent()!=lamp:raise RuntimeError('Optics changed')
   row={'index':i,'locked_review_hashes':getattr(scope,'_vendor_review_hashes',None) is not None,'capture_seconds':elapsed,'capture_to_managed_ack_s':capture_to_ack,'timings':dict(frame.metadata['acquisition_timing_s']),
    'guards':dict(phases),'rpc':list(rpc_records),'experiment_id':identifier,'pixel_sha256':pixel_hash,
    'position':asdict(frame.position),'shape':list(frame.rgb.shape),'dtype':str(frame.rgb.dtype),
    'pixel_size_um':list(frame.pixel_size_um),'max_adc':int(frame.rgb.max()),'retention':receipt}
   result['frames'].append(row);atomic_json(out/'scan-status.json',result)
   print(json.dumps({'frame':i,'locked_reviews':row['locked_review_hashes'],'capture_seconds':elapsed,'capture_to_managed_ack_s':capture_to_ack,'timings':row['timings'],'guards':row['guards']}),flush=True)
  result['after']={'position':asdict(scope.position()),'objective':mtb.objectives(),'illumination_percent':mtb.lamp_percent()}
  result.update(state='complete',unique_experiment_ids=len(identifiers),unique_pixel_hashes=len(hashes),
   qualification='Stationary empty-stage timing only; not thin-flake image-quality validation')
 except BaseException as exc:
  result.update(state='failed',error=str(exc),error_type=type(exc).__name__)
  print('FAILED '+str(exc),flush=True)
 finally:
  if scope:scope.close()
  if cache:cache.close()
  result.update(hardware_closed=True,phase='finished',finished_at=time.time());atomic_json(out/'scan-status.json',result)
  print('CLOSED '+str(out),flush=True)
 return result


if __name__=='__main__':
 p=argparse.ArgumentParser();p.add_argument('--count',type=int,default=3);p.add_argument('--inventory',type=Path,required=True);p.add_argument('--locked-reviews',action='store_true');p.add_argument('--paired-review-cache',action='store_true')
 args=p.parse_args();result=run(args.count,args.inventory,args.locked_reviews,args.paired_review_cache);raise SystemExit(result['state']!='complete')
