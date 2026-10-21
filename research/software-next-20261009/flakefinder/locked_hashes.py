"""Session-local hashes backed by Windows handles denying writes and deletion.

Only byte hashing is reused. Callers must still validate their receipts, inventory,
ownership and semantic evidence on every use. Unsupported files fall back to reads.
"""
import ctypes
import hashlib
import os
from pathlib import Path
import stat

from .model import SafetyError


def identity(info):
    # Windows stat/fstat disagree on legacy st_ctime (birth vs change time).
    # Stable file ID plus the held deny-write/delete handle binds the bytes.
    return (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns)


def open_locked(path):
    if os.name != 'nt':
        raise OSError('Windows sharing locks unavailable')
    import msvcrt
    from ctypes import wintypes as w
    kernel = ctypes.WinDLL('kernel32', use_last_error=True)
    create = kernel.CreateFileW
    create.argtypes = [w.LPCWSTR, w.DWORD, w.DWORD, w.LPVOID, w.DWORD, w.DWORD, w.HANDLE]
    create.restype = w.HANDLE
    handle = create(str(path), 0x80000000, 1, None, 3, 0, None)
    if handle == ctypes.c_void_p(-1).value:
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        return os.fdopen(msvcrt.open_osfhandle(handle, os.O_RDONLY | os.O_BINARY), 'rb')
    except BaseException:
        kernel.CloseHandle.argtypes = [w.HANDLE]
        kernel.CloseHandle(handle)
        raise


class LockedHashes:
    def __init__(self, maximum_files=128):
        self.maximum_files = maximum_files
        self.entries = {}
        self.hits = 0
        self.misses = 0
        self.text_bytes = 0

    def _identity(self, path, stream):
        info = path.lstat()
        if (stat.S_ISLNK(info.st_mode) or getattr(info, 'st_file_attributes', 0) & 0x400
                or path.resolve() != path.absolute()):
            raise SafetyError('Locked hash source is redirected.')
        held = os.fstat(stream.fileno())
        if not held.st_ino or not held.st_dev:
            raise OSError('Stable file identity unavailable')
        if identity(info) != identity(held):
            raise SafetyError('Locked hash source identity changed.')
        return identity(held)

    def digest(self, path, fallback):
        path = Path(path).absolute()
        if path in self.entries:
            stream, original, digest, _ = self.entries[path]
            if self._identity(path, stream) != original:
                raise SafetyError('Locked hash source changed.')
            self.hits += 1
            return digest
        self.misses += 1
        if len(self.entries) >= self.maximum_files:
            return fallback(path)
        try:
            stream = open_locked(path)
        except OSError:
            return fallback(path)
        try:
            try:
                original = self._identity(path, stream)
            except (OSError, SafetyError):
                # Cache eligibility is not a new acceptance policy. The caller
                # still performs its original path/receipt validation. A
                # provider may lack a usable path-to-handle identity initially.
                # After identity is established, any change still fails below
                # and on every cached access; no cached digest is reused here.
                stream.close()
                return fallback(path)
            h = hashlib.sha256()
            data = bytearray() if path.suffix.lower() in ('.json','.metadata') and original[2] <= 2*1024**2 and self.text_bytes + original[2] <= 4*1024**2 else None
            for block in iter(lambda: stream.read(1024 * 1024), b''):
                h.update(block)
                if data is not None:data.extend(block)
            if self._identity(path, stream) != original:
                raise SafetyError('Locked hash source changed during hashing.')
            digest = h.hexdigest()
            data = bytes(data) if data is not None else None
            self.text_bytes += len(data) if data is not None else 0
            self.entries[path] = (stream, original, digest, data)
            return digest
        except BaseException:
            stream.close()
            raise

    def read_text(self, path, fallback):
        path = Path(path).absolute()
        entry = self.entries.get(path)
        if entry is None or entry[3] is None:return fallback(path)
        if self._identity(path, entry[0]) != entry[1]:
            raise SafetyError('Locked text source changed.')
        return entry[3].decode('utf-8-sig')

    def close(self):
        for stream, _, _, _ in self.entries.values():
            stream.close()
        self.entries.clear()
        self.text_bytes = 0
