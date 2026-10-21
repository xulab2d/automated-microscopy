"""Host RAM preflight only; no camera, process controls or automatic cleanup."""
import ctypes
import os
from .model import AcquisitionError

MEMORY_RESERVE_BYTES = 1024**3
CAPTURE_WORKING_BYTES = 512*1024**2


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
        raise AcquisitionError(f'Insufficient available RAM before {purpose}: {available} bytes available, {required} required. No automatic retry or cleanup.')
    return {'available_bytes':available,'reserve_bytes':MEMORY_RESERVE_BYTES,'working_allowance_bytes':working_bytes,
            'purpose':purpose,'qualification':'Pre-allocation headroom check; concurrent external allocations are not controlled.'}
