"""Low-memory file/process-only snapshot for the authorized microscopy project."""
import ctypes
from ctypes import wintypes as W
from datetime import datetime,timezone
import hashlib,importlib.util,json,os,sys
from pathlib import Path

PROJECT=Path(r'G:\Shared drives\Xu Lab - Microscopy and Instruments\Automated Microscopy')
SOURCE=Path(__file__).parent/'software-next-20261009'
spec=importlib.util.spec_from_file_location('watcher',SOURCE/'scripts/watch_scan_memory.py')
watcher=importlib.util.module_from_spec(spec);spec.loader.exec_module(watcher)
read=watcher.read
def optional(path):return read(path) if path.is_file() else None
def pick(value,*keys):return {k:value.get(k) for k in keys} if value else None
def rss(pid=None):
    class Counters(ctypes.Structure):
        _fields_=[('cb',W.DWORD),('faults',W.DWORD),*[(n,ctypes.c_size_t) for n in
            ('peak','working','paged_peak','paged','nonpaged_peak','nonpaged','pagefile','pagefile_peak')]]
    k=ctypes.WinDLL('kernel32');k.GetCurrentProcess.restype=W.HANDLE
    k.OpenProcess.argtypes=[W.DWORD,W.BOOL,W.DWORD];k.OpenProcess.restype=W.HANDLE
    k.CloseHandle.argtypes=[W.HANDLE]
    ps=ctypes.WinDLL('psapi');ps.GetProcessMemoryInfo.argtypes=[W.HANDLE,ctypes.c_void_p,W.DWORD]
    c=Counters();c.cb=ctypes.sizeof(c)
    handle=k.GetCurrentProcess() if pid is None else k.OpenProcess(0x410,False,pid)
    if not handle:return None
    try:
        if not ps.GetProcessMemoryInfo(handle,ctypes.byref(c),c.cb):return None
        return c.working
    finally:
        if pid is not None:k.CloseHandle(handle)

run=PROJECT/'data'/read(PROJECT/'scan-control/current.json')['id'];status=read(run/'scan-status.json')
request=read(run/'scan-request.json');guard=optional(run/'memory-guard/status.json');host=watcher.Native()
discoveries=[]
for path in sorted(run.glob('discovery/candidate*/discovery.json')):
    d=read(path);attempts=[]
    for a in d.get('edge_focus_attempts',[]):
        f=path.parent/a['folder'];r=optional(f/'recovery.json');result=optional(f/'focus-result.json')
        attempts.append({'state':a['state'],'result':pick(result,'success','outcome','z_um','score','reason'),
            'samples':len((r or {}).get('samples',[])),'confirmation':(r or {}).get('confirmation'),
            'reuse':optional(f/'focus-reuse.json'),'partial_reuse':optional(f/'partial-focus-reuse.json'),
            'partial_refinement':pick(optional(f/'partial-refinement/quick-focus.json'),'state','quick_samples','confirmation')})
        attempts[-1]['pending_confirmation']=pick(optional(f/'pending-confirmation/quick-focus.json'),'state','confirmation','result','error')
        attempts[-1]['pending_reuse']=pick(optional(f/'pending-focus-reuse.json'),'source_run','source_quick_fit','accepted','fresh_result')
    discoveries.append({'candidate':path.parent.name,'state':d['state'],'records':len(d['records']),
        'imported_records':len(d.get('imported_records',[])),
        'accepted_edges':sum(bool(r.get('edge_validation',{}).get('accepted')) for r in d['records']+d.get('imported_records',[])),
        'rays':d.get('rays'),'outline':pick(d.get('estimated_outline'),'status','reason'),
        'budget':d.get('operation_budget'),'elapsed':datetime.now(timezone.utc).timestamp()-d['started_at'],'focus':attempts})
batch=optional(run/'batch.json');manifests=[]
for path in run.rglob('manifest.json'):
    m=read(path);manifests.append({'path':str(path.relative_to(run)),'state':m.get('state'),'tiles':len(m.get('completed',{}))})
snapshot={'at':datetime.now(timezone.utc).isoformat(),'run_id':run.name,
    'request_sha256':hashlib.sha256((run/'scan-request.json').read_bytes()).hexdigest(),
    'lineage':pick(request.get('continuation'),'prior_run_id','prior_coordinate_frame_id','deadline_at'),
    'instruction':pick(optional(run/'session-instruction.json'),'continued_from','expected_chip_count','specimen_moved'),
    'status':pick(status,'state','phase','pid','hardware_closed','error_type','error','updated_at','request_sha256','progress'),
    'heartbeat':optional(run/'heartbeat.json'),'memory':optional(run/'memory-recovery.json'),
    'recovery':pick(optional(run/'recovery-needed.json'),'state','error_type','error','hardware_closed'),
    'guard':pick(guard,'state','guard_pid','worker_start_utc','available_bytes','updated_at','watcher_implementation','actions','renderer_trims','last_error'),
    'guard_events':(optional(run/'memory-guard/events.json') or [])[-2:],
    'batch':pick(batch,'state','phase','current_chip','chip_states','error'),
    'focus_reused':optional(run/'initial-focus-reused.json'),'progress_reused':optional(run/'progress-reused.json'),'discoveries':discoveries,'manifests':manifests,
    'available_bytes_now':host.available(),'snapshot_working_set_bytes':rss()}
if guard:
    snapshot['worker_identity_alive']=host.matches(status['pid'],watcher.start_ticks(guard['worker_start_utc']))
    snapshot['guard_working_set_bytes']=rss(guard['guard_pid'])
if '--record' in sys.argv:
    folder=run/'supervision';folder.mkdir(exist_ok=True)
    watcher.write(folder/'low-memory-snapshot.json',snapshot)
print(json.dumps(snapshot,ensure_ascii=True))
