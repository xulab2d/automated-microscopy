"""Run measured-focus interior scans from an automatically verified chip layout.

Hardware acquisition has one owner. A single low-thread analysis worker processes
completed chips independently, without delaying the next chip's acquisition.
"""
from pathlib import Path
from concurrent.futures import ProcessPoolExecutor,wait
import argparse,hashlib,json,sys,time,os,subprocess
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from flakefinder.config import connect,arm,load_config,transform_from_config
from flakefinder.model import atomic_json,Cancelled,AcquisitionError
from flakefinder.scan import ScanPlan,run_scan

def batch_fingerprint(config,layout,patch_size_um):
 return hashlib.sha256(json.dumps({'config':config,'layout':layout,'patch_size_um':patch_size_um},sort_keys=True).encode()).hexdigest()

def validate_resume(out,config,layout,patch_size_um):
 state=json.loads((out/'batch.json').read_text())
 expected=batch_fingerprint(config,layout,patch_size_um)
 original=state.get('fingerprint')
 if original is None:
  original=batch_fingerprint(json.loads((out/'configuration.json').read_text()),json.loads((out/'layout.json').read_text()),state['patch_size_um'])
 if original!=expected:raise ValueError('Resume configuration, chip layout or patch size differs from the original batch.')
 if set(state['chip_states'])!={chip['id'] for chip in layout['chips']}:raise ValueError('Resume chip identities differ.')
 state['fingerprint']=expected
 return state

def analyse_one(folder,options,cancel_path):
 os.environ['OMP_NUM_THREADS']='2';os.environ['OPENBLAS_NUM_THREADS']='2'
 import cv2
 cv2.setNumThreads(2)
 from threadpoolctl import threadpool_limits
 from flakefinder.analysis import analyze_scan
 def check_cancel():
  if Path(cancel_path).exists():raise Cancelled('Batch analysis cancellation requested.')
 check_cancel()
 with threadpool_limits(limits=2):
  result=analyze_scan(folder,options,output=Path(folder)/'analysis',check_cancel=check_cancel)
 failures=result.get('failed_tiles',[])
 outcome={'candidate_count':result['candidate_count'],'analysis_folder':str(Path(folder).name+'/analysis'),
         'thin_screen_count':result.get('thin_screen_count'), 'analysis_state':'complete_with_failures' if failures else 'complete'}
 if failures:outcome['analysis_error']=f'{len(failures)} fields failed analysis; inspect the saved analysis report.'
 return outcome

def stop_analysis(pool,pending,cancel_path,timeout=20):
 """Cooperate at analysis boundaries; bound shutdown of our owned worker only."""
 Path(cancel_path).write_text('cancel requested\n',encoding='utf-8')
 for future in pending.values():future.cancel()
 unfinished=[future for future in pending.values() if not future.done()]
 if unfinished:
  _,running=wait(unfinished,timeout=timeout)
  if running:
   # Python 3.12 has no public terminate_workers API. Snapshot owned processes
   # before shutdown; never target a process discovered by executable name.
   for process in list(getattr(pool,'_processes',{}).values()):
    if not process.is_alive():continue
    if os.name=='nt':
     try:
      subprocess.run(['taskkill','/PID',str(process.pid),'/T','/F'],capture_output=True,timeout=5,
                     creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0))
     except (OSError,subprocess.TimeoutExpired):process.terminate()
    else:process.terminate()
    process.join(timeout=3)
    if process.is_alive():process.kill();process.join(timeout=3)
 pool.shutdown(wait=True,cancel_futures=True)

def main():
 p=argparse.ArgumentParser();p.add_argument('--config',required=True);p.add_argument('--layout',required=True)
 p.add_argument('--output',required=True);p.add_argument('--patch-size-um',type=float,default=1800)
 p.add_argument('--resume',action='store_true');a=p.parse_args()
 from flakefinder.storage import resolve_output_path
 cfg=load_config(a.config);layout=json.loads(Path(a.layout).read_text());out=resolve_output_path(a.output)
 if not 400<=a.patch_size_um<=10000:raise ValueError('Patch size must be 400..10000 micrometres.')
 chips=layout['chips']
 if not chips or any(c.get('status')!='substrate_verified' for c in chips):raise ValueError('Scan only raw-image-verified substrate centres.')
 if len({chip['id'] for chip in chips})!=len(chips):raise ValueError('Chip identities must be unique.')
 if out.exists() and any(out.iterdir()) and not a.resume:raise ValueError('Use a new output directory or explicit resume.')
 out.mkdir(parents=True,exist_ok=True)
 if (out/'STOP').exists():raise ValueError('A STOP request exists; clear it deliberately before resuming.')
 state={'schema':1,'state':'running','fingerprint':batch_fingerprint(cfg,layout,a.patch_size_um),'coverage_kind':'initial_chip_interior_pass',
        'coverage_note':'Centred patches; full chip-edge coverage remains to be acquired.',
        'patch_size_um':a.patch_size_um,'layout':str(Path(a.layout).resolve()),'started_at':time.time(),
        'current_chip':None,'phase':'starting','chip_states':{c['id']:{'state':'queued','centre_um':c['centre_um'],
        'completed_tiles':0,'total_tiles':None,'candidate_count':None} for c in chips}}
 if a.resume and (out/'batch.json').exists():state=validate_resume(out,cfg,layout,a.patch_size_um);state['state']='running';state.pop('error',None)
 cancel_path=out/'.ANALYSIS_STOP'
 if a.resume:cancel_path.unlink(missing_ok=True)
 atomic_json(out/'batch.json',state);atomic_json(out/'configuration.json',cfg);atomic_json(out/'layout.json',layout)
 workers=cfg.get('analysis_workers',1)
 if isinstance(workers,bool) or not isinstance(workers,int) or not 1<=workers<=4:raise ValueError('analysis_workers must be an integer from 1 to 4.')
 pending={};s=None;pool=ProcessPoolExecutor(max_workers=workers)
 def collect():
  for key,future in list(pending.items()):
   if not future.done():continue
   try:
    outcome=future.result();state['chip_states'][key].update(outcome)
    if not outcome.get('analysis_error'):state['chip_states'][key].pop('analysis_error',None)
   except Cancelled as exc:state['chip_states'][key].update(analysis_state='stopped',analysis_error=str(exc))
   except Exception as exc:state['chip_states'][key].update(analysis_state='failed',analysis_error=str(exc))
   pending.pop(key)
 def save():collect();state['updated_at']=time.time();atomic_json(out/'batch.json',state)
 def check():
  if s is not None:base_check()
  if (out/'STOP').exists():raise Cancelled('Batch STOP requested.')
 try:
  s=connect(cfg);base_check=s.check_cancel;s.check_cancel=check
  arm(s,cfg);transform=transform_from_config(cfg)
  remaining=chips.copy();ordered=[];position=s.position();xy=[position.x,position.y]
  while remaining:
   chip=min(remaining,key=lambda c:sum((c['centre_um'][i]-xy[i])**2 for i in (0,1)))
   ordered.append(chip);remaining.remove(chip);xy=chip['centre_um']
  for chip in ordered:
   check();key=chip['id'];item=state['chip_states'][key];folder=out/key
   if item['state']=='complete':
    if item.get('analysis_state')!='complete':
     item.update(analysis_state='queued');item.pop('analysis_error',None)
     pending[key]=pool.submit(analyse_one,str(folder),cfg.get('analysis',{'backend':'empirical'}),str(cancel_path))
    continue
   x,y=chip['centre_um'];half=a.patch_size_um/2
   options=dict(cfg.get('interior_scan',{}))
   plan=ScanPlan([x-half,x+half,y-half,y+half],chip_id=key,mode='survey',focus_strategy='surface',**options)
   state.update(current_chip=key,phase='measuring_focus_surface');item.update(state='focusing');item.pop('error',None);save()
   def progress(phase,frame,info):
    check();state['phase']=phase
    if phase=='tile':
     item.update(state='acquiring',completed_tiles=info['index'],total_tiles=info['total'],
                 latest_image=f'{key}/tiles/tile_{info["index"]-1:06d}.jpg')
     print('TILE',key,info,flush=True)
    else:
     item['focus_progress']={k:info[k] for k in ('anchor_index','z_um','search_phase','score') if k in info}
    save()
   try:
    manifest=run_scan(s,plan,transform,folder,context=cfg.get('context'),callback=progress,
                      resume=a.resume and (folder/'manifest.json').exists())
   except AcquisitionError as exc:
    # A failed optical curve is reviewable per chip. Positioning, stream and
    # objective errors still stop the batch, never trigger a blind retry.
    if not str(exc).startswith('Autofocus failed at tile'):raise
    item.update(state='focus_failed',error=str(exc));save();arm(s,cfg);continue
   item.update(state='complete',completed_tiles=len(manifest['completed']),total_tiles=manifest['tile_count'],analysis_state='queued')
   pending[key]=pool.submit(analyse_one,str(folder),cfg.get('analysis',{'backend':'empirical'}),str(cancel_path));save()
  state.update(state='analysing',phase='finishing_analysis',current_chip=None);save()
  s.close();s=None
  while pending:
   if (out/'STOP').exists():raise Cancelled('Batch STOP requested during analysis.')
   collect();save();time.sleep(1)
  state['state']='complete_with_failures' if any(v['state']!='complete' or v.get('analysis_state')!='complete' for v in state['chip_states'].values()) else 'complete'
  state['phase']='interior_pass_complete';save()
 except BaseException as exc:
  terminal='stopped' if isinstance(exc,Cancelled) else 'failed'
  state.update(state=terminal,error=str(exc))
  active=state['chip_states'].get(state.get('current_chip'))
  if active is not None and active.get('state')!='complete':active.update(state=terminal,error=str(exc))
  save()
  if s:s.stop()
  raise
 finally:
  try:
   if s:s.close()
  finally:
   if pending:stop_analysis(pool,pending,cancel_path)
   else:pool.shutdown(wait=True,cancel_futures=True)
   save()

if __name__=='__main__':main()
