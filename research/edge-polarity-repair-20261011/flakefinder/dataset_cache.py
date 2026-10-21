"""Bounded disposable cache of verified project objects, never Drive eviction.

Use directly from the Drive project by default. This optional cache is for
repeated local/cluster workloads; its copies are deleted by LRU when unpinned.
Deleting a mounted Drive file would delete the cloud object, so it is forbidden.
"""
from contextlib import contextmanager
from hashlib import sha256
from pathlib import Path
import json
import os
import time
import uuid


@contextmanager
def _lock(path):
    with path.open('a+b') as stream:
        stream.seek(0); stream.write(b'0'); stream.flush(); stream.seek(0)
        if os.name == 'nt':
            import msvcrt
            msvcrt.locking(stream.fileno(), msvcrt.LK_LOCK, 1)
        else:
            import fcntl
            fcntl.flock(stream, fcntl.LOCK_EX)
        try:
            yield
        finally:
            stream.seek(0)
            if os.name == 'nt':
                msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(stream, fcntl.LOCK_UN)


def file_hash(path):
    h = sha256()
    with Path(path).open('rb') as stream:
        for block in iter(lambda:stream.read(1024*1024), b''):
            h.update(block)
    return h.hexdigest()


class DatasetCache:
    def __init__(self, catalog, cache_root, quota_bytes=2*1024**3):
        self.catalog = catalog
        self.root = Path(cache_root).resolve()
        if self.root.is_relative_to(catalog.root) or catalog.root.is_relative_to(self.root):
            raise ValueError('Disposable cache must be separate from the authoritative project')
        if quota_bytes <= 0:
            raise ValueError('Cache quota must be positive')
        self.quota = int(quota_bytes)
        self.root.mkdir(parents=True,exist_ok=True)
        self.marker = self.root/'.flakefinder-cache.json'
        with _lock(self.root/'owner.lock'):
            if self.marker.exists():
                if json.loads(self.marker.read_text()).get('project_id') != catalog.project_id:
                    raise ValueError('Cache belongs to a different project')
            elif any(p.name != 'owner.lock' for p in self.root.iterdir()):
                raise ValueError('Refusing to adopt a nonempty unmarked directory')
            else:
                self.marker.write_text(json.dumps(dict(schema=1,project_id=catalog.project_id,disposable=True)))
            for name in ('objects','pins','receipts'):
                (self.root/name).mkdir(exist_ok=True)

    def _owned(self, path):
        value = path.resolve()
        if not value.is_relative_to(self.root) or path.is_symlink():
            raise ValueError('Cache path escapes its owned root')
        return value

    def _evict(self, needed):
        pins = {json.loads(p.read_text())['sha256'] for p in (self.root/'pins').glob('*.json')}
        objects = sorted((self.root/'objects').iterdir(),key=lambda p:p.stat().st_mtime_ns)
        used = sum(p.stat().st_size for p in objects)
        removed = []
        for path in objects:
            if used + needed <= self.quota:
                break
            receipt = self.root/'receipts'/(path.name+'.json')
            if path.name in pins or not receipt.is_file():
                continue
            record = json.loads(receipt.read_text())
            if record.get('sha256') != path.name or not record.get('verified_copy'):
                continue
            size = path.stat().st_size
            self._owned(path).unlink()
            self._owned(receipt).unlink()
            used -= size
            removed.append(path.name)
        if used + needed > self.quota:
            raise OSError('Cache budget exhausted by pinned/unknown files; no authoritative data removed')
        return removed

    @contextmanager
    def acquire(self, relative, expected_sha256):
        import re
        if not re.fullmatch('[a-f0-9]{64}',expected_sha256):
            raise ValueError('A known source SHA256 is required')
        source = self.catalog.resolve(relative)
        target = self.root/'objects'/expected_sha256
        pin = self.root/'pins'/(uuid.uuid4().hex+'.json')
        with _lock(self.root/'owner.lock'):
            if target.exists():
                if file_hash(target) != expected_sha256:
                    raise ValueError('Cached object checksum failed')
            else:
                self._evict(source.stat().st_size)
                pending = self.root/'objects'/(uuid.uuid4().hex+'.pending')
                try:
                    with source.open('rb') as read, pending.open('xb') as write:
                        for block in iter(lambda:read.read(1024*1024),b''):
                            write.write(block)
                        write.flush(); os.fsync(write.fileno())
                    if file_hash(pending) != expected_sha256:
                        raise ValueError('Project object differs from expected source hash')
                    os.replace(pending,target)
                    (self.root/'receipts'/(expected_sha256+'.json')).write_text(json.dumps(dict(
                        relative_path=relative,sha256=expected_sha256,verified_copy=True,bytes=target.stat().st_size)))
                finally:
                    if pending.exists(): self._owned(pending).unlink()
            os.utime(target,None)
            pin.write_text(json.dumps(dict(sha256=expected_sha256,pid=os.getpid(),created_at=time.time())))
        try:
            yield target
        finally:
            with _lock(self.root/'owner.lock'):
                self._owned(pin).unlink()

    def trim(self):
        with _lock(self.root/'owner.lock'):
            return self._evict(0)
