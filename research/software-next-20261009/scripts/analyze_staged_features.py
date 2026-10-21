"""Single-process, resumable offline feature analysis; never connects hardware."""
from pathlib import Path
import argparse,ctypes,json,os,sys,time

# Set native library budgets before importing numpy/OpenCV/scipy.
for name in ('OMP_NUM_THREADS','OPENBLAS_NUM_THREADS','MKL_NUM_THREADS','NUMEXPR_NUM_THREADS'):
    os.environ[name]='1'
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
import cv2
cv2.setNumThreads(1)
if os.name=='nt':
    try:ctypes.windll.kernel32.SetPriorityClass(ctypes.windll.kernel32.GetCurrentProcess(),0x4000)
    except (AttributeError,OSError):pass
from flakefinder.staged_features import StagedFeatureOptions,analyze_staged_scan
from flakefinder.model import atomic_json,Cancelled


class WorkerLock:
    def __init__(self,path):self.path=path;self.stream=None
    def __enter__(self):
        self.path.parent.mkdir(parents=True,exist_ok=True);self.stream=self.path.open('a+b')
        self.stream.seek(0,2)
        if self.stream.tell()==0:self.stream.write(b'0');self.stream.flush()
        self.stream.seek(0)
        try:
            if os.name=='nt':
                import msvcrt
                msvcrt.locking(self.stream.fileno(),msvcrt.LK_NBLCK,1)
            else:
                import fcntl
                fcntl.flock(self.stream,fcntl.LOCK_EX|fcntl.LOCK_NB)
        except OSError:
            self.stream.close();raise RuntimeError('An offline analysis worker already owns this folder.')
        return self
    def __exit__(self,*exc):
        if self.stream:
            self.stream.seek(0)
            if os.name=='nt':
                import msvcrt
                msvcrt.locking(self.stream.fileno(),msvcrt.LK_UNLCK,1)
            self.stream.close()


def main():
    p=argparse.ArgumentParser(description=__doc__)
    location=p.add_mutually_exclusive_group(required=True)
    location.add_argument('--scan',type=Path);location.add_argument('--campaign',type=Path)
    p.add_argument('--tiles',nargs='+',help='Representative tile numbers or full tile identifiers, only with --scan')
    p.add_argument('--max-new-tiles',type=int);p.add_argument('--options',type=Path)
    p.add_argument('--watch',action='store_true',help='Wait for newly completed chips; ends when campaign reaches a terminal state')
    p.add_argument('--poll-seconds',type=float,default=15)
    args=p.parse_args()
    if args.tiles and not args.scan:p.error('--tiles requires --scan')
    if args.watch and not args.campaign:p.error('--watch requires --campaign')
    if args.max_new_tiles is not None and args.max_new_tiles<1:p.error('--max-new-tiles must be positive')
    if args.poll_seconds<1:p.error('--poll-seconds must be at least1')
    root=(args.scan or args.campaign).resolve();options=StagedFeatureOptions(**json.loads(args.options.read_text(encoding='utf-8'))) if args.options else StagedFeatureOptions()
    from flakefinder.storage import resolve_output_path, WORKSPACE
    output_root=resolve_output_path(root)
    output_root.mkdir(parents=True,exist_ok=True)
    tile_ids={f'tile_{int(v):06d}' if str(v).isdigit() else v for v in args.tiles} if args.tiles else None
    def check():
        if (root/'ANALYSIS_STOP').exists() or (output_root/'ANALYSIS_STOP').exists():raise Cancelled('Offline analysis stop requested.')
    def progress(key,new,total,tile):
        check();print(json.dumps({'tile':key,'new_fields':new,'total_fields':total,
            'proposals':tile.get('proposal_count'),'selected':tile.get('selected_count'),'seconds':tile.get('analysis_seconds')}),flush=True)
    import hashlib
    lock_id=hashlib.sha256(str(output_root).encode()).hexdigest()[:20]
    with WorkerLock(WORKSPACE/'data/locks'/('analysis-'+lock_id+'.lock')):
        reports={};last_signatures={}
        atomic_json(output_root/'analysis-worker.json',{'state':'running','pid':os.getpid(),'started_at':time.time(),'cpu_threads':1,'hardware_access':False})
        try:
            while True:
                check();folders=[root] if args.scan else sorted(path.parent for path in root.glob('chip*/manifest.json'))
                for folder in folders:
                    manifest=json.loads((folder/'manifest.json').read_text(encoding='utf-8'))
                    if args.campaign and not manifest.get('planned_area_acquired'):continue
                    signature=(manifest.get('fingerprint'),len(manifest.get('completed',{})))
                    if args.campaign and last_signatures.get(str(folder))==signature:continue
                    summary=analyze_staged_scan(folder,options=options,tile_ids=tile_ids,max_new_tiles=args.max_new_tiles,callback=progress,check_cancel=check)
                    reports[folder.name]={k:summary.get(k) for k in ('analysis_state','completed_tiles','total_tiles','raw_regions','candidate_count','thin_screen_count')}
                    last_signatures[str(folder)]=signature
                    atomic_json(output_root/'analysis-worker.json',{'state':'running','pid':os.getpid(),'updated_at':time.time(),'cpu_threads':1,'hardware_access':False,'chips':reports})
                if not args.watch:break
                batch=json.loads((root/'batch.json').read_text(encoding='utf-8'))
                if batch.get('state') in ('search_sampling_complete','chip_budget_reached','time_budget_reached','storage_budget_reached','stopped','failed'):break
                time.sleep(args.poll_seconds)
            atomic_json(output_root/'analysis-worker.json',{'state':'complete','pid':os.getpid(),'updated_at':time.time(),'chips':reports,'hardware_access':False})
            print(json.dumps({'state':'complete','chips':reports}),flush=True)
        except Cancelled as exc:
            atomic_json(output_root/'analysis-worker.json',{'state':'stopped','pid':os.getpid(),'updated_at':time.time(),'error':str(exc),'chips':reports,'hardware_access':False})
        except BaseException as exc:
            atomic_json(output_root/'analysis-worker.json',{'state':'failed','pid':os.getpid(),'updated_at':time.time(),'error':str(exc),'chips':reports,'hardware_access':False})
            raise


if __name__=='__main__':main()
