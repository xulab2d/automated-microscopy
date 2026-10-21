import ctypes as C
from ctypes import wintypes as W
import json, runpy
from pathlib import Path
from datetime import datetime,timezone
class Status(C.Structure):
    _fields_=[('length',W.DWORD),('load',W.DWORD)]+[(n,C.c_ulonglong) for n in
        ('total','available','page_total','page_available','virtual_total','virtual_available','extended')]
s=Status();s.length=C.sizeof(s)
assert C.windll.kernel32.GlobalMemoryStatusEx(C.byref(s))
print(json.dumps({'at':datetime.now(timezone.utc).isoformat(),'physical_total':s.total,'physical_available':s.available,
 'system_commit_limit':s.page_total,'system_commit_available':s.page_available}))
class Performance(C.Structure):
    _fields_=[('cb',W.DWORD)]+[(n,C.c_size_t) for n in ('commit_total','commit_limit','commit_peak','physical_total','physical_available','system_cache','kernel_total','kernel_paged','kernel_nonpaged','page_size')]+[(n,W.DWORD) for n in ('handles','processes','threads')]
p=Performance();p.cb=C.sizeof(p)
assert C.windll.psapi.GetPerformanceInfo(C.byref(p),p.cb)
print(json.dumps({n:getattr(p,n)*p.page_size for n in ('commit_total','system_cache','kernel_total','kernel_paged','kernel_nonpaged')}))
runpy.run_path(str(Path(__file__).with_name('read_process_memory_native.py')))
