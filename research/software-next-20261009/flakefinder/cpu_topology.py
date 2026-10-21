"""Prefer separate physical cores within the process's allowed Windows CPU mask."""
import ctypes
import os


def select_processor_masks(allowed_mask, count, core_masks=()):
    """Return one-bit masks, choosing different cores before SMT siblings."""
    if type(allowed_mask) is not int or allowed_mask <= 0:
        raise ValueError('A positive allowed CPU mask is required.')
    if type(count) is not int or count < 1:
        raise ValueError('A positive CPU count is required.')
    selected=[]
    for core in core_masks:
        if type(core) is not int or core <= 0:
            raise ValueError('Invalid physical-core mask.')
        available=core & allowed_mask
        if available and not any(mask & core for mask in selected):
            selected.append(available & -available)
            if len(selected)==count:
                return selected
    remaining=allowed_mask
    for mask in selected:
        remaining &= ~mask
    while remaining and len(selected)<count:
        mask=remaining & -remaining
        selected.append(mask)
        remaining &= ~mask
    return selected


def windows_core_masks():
    """Read masks for the current processor group; no affinity is changed."""
    if os.name!='nt':
        raise OSError('Windows CPU topology is unavailable on this platform.')
    from ctypes import wintypes
    class ProcessorInfo(ctypes.Structure):
        _fields_=[('mask',ctypes.c_size_t),('relationship',wintypes.DWORD),
                  ('reserved',ctypes.c_ulonglong*2)]
    kernel=ctypes.WinDLL('kernel32',use_last_error=True)
    query=kernel.GetLogicalProcessorInformation
    query.argtypes=[ctypes.c_void_p,ctypes.POINTER(wintypes.DWORD)]
    query.restype=wintypes.BOOL
    length=wintypes.DWORD()
    query(None,ctypes.byref(length))
    if not length.value or length.value%ctypes.sizeof(ProcessorInfo):
        raise OSError('Windows returned no valid CPU topology.')
    buffer=ctypes.create_string_buffer(length.value)
    if not query(buffer,ctypes.byref(length)):
        raise ctypes.WinError(ctypes.get_last_error())
    records=[ProcessorInfo.from_buffer(buffer,i) for i in range(0,length.value,ctypes.sizeof(ProcessorInfo))]
    cores=[int(record.mask) for record in records if record.relationship==0]
    if not cores:
        raise OSError('Windows returned no physical CPU cores.')
    return cores


def processor_assignment(allowed_mask,count):
    """Report the chosen mask and topology provenance without changing it."""
    try:
        cores=windows_core_masks()
        source='Windows GetLogicalProcessorInformation'
    except OSError as exc:
        cores=[]
        source='logical CPU fallback: '+str(exc)
    masks=select_processor_masks(allowed_mask,count,cores)
    return {'processor_masks':masks,'affinity_mask':sum(masks),
            'allowed_mask':allowed_mask,'physical_core_masks':cores,'topology_source':source}
