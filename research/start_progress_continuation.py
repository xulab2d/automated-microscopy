from pathlib import Path
import sys,json,hashlib,ctypes,runpy
from datetime import datetime,timezone
workspace=Path(__file__).resolve().parents[1];src=workspace/'research/software-next-20261009'
project=Path(r'G:\Shared drives\Xu Lab - Microscopy and Instruments\Automated Microscopy')
report_root=project/'research/session-20261011-progress-continuation';report=json.loads((report_root/'repair-report.json').read_text())
assert report['state']=='validated_offline'
for n,h in report['source_after_sha256'].items():assert hashlib.sha256((src/n).read_bytes()).hexdigest()==h,n
class Memory(ctypes.Structure):
    _fields_=[('length',ctypes.c_ulong),('load',ctypes.c_ulong)]+[(n,ctypes.c_ulonglong) for n in ('total','available','page_total','page_available','virtual_total','virtual_available','extended')]
m=Memory();m.length=ctypes.sizeof(m);assert ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(m))
assert m.available>=2*1024**3,('Need startup headroom above2GiB',m.available)
ready={'at':datetime.now(timezone.utc).isoformat(),'api_mode':'running','hardware_initialization':'CompletedSuccessfully',
       'application_ready':True,'api_control_allowed':True,'observed_via':'Computer Use ZEN window788264 API console; no UI change',
       'prelaunch_available_bytes':m.available}
(report_root/'api-ready-before-continuation.json').write_text(json.dumps(ready,indent=2))
sys.argv=['resume_adaptive_focus.py','--project',str(project),'--prior','scan-20261011-130111-bf685ad5','--start']
runpy.run_path(str(src/'scripts/resume_adaptive_focus.py'),run_name='__main__')
