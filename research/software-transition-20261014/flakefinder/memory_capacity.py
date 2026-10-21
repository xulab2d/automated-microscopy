"""Allocation capacity, not a requirement to leave pageable physical RAM idle.

Windows ullAvailPageFile is the commit available to this process (including job
quotas); ullAvailVirtual additionally bounds its address space. Neither changes
pagefile or operating-system settings. This module uses the standard library.
"""
import ctypes
import os

MIB=1024**2
MAX_RGB_BYTES=128*MIB
STARTUP_WORKING_BYTES=512*MIB+6*MAX_RGB_BYTES+64*MIB
CONTROL_WORKING_BYTES=64*MIB

def snapshot():
    if os.name=='nt':
        class Memory(ctypes.Structure):
            _fields_=[('length',ctypes.c_ulong),('load',ctypes.c_ulong)]+[(n,ctypes.c_ulonglong) for n in
                ('total','available','commit_limit','commit_available','virtual_total','virtual_available','extended')]
        value=Memory();value.length=ctypes.sizeof(value)
        if not ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(value)):raise OSError('Windows allocation capacity unavailable')
        return {'physical_available_bytes':int(value.available),'physical_total_bytes':int(value.total),
                'commit_available_bytes':int(value.commit_available),'address_available_bytes':int(value.virtual_available),
                'policy':'windows_commit_and_address_capacity_v1'}
    available=int(os.sysconf('SC_AVPHYS_PAGES'))*int(os.sysconf('SC_PAGE_SIZE'))
    return {'physical_available_bytes':available,'physical_total_bytes':int(os.sysconf('SC_PHYS_PAGES'))*int(os.sysconf('SC_PAGE_SIZE')),
            'commit_available_bytes':available,'address_available_bytes':available,'policy':'physical_capacity_fallback_v1'}

def capture_working_bytes(rgb_bytes=MAX_RGB_BYTES):
    if type(rgb_bytes) is not int or not 0<rgb_bytes<=MAX_RGB_BYTES:raise ValueError('Invalid raw RGB memory estimate')
    # Raw transfer, decoded RGB, vendor/serialization copies and processing
    # staging; 64MiB for metadata/ROI/encoding. This is an estimate, not a claim
    # of measured peak usage. First frame uses the decoder's maximum bound.
    return 6*rgb_bytes+64*MIB

def assess(value,working_bytes,*,purpose):
    if type(working_bytes) is not int or working_bytes<0:raise ValueError('Invalid operation memory estimate')
    for key in ('physical_available_bytes','commit_available_bytes','address_available_bytes'):
        if type(value.get(key)) is not int or value[key]<0:raise OSError('Missing allocation capacity: '+key)
    margin=(working_bytes+3)//4;required=working_bytes+margin
    capacity=min(value['commit_available_bytes'],value['address_available_bytes'])
    return {**value,'purpose':purpose,'working_allowance_bytes':working_bytes,'uncertainty_allowance_bytes':margin,
            'required_capacity_bytes':required,'adequate':capacity>=required,'physical_free_floor_bytes':0,
            'qualification':'Estimated incremental allocation plus 25% uncertainty; Windows paging remains enabled and unchanged.'}

def require(value,working_bytes,*,purpose):
    result=assess(value,working_bytes,purpose=purpose)
    if not result['adequate']:
        raise MemoryError(f"Insufficient allocation capacity before {purpose}: {min(value['commit_available_bytes'],value['address_available_bytes'])} bytes available, {result['required_capacity_bytes']} estimated required.")
    return result
