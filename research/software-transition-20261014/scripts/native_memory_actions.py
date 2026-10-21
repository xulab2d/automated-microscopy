"""Small Windows-native implementation of the existing memory action allowlist.

No WMI, PowerShell, hardware access, services or privilege adjustment. Every
action checks the same-user SID, installed path and PID/start on its open handle.
"""
import ctypes as C
from ctypes import wintypes as W
from contextlib import contextmanager
from datetime import datetime, timezone
import json
import ntpath
import os
from pathlib import Path
import re
import time

DOTNET_EPOCH_TICKS=504911232000000000
ALLOWED=('PhoneExperienceHost','WidgetService','Widgets','DellOptimizer.Systray','Dell.UUE.User.SubAgent')

def allowed_image(name,path,programs=r'C:\Program Files'):
    path=ntpath.normpath(path).lower();base=ntpath.normpath(programs).lower()
    if name not in ALLOWED or ntpath.basename(path)!=(name+'.exe').lower():return False
    exact={'DellOptimizer.Systray':r'Dell\DellOptimizer\Console\DellOptimizer.Systray.exe',
           'Dell.UUE.User.SubAgent':r'Dell\PlatformUserSubAgent\Dell.UUE.User.SubAgent.exe'}
    if name in exact:return path==ntpath.join(base,exact[name]).lower()
    package='Microsoft.YourPhone_' if name=='PhoneExperienceHost' else '(?:Microsoft.WidgetsPlatformRuntime_|MicrosoftWindows.Client.WebExperience_)'
    suffix=re.escape(name)+r'\.exe'
    # WidgetService is also installed inside the package's WidgetService folder.
    if name=='WidgetService':suffix=r'(?:WidgetService\\)?'+suffix
    return bool(re.fullmatch(re.escape(base)+r'\\WindowsApps\\'+package+r'[^\\]+\\'+suffix,path,re.I))

def renderer(path,command,programs=r'C:\Program Files'):
    return bool(re.fullmatch(re.escape(ntpath.normpath(programs))+r'\\WindowsApps\\OpenAI\.Codex_[0-9.]+_x64__2p2nqsd0c76g0\\app\\ChatGPT\.exe',ntpath.normpath(path),re.I)
        and re.search(r'(?:^|\s)--type=renderer(?:\s|$)',command))

def expiry(value):
    try:d=datetime.fromisoformat(str(value).replace('Z','+00:00'))
    except ValueError:d=datetime.strptime(str(value),'%m/%d/%Y %H:%M:%S')
    return d.replace(tzinfo=timezone.utc).timestamp() if d.tzinfo is None else d.timestamp()

def same(a,b):return all(a[k]==b[k] for k in ('pid','ticks','path','sid','name'))

class NativeActions:
    def __init__(self):
        self.k=C.WinDLL('kernel32',use_last_error=True);self.p=C.WinDLL('psapi',use_last_error=True)
        self.a=C.WinDLL('advapi32',use_last_error=True);self.n=C.WinDLL('ntdll')
        self.k.OpenProcess.argtypes=[W.DWORD,W.BOOL,W.DWORD];self.k.OpenProcess.restype=W.HANDLE
        self.k.CloseHandle.argtypes=[W.HANDLE];self.k.GetCurrentProcess.restype=W.HANDLE
        self.k.QueryFullProcessImageNameW.argtypes=[W.HANDLE,W.DWORD,W.LPWSTR,C.POINTER(W.DWORD)]
        self.k.GetProcessTimes.argtypes=[W.HANDLE]+[C.POINTER(W.FILETIME)]*4
        self.k.GetExitCodeProcess.argtypes=[W.HANDLE,C.POINTER(W.DWORD)]
        self.k.TerminateProcess.argtypes=[W.HANDLE,W.UINT]
        self.k.LocalFree.argtypes=[C.c_void_p];self.k.LocalFree.restype=C.c_void_p
        self.p.GetProcessMemoryInfo.argtypes=[W.HANDLE,C.c_void_p,W.DWORD]
        self.p.EmptyWorkingSet.argtypes=[W.HANDLE]
        self.a.OpenProcessToken.argtypes=[W.HANDLE,W.DWORD,C.POINTER(W.HANDLE)]
        self.a.GetTokenInformation.argtypes=[W.HANDLE,C.c_int,C.c_void_p,W.DWORD,C.POINTER(W.DWORD)]
        self.a.ConvertSidToStringSidW.argtypes=[C.c_void_p,C.POINTER(W.LPWSTR)]
        self.n.NtQueryInformationProcess.argtypes=[W.HANDLE,W.ULONG,C.c_void_p,W.ULONG,C.POINTER(W.ULONG)]
        self.n.NtQueryInformationProcess.restype=C.c_long
        self.sid=self.owner(self.k.GetCurrentProcess());self.programs=os.environ.get('ProgramFiles',r'C:\Program Files')

    def owner(self,h):
        token=W.HANDLE()
        if not self.a.OpenProcessToken(h,8,C.byref(token)):raise OSError('Cannot verify owner')
        try:
            size=W.DWORD();self.a.GetTokenInformation(token,1,None,0,C.byref(size))
            if not 0<size.value<65536:raise OSError('Invalid token size')
            data=C.create_string_buffer(size.value)
            if not self.a.GetTokenInformation(token,1,data,size,C.byref(size)):raise OSError('Cannot read owner')
            sidptr=C.cast(data,C.POINTER(C.c_void_p))[0];text=W.LPWSTR()
            if not self.a.ConvertSidToStringSidW(sidptr,C.byref(text)):raise OSError('Cannot decode owner')
            try:return text.value
            finally:self.k.LocalFree(C.cast(text,C.c_void_p))
        finally:self.k.CloseHandle(token)

    def identity(self,h,pid):
        code=W.DWORD();times=[W.FILETIME() for _ in range(4)];path=C.create_unicode_buffer(32768);length=W.DWORD(len(path))
        if not self.k.GetExitCodeProcess(h,C.byref(code)) or code.value!=259:raise OSError('Process exited')
        if not self.k.GetProcessTimes(h,*[C.byref(t) for t in times]):raise OSError('Cannot read creation identity')
        if not self.k.QueryFullProcessImageNameW(h,0,path,C.byref(length)):raise OSError('Cannot read image path')
        class Memory(C.Structure):
            _fields_=[('cb',W.DWORD),('faults',W.DWORD)]+[(n,C.c_size_t) for n in
                ('peak_ws','ws','peak_paged','paged','peak_nonpaged','nonpaged','pagefile','peak_pagefile','private')]
        mem=Memory();mem.cb=C.sizeof(mem)
        if not self.p.GetProcessMemoryInfo(h,C.byref(mem),mem.cb):raise OSError('Cannot read resident size')
        return dict(pid=pid,ticks=((times[0].dwHighDateTime<<32)|times[0].dwLowDateTime)+DOTNET_EPOCH_TICKS,
                    path=path.value,name=ntpath.basename(path.value)[:-4],sid=self.owner(h),ws=mem.ws)

    def command(self,h):
        # ProcessCommandLineInformation: unsupported/denied reads fail closed.
        size=W.ULONG();self.n.NtQueryInformationProcess(h,60,None,0,C.byref(size))
        if not 0<size.value<=131072:raise OSError('Command line unavailable')
        data=C.create_string_buffer(size.value)
        if self.n.NtQueryInformationProcess(h,60,data,size,C.byref(size))<0:raise OSError('Command line unreadable')
        class String(C.Structure):_fields_=[('length',W.USHORT),('maximum',W.USHORT),('buffer',C.c_void_p)]
        value=String.from_buffer(data);begin=C.addressof(data)
        if value.length%2 or not begin<=value.buffer<=value.buffer+value.length<=begin+len(data):raise OSError('Invalid command buffer')
        return C.wstring_at(value.buffer,value.length//2)

    def candidates(self):
        ids=(W.DWORD*16384)();size=W.DWORD()
        if not self.p.EnumProcesses(ids,C.sizeof(ids),C.byref(size)):raise OSError('Cannot list processes')
        rows=[]
        for pid in ids[:size.value//4]:
            h=self.k.OpenProcess(0x1000|0x10,False,pid)
            if not h:continue
            try:
                path=C.create_unicode_buffer(32768);length=W.DWORD(len(path))
                if not self.k.QueryFullProcessImageNameW(h,0,path,C.byref(length)):continue
                name=ntpath.basename(path.value)[:-4]
                if name not in (*ALLOWED,'ChatGPT'):continue
                row=self.identity(h,pid)
                if row['sid']!=self.sid:continue
                if allowed_image(name,path.value,self.programs):row['action']='stop'
                elif name=='ChatGPT' and row['ws']>=256*1024**2 and renderer(path.value,self.command(h),self.programs):row['action']='trim'
                else:continue
                rows.append(row)
            except OSError:continue
            finally:self.k.CloseHandle(h)
        return sorted(rows,key=lambda r:r['ws'],reverse=True)

    def act(self,expected,before):
        mode=expected['action'];rights=0x1000|0x10|(1 if mode=='stop' else 0x100|0x400)
        h=self.k.OpenProcess(rights,False,expected['pid'])
        if not h:raise OSError('Cannot open verified process for action')
        try:
            row=self.identity(h,expected['pid'])
            if not same(expected,row) or row['sid']!=self.sid:raise OSError('Identity changed before action')
            if mode=='stop' and not allowed_image(row['name'],row['path'],self.programs):raise OSError('Image not allowlisted')
            if mode=='trim' and not renderer(row['path'],self.command(h),self.programs):raise OSError('Not a renderer')
            if mode not in ('stop','trim'):raise OSError('Invalid action')
            before(row) # durable intent and fresh worker lifecycle before mutation
            if not same(row,self.identity(h,row['pid'])):raise OSError('Process changed')
            if mode=='stop':ok=self.k.TerminateProcess(h,0)
            else:ok=self.p.EmptyWorkingSet(h)
            if not ok:raise OSError(C.get_last_error(),'Native action failed')
            return {**{k:row[k] for k in ('pid','name','path','ticks')},'before_bytes':row['ws'],
                    'state':'stopped' if mode=='stop' else 'trimmed','process_terminated':mode=='stop'}
        finally:self.k.CloseHandle(h)

class Cleanup:
    def __init__(self,run,host,life,read,write,native=None,clock=time.time):
        self.run=Path(run);self.folder=self.run/'memory-guard';self.host=host;self.life=life
        self.read=read;self.write=write;self.native=native or NativeActions();self.clock=clock;self.cooldown={}
        self.ledger=self.run.parent.parent/'scan-control/renderer-trim-cooldowns.json'
    def event(self,kind,detail):
        path=self.folder/'events.json';events=self.read(path) if path.exists() else []
        events.append({'at':datetime.now(timezone.utc).isoformat(),'kind':kind,'detail':detail})
        self.write(path,events[-200:])
    def __call__(self):
        if self.life()!='running':return
        # Cross-implementation exclusive lock, including the legacy PS helper.
        import msvcrt
        with (self.folder/'guard.lock').open('a+b') as lock:
            lock.seek(0);lock.write(b'1');lock.flush();lock.seek(0);msvcrt.locking(lock.fileno(),msvcrt.LK_NBLCK,1)
            self.pass_once()
    def pass_once(self):
        ledger=self.read(self.ledger) if self.ledger.exists() else {};counts={'stop':0,'trim':0}
        for row in self.native.candidates():
            if self.life()!='running':return
            mode=row['action'];now=self.clock();available=self.host.available()
            if counts[mode]>=3 or available>=512*1024**2:continue
            if mode=='trim' and available>=256*1024**2:continue
            key=f"{row['pid']}:{row['ticks']}" if mode=='trim' else row['name']
            if mode=='trim' and key in ledger and expiry(ledger[key])>now:continue
            if mode=='stop' and self.cooldown.get(key,0)>now:continue
            try:
                def before(current):
                    if self.life()!='running':raise OSError('Worker no longer active')
                    if mode=='trim':
                        ledger[key]=datetime.fromtimestamp(now+1200,timezone.utc).isoformat();self.write(self.ledger,ledger)
                    self.event('renderer_trim_intent' if mode=='trim' else 'stop_intent',
                        {k:current[k] for k in ('pid','name','path','ticks')})
                    if self.life()!='running':raise OSError('Worker stopped after intent')
                result=self.native.act(row,before);counts[mode]+=1
                if mode=='stop':self.cooldown[key]=now+60
                self.event('renderer_page_reclamation' if mode=='trim' else 'stopped',result)
                state=self.read(self.folder/'status.json');counter='renderer_trims' if mode=='trim' else 'actions'
                state[counter]=state.get(counter,0)+1;self.write(self.folder/'status.json',state)
            except Exception as exc:
                if mode=='stop':self.cooldown[key]=now+900
                self.event('denied_or_identity_unverified',{'pid':row['pid'],'error':str(exc)[:500]})
