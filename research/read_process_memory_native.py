"""Read-only process memory inventory without WMI or hardware access."""
import ctypes as c
from ctypes import wintypes as w
import json
import os

k = c.WinDLL("kernel32", use_last_error=True)
p = c.WinDLL("psapi", use_last_error=True)
k.OpenProcess.argtypes = [w.DWORD, w.BOOL, w.DWORD]
k.OpenProcess.restype = w.HANDLE
k.CloseHandle.argtypes = [w.HANDLE]
k.QueryFullProcessImageNameW.argtypes = [w.HANDLE, w.DWORD, w.LPWSTR, c.POINTER(w.DWORD)]
p.GetProcessMemoryInfo.argtypes = [w.HANDLE, c.c_void_p, w.DWORD]
class Memory(c.Structure):
    _fields_ = [("cb",w.DWORD),("faults",w.DWORD)] + [(s,c.c_size_t) for s in
      ("peak_ws","ws","peak_paged","paged","peak_nonpaged","nonpaged","pagefile","peak_pagefile","private")]
ids = (w.DWORD * 8192)()
size = w.DWORD()
if not p.EnumProcesses(ids, c.sizeof(ids), c.byref(size)): raise OSError("EnumProcesses failed")
rows = []
for pid in ids[:size.value // c.sizeof(w.DWORD)]:
    if not pid: continue
    handle = k.OpenProcess(0x1010, False, pid)
    if not handle: continue
    try:
        buf = c.create_unicode_buffer(32768); count = w.DWORD(len(buf)); mem = Memory(); mem.cb=c.sizeof(mem)
        if not k.QueryFullProcessImageNameW(handle,0,buf,c.byref(count)): continue
        if not p.GetProcessMemoryInfo(handle,c.byref(mem),mem.cb): continue
        if mem.ws >= 80*1024**2 or pid in (8492,26460):
            rows.append({"pid":pid,"name":os.path.basename(buf.value),"path":buf.value,
                         "working_set_bytes":mem.ws,"private_bytes":mem.private})
    finally: k.CloseHandle(handle)
print(json.dumps(sorted(rows,key=lambda row:row["working_set_bytes"],reverse=True)))
