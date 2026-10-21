"""Small resident supervisor with native identity-checked cleanup actions.

No microscope imports or access. Windows native read-only worker/RAM checks use
stdlib only. Cleanup retains SID/image/PID validation, exclusive locks,
allowlists and persisted renderer cooldowns without spawning a second runtime.
"""
import argparse
import ctypes
from ctypes import wintypes as W
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import time


def read(path):
    # Drive-mounted atomic replacements can briefly disappear to another process.
    for attempt in range(3):
        try:return json.loads(Path(path).read_text(encoding='utf-8-sig'))
        except (OSError,json.JSONDecodeError):
            if attempt==2:raise
            time.sleep(.1*(attempt+1))


def write(path,value):
    temp=path.with_name(path.name+'.watcher.tmp')
    with temp.open('w',encoding='utf-8') as f:
        json.dump(value,f,indent=2);f.flush();os.fsync(f.fileno())
    os.replace(temp,path)


def start_ticks(utc):
    match=re.fullmatch(r'(\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d)(?:\.(\d{1,7}))?Z',utc)
    if not match:raise ValueError('Exact UTC process identity required')
    seconds=int(datetime.fromisoformat(match[1]).replace(tzinfo=timezone.utc).timestamp())+11644473600
    return seconds*10_000_000+int((match[2] or '').ljust(7,'0'))


class Native:
    def __init__(self):
        self.k=ctypes.WinDLL('kernel32',use_last_error=True)
        self.k.OpenProcess.argtypes=[W.DWORD,W.BOOL,W.DWORD];self.k.OpenProcess.restype=W.HANDLE
        self.k.GetProcessTimes.argtypes=[W.HANDLE]+[ctypes.POINTER(W.FILETIME)]*4
        self.k.GetExitCodeProcess.argtypes=[W.HANDLE,ctypes.POINTER(W.DWORD)]
        self.k.CloseHandle.argtypes=[W.HANDLE]
    def matches(self,pid,ticks):
        handle=self.k.OpenProcess(0x1000,False,pid)
        if not handle:return False
        try:
            code=W.DWORD();times=[W.FILETIME() for _ in range(4)]
            if not self.k.GetExitCodeProcess(handle,ctypes.byref(code)) or code.value!=259:return False
            if not self.k.GetProcessTimes(handle,*[ctypes.byref(t) for t in times]):return False
            return ((times[0].dwHighDateTime<<32)|times[0].dwLowDateTime)==ticks
        finally:self.k.CloseHandle(handle)
    def available(self):
        class Status(ctypes.Structure):
            _fields_=[('length',W.DWORD),('load',W.DWORD),*[(n,ctypes.c_ulonglong) for n in
                ('total','available','page_total','page_available','virtual_total','virtual_available','extended')]]
        value=Status();value.length=ctypes.sizeof(value)
        if not self.k.GlobalMemoryStatusEx(ctypes.byref(value)):raise OSError('Memory read failed')
        return value.available


def lifecycle(status,run_id,pid,sha,matches,now,deadline):
    if now>=deadline:return 'deadline_reached'
    if not matches:return 'worker_exited_or_unreadable'
    if status.get('id')!=run_id or status.get('pid')!=pid or status.get('request_sha256')!=sha:return 'run_identity_changed'
    if status.get('state')!='running' or status.get('hardware_closed'):return 'run_not_running'
    return 'running'


def cleanup_due(available,now,last):return available<512*1024**2 and now-last>=60


def monitor(run,pid,started,deadline,action,host,*,clock=time.time,sleep=time.sleep,cycles=None):
    run=Path(run);folder=run/'memory-guard';folder.mkdir(exist_ok=True)
    sha=hashlib.sha256((run/'scan-request.json').read_bytes()).hexdigest()
    receipt=read(run/'worker-start.json')
    if receipt.get('id')!=run.name or receipt.get('pid')!=pid or receipt.get('request_sha256')!=sha:
        raise ValueError('Worker receipt identity changed')
    ticks=start_ticks(started);last=-float('inf');iteration=0;prior={}
    def publish(result):
        try:write(folder/'status.json',result)
        except OSError as exc:
            # Monitoring continues even if the optional report cannot be saved.
            result['report_write_error']=str(exc)[:1000]
    while cycles is None or iteration<cycles:
        iteration+=1;now=clock();matches=host.matches(pid,ticks);read_error=None
        if now>=deadline or not matches:
            state=lifecycle({},run.name,pid,sha,matches,now,deadline)
        else:
            try:status=read(run/'scan-status.json')
            except (OSError,json.JSONDecodeError) as exc:
                state='waiting_for_status';read_error=str(exc)[:1000]
            else:state=lifecycle(status,run.name,pid,sha,matches,now,deadline)
        try:prior=read(folder/'status.json')
        except (OSError,json.JSONDecodeError):pass
        result={**prior,'schema':1,'run_id':run.name,'worker_pid':pid,'worker_start_utc':started,
            'guard_pid':os.getpid(),'state':state,'deadline_utc':datetime.fromtimestamp(deadline,timezone.utc).isoformat(),
            'watcher_implementation':'stdlib-native-v2','poll_seconds':10,'cleanup_minimum_interval_seconds':60,
            'hardware_access':False,'automatic_restart':False,
            'actions':prior.get('actions',0),'access_denials':prior.get('access_denials',0),
            'renderer_trims':prior.get('renderer_trims',0),'updated_at':datetime.now(timezone.utc).isoformat()}
        if state=='waiting_for_status':
            result.update(last_error=read_error,cleanup_suspended=True)
            publish(result);prior=result;sleep(10);continue
        if state!='running':publish(result);return state
        result.pop('last_error',None);result.pop('cleanup_suspended',None)
        available=host.available();result.update(available_bytes=available,state='low_memory' if available<512*1024**2 else 'monitoring',
            threshold_bytes=512*1024**2,renderer_trim_trigger_bytes=256*1024**2,
            pressure_policy='Optional reclamation only; physical availability does not block camera allocation.')
        publish(result);prior=result
        if cleanup_due(available,now,last):
            last=now
            try:action()
            except Exception as exc:
                result.update(last_error=str(exc)[:1000]);publish(result)
        sleep(10)
    return 'test_cycle_limit'


def main():
    p=argparse.ArgumentParser();p.add_argument('--run',type=Path,required=True);p.add_argument('--worker',type=int,required=True)
    p.add_argument('--started',required=True);p.add_argument('--deadline',required=True);p.add_argument('--powershell')
    a=p.parse_args();run=a.run.resolve(strict=True)
    if not re.fullmatch(r'scan-\d{8}-\d{6}-[a-f0-9]{8}',run.name) or run.parent.name!='data':raise ValueError('Invalid run folder')
    if not (run.parent.parent/'.flakefinder-project.json').is_file():raise ValueError('Not a managed project')
    deadline=datetime.fromisoformat(a.deadline.replace('Z','+00:00')).timestamp()
    folder=run/'memory-guard';folder.mkdir(exist_ok=True)
    # Hold a separate lifetime lock; the short action helper uses guard.lock.
    import msvcrt
    with (folder/'watcher.lock').open('a+b') as lock:
        lock.seek(0);lock.write(b'1');lock.flush();lock.seek(0);msvcrt.locking(lock.fileno(),msvcrt.LK_NBLCK,1)
        from native_memory_actions import Cleanup
        host=Native();sha=hashlib.sha256((run/'scan-request.json').read_bytes()).hexdigest();ticks=start_ticks(a.started)
        def life():return lifecycle(read(run/'scan-status.json'),run.name,a.worker,sha,
                                   host.matches(a.worker,ticks),time.time(),deadline)
        action=Cleanup(run,host,life,read,write)
        monitor(run,a.worker,a.started,deadline,action,host)


if __name__=='__main__':main()
