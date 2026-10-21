"""Launch the existing allowlisted cleanup helper, bound to one scan worker."""
from datetime import datetime, timezone
from pathlib import Path
import ctypes
import os
import shutil
import subprocess
import sys

from .model import atomic_json


def process_started_utc():
    """Preserve Windows' 100 ns identity precision when handing off the PID."""
    from ctypes import wintypes
    api=ctypes.WinDLL('kernel32',use_last_error=True)
    api.GetCurrentProcess.restype=wintypes.HANDLE
    api.GetProcessTimes.argtypes=[wintypes.HANDLE]+[ctypes.POINTER(wintypes.FILETIME)]*4
    values=[wintypes.FILETIME() for _ in range(4)]
    if not api.GetProcessTimes(api.GetCurrentProcess(),*[ctypes.byref(v) for v in values]):
        raise OSError(ctypes.get_last_error(),'Cannot read worker process identity')
    ticks=(values[0].dwHighDateTime<<32)|values[0].dwLowDateTime
    seconds,fraction=divmod(ticks,10_000_000)
    date=datetime.fromtimestamp(seconds-11644473600,timezone.utc)
    return date.strftime('%Y-%m-%dT%H:%M:%S')+f'.{fraction:07d}Z'


def powershell_path():
    choices=[shutil.which('pwsh'),Path(sys.base_prefix).parent/'native'/'powershell'/'pwsh.exe']
    return next((Path(p).resolve() for p in choices if p and Path(p).is_file()),None)


def launch_memory_guard(run,deadline_at,*,launcher=None,started_reader=None,runtime=None,source=None):
    run=Path(run);source=Path(source) if source is not None else Path(__file__).resolve().parents[1]
    record={'schema':1,'run_id':run.name,'worker_pid':os.getpid(),'state':'unavailable'}
    try:
        if os.name!='nt':raise OSError('Allowlisted cleanup helper is Windows-only')
        runtime=Path(runtime) if runtime else powershell_path()
        script=source/'scripts'/'watch_scan_memory.ps1'
        if runtime is None or not runtime.is_file() or not script.is_file():
            raise OSError('PowerShell 7 or run-scoped cleanup script is unavailable')
        started=(started_reader or process_started_utc)()
        deadline=datetime.fromtimestamp(deadline_at,timezone.utc).isoformat()
        command=[str(runtime),'-NoLogo','-NoProfile','-NonInteractive','-File',str(script),
            '-RunDirectory',str(run),'-WorkerPid',str(os.getpid()),'-WorkerStartUtc',started,
            '-DeadlineUtc',deadline]
        with (run/'memory-guard.stdout.log').open('ab') as out,(run/'memory-guard.stderr.log').open('ab') as err:
            child=(launcher or subprocess.Popen)(command,stdin=subprocess.DEVNULL,stdout=out,stderr=err,
                creationflags=subprocess.CREATE_NO_WINDOW,cwd=str(source))
        record.update(state='launched',pid=child.pid,worker_start_utc=started,deadline_utc=deadline)
    except Exception as exc:
        record['error']=str(exc)[:1000]
    atomic_json(run/'memory-guard-launch.json',record)
    return record
