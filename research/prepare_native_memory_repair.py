from pathlib import Path
import json, hashlib, shutil, ctypes
from ctypes import wintypes as W
from datetime import datetime, timezone
src=Path(__file__).parent/'software-next-20261009'
project=Path(r'G:\Shared drives\Xu Lab - Microscopy and Instruments\Automated Microscopy')
run=project/'data'/json.loads((project/'scan-control/current.json').read_text())['id']
assert run.name=='scan-20261011-133357-4a1271ad'
status=json.loads((run/'scan-status.json').read_text())
assert status['hardware_closed'] and status['state']=='failed' and status['error_type']=='MemoryHeadroomError'
k=ctypes.WinDLL('kernel32',use_last_error=True)
k.OpenProcess.argtypes=[W.DWORD,W.BOOL,W.DWORD];k.OpenProcess.restype=W.HANDLE
k.GetExitCodeProcess.argtypes=[W.HANDLE,ctypes.POINTER(W.DWORD)];k.CloseHandle.argtypes=[W.HANDLE]
exits={}
for pid in (29716,25204,17636,19692):
    h=k.OpenProcess(0x1000,False,pid)
    if not h:
        assert ctypes.get_last_error()==87,(pid,ctypes.get_last_error())
        exits[str(pid)]='absent'
    else:
        code=W.DWORD()
        try:assert k.GetExitCodeProcess(h,ctypes.byref(code)) and code.value!=259,(pid,code.value)
        finally:k.CloseHandle(h)
        exits[str(pid)]=code.value
out=project/'research/session-20261011-native-memory';out.mkdir(exist_ok=True)
names=['scripts/watch_scan_memory.py','flakefinder/memory_cleanup.py','flakefinder/scan_continuation.py','flakefinder/focus_checkpoint.py','flakefinder/sparse_acquisition.py','flakefinder/autofocus.py']
hashes={}
for name in names:
    dest=out/'source-before'/name;dest.parent.mkdir(parents=True,exist_ok=True)
    if not dest.exists():shutil.copy2(src/name,dest)
    hashes[name]=hashlib.sha256(dest.read_bytes()).hexdigest()
report={'at':datetime.now(timezone.utc).isoformat(),'state':'repairing_offline','prior_run':run.name,
 'exited_processes':exits,'hardware_closed':True,'source_before_sha256':hashes,
 'error':status['error'],'plan':'Native identity-checked RAM cleanup without transient PowerShell process; preserve interrupted focus confirmation and imported geometric evidence.',
 'capture_ram_floor_bytes':1610612736,'chip_outlines':0,'raster_tiles':0}
(out/'repair-report.json').write_text(json.dumps(report,indent=2))
(run/'supervision/repair-status.json').write_text(json.dumps(report,indent=2))
print(json.dumps(report))
