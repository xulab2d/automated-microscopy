"""Host RAM checks and bounded waiting before allocation; no hardware access."""
import ctypes
import os
import time
from .model import AcquisitionError

MEMORY_RESERVE_BYTES = 1024**3
CAPTURE_WORKING_BYTES = 512*1024**2

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
    if type(working_bytes) is not int or working_bytes<0:raise ValueError('Invalid RAM working allowance.')
    available=available_memory_bytes();required=MEMORY_RESERVE_BYTES+working_bytes
    if available<required:
        raise MemoryHeadroomError(f'Insufficient available RAM before {purpose}: {available} bytes available, {required} required.')
    return {'available_bytes':available,'reserve_bytes':MEMORY_RESERVE_BYTES,'working_allowance_bytes':working_bytes,
            'purpose':purpose,'qualification':'Pre-allocation headroom check; concurrent external allocations are not controlled.'}


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
