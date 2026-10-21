from pathlib import Path
import sys,json,hashlib,shutil,ctypes
from ctypes import wintypes
from datetime import datetime,timezone
src=Path(__file__).parent/'software-next-20261009'
project=Path(r'G:\Shared drives\Xu Lab - Microscopy and Instruments\Automated Microscopy')
run=project/'data'/json.loads((project/'scan-control/current.json').read_text())['id']
assert run.name=='scan-20261011-130111-bf685ad5'
status=json.loads((run/'scan-status.json').read_text())
assert status['hardware_closed'] and status['state']=='failed' and status['error_type']=='MemoryHeadroomError'
k=ctypes.WinDLL('kernel32',use_last_error=True)
k.OpenProcess.argtypes=[wintypes.DWORD,wintypes.BOOL,wintypes.DWORD];k.OpenProcess.restype=wintypes.HANDLE
k.GetExitCodeProcess.argtypes=[wintypes.HANDLE,ctypes.POINTER(wintypes.DWORD)]
k.CloseHandle.argtypes=[wintypes.HANDLE]
exits={}
for pid in (8492,32252,26460,22432):
    h=k.OpenProcess(0x1000,False,pid)
    if not h:
        assert ctypes.get_last_error()==87,(pid,ctypes.get_last_error())
        exits[str(pid)]='absent'
    else:
        code=wintypes.DWORD()
        try:assert k.GetExitCodeProcess(h,ctypes.byref(code)) and code.value!=259,(pid,code.value)
        finally:k.CloseHandle(h)
        exits[str(pid)]=code.value
out=project/'research/session-20261011-progress-continuation';out.mkdir(exist_ok=True)
files=['flakefinder/scan_continuation.py','flakefinder/focus_checkpoint.py','flakefinder/sparse_acquisition.py','flakefinder/chip_campaign.py']
hashes={}
for name in files:
    path=src/name;dest=out/'source-before'/name;dest.parent.mkdir(parents=True,exist_ok=True)
    if not dest.exists():shutil.copy2(path,dest)
    hashes[name]=hashlib.sha256(dest.read_bytes()).hexdigest()
report={'at':datetime.now(timezone.utc).isoformat(),'state':'repairing_offline','prior_run':run.name,'exited_processes':exits,
 'hardware_closed':True,'error':status['error'],'source_before_sha256':hashes,
 'actual_coverage':{'completed_chip_outlines':0,'raster_tiles':0,'candidate002_accepted_edge_observations':3},
 'repair':'Reuse pinned geometric probes and interrupted local focus bracket in verified same-placement continuation; preserve original deadline and capture RAM floor.'}
(out/'repair-report.json').write_text(json.dumps(report,indent=2))
(run/'supervision/repair-status.json').write_text(json.dumps(report,indent=2))
print(json.dumps(report))
