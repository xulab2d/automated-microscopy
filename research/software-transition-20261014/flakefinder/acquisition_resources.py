"""Host RAM checks and bounded waiting before allocation; no hardware access."""
import ctypes
import os
import time
from .model import AcquisitionError

from . import memory_capacity
MEMORY_RESERVE_BYTES = 0  # No fixed free-physical-RAM reservation.
CAPTURE_WORKING_BYTES = memory_capacity.capture_working_bytes()

class MemoryHeadroomError(AcquisitionError):
    """The next allocation has not started and can wait for available memory."""


def available_memory_bytes():
    if os.name == 'nt':
        class MemoryStatus(ctypes.Structure):
            _fields_ = [('length',ctypes.c_ulong),('load',ctypes.c_ulong),
                       *[(name,ctypes.c_ulonglong) for name in
                         ('total_phys','avail_phys','total_page','avail_page','total_virtual','avail_virtual','avail_extended')]]
        status=MemoryStatus();status.length=ctypes.sizeof(status)
        if not ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(status)):
            raise AcquisitionError('Cannot read available host RAM before acquisition.')
        return int(status.avail_phys)
    try:return int(os.sysconf('SC_AVPHYS_PAGES'))*int(os.sysconf('SC_PAGE_SIZE'))
    except (ValueError,OSError,AttributeError):
        raise AcquisitionError('Cannot read available host RAM before acquisition.')


def check_memory_headroom(working_bytes=CAPTURE_WORKING_BYTES, *, purpose='camera capture'):
    try:result=memory_capacity.require(memory_capacity.snapshot(),working_bytes,purpose=purpose)
    except MemoryError as exc:raise MemoryHeadroomError(str(exc)) from exc
    except OSError as exc:raise AcquisitionError(str(exc)) from exc
    return {**result,'available_bytes':result['physical_available_bytes'],'reserve_bytes':0}


def wait_for_memory_headroom(check_cancel, wait, *, report=None, max_wait_s=120.,
                             clock=time.monotonic, checker=None):
    """Pause before starting a camera job; resume that same pending capture.

    Only the typed pre-allocation failure is retried. No image/job is submitted,
    no process is restarted, and deadlines/cancellation remain checked while
    waiting. A sustained shortage leaves a diagnostic error for supervision.
    """
    import math
    if not math.isfinite(max_wait_s) or max_wait_s<=0:raise ValueError('Invalid memory recovery duration.')
    checker=checker or check_memory_headroom
    start=clock();attempts=0;last_report=None
    while True:
        check_cancel()
        try:
            result=checker()
        except MemoryHeadroomError as exc:
            elapsed=clock()-start
            if elapsed>=max_wait_s:
                if report:report('exhausted',elapsed,str(exc))
                raise MemoryHeadroomError(f'Memory recovery exhausted after {elapsed:.1f}s: {exc}') from exc
            if report and (last_report is None or elapsed-last_report>=5):
                report('waiting',elapsed,str(exc));last_report=elapsed
            attempts+=1
            wait(min(1.,max_wait_s-elapsed))
        else:
            elapsed=clock()-start
            check_cancel()
            if attempts and report:report('recovered',elapsed,None)
            return {**result,'memory_wait_seconds':elapsed if attempts else 0.,
                    'memory_rechecks':attempts,'memory_recovery':'recovered' if attempts else 'not_needed'}
