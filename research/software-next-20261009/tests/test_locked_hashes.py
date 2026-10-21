import hashlib
import os
from pathlib import Path
from types import SimpleNamespace

import pytest

from flakefinder.locked_hashes import LockedHashes
from flakefinder.model import SafetyError
from flakefinder import vendor_lifecycle as lifecycle


@pytest.mark.skipif(os.name != 'nt', reason='Windows sharing semantics')
def test_locked_bytes_reused_and_write_delete_denied_until_close(tmp_path):
    path=tmp_path/'source';path.write_bytes(b'original bytes')
    cache=LockedHashes()
    try:
        expected=hashlib.sha256(path.read_bytes()).hexdigest()
        assert cache.digest(path,lifecycle._file_hash)==expected
        with pytest.raises(OSError):path.write_bytes(b'mutated')
        with pytest.raises(OSError):path.unlink()
        assert cache.digest(path,lambda _:pytest.fail('hash reread'))==expected
        assert cache.hits==1
    finally:cache.close()
    path.write_bytes(b'changed after release')
    assert cache.digest(path,lifecycle._file_hash)!=expected
    cache.close()


def test_unavailable_lock_falls_back_every_time(tmp_path,monkeypatch):
    import flakefinder.locked_hashes as module
    def denied(_):raise OSError('unsupported provider')
    monkeypatch.setattr(module,'open_locked',denied)
    path=tmp_path/'source';path.write_bytes(b'a');cache=LockedHashes()
    a=cache.digest(path,lifecycle._file_hash);path.write_bytes(b'b')
    assert cache.digest(path,lifecycle._file_hash)!=a
    assert not cache.entries


@pytest.mark.parametrize('error',[OSError('provider identity unavailable'),
    SafetyError('Locked hash source is redirected.'),
    SafetyError('Locked hash source identity changed.')])
def test_initial_uncacheable_identity_uses_baseline_without_entry(tmp_path,monkeypatch,error):
    import flakefinder.locked_hashes as module
    p=tmp_path/'source';p.write_bytes(b'original');opened=[]
    def opened_read(path):
        stream=path.open('rb');opened.append(stream);return stream
    monkeypatch.setattr(module,'open_locked',opened_read)
    cache=LockedHashes()
    def unavailable(*_):raise error
    monkeypatch.setattr(cache,'_identity',unavailable)
    assert cache.digest(p,lifecycle._file_hash)==lifecycle._file_hash(p)
    assert opened[0].closed and not cache.entries
    p.write_bytes(b'mutated')
    assert cache.digest(p,lifecycle._file_hash)==lifecycle._file_hash(p)
    assert all(stream.closed for stream in opened)


def test_initial_cache_fallback_does_not_swallow_baseline_rejection(tmp_path,monkeypatch):
    import flakefinder.locked_hashes as module
    p=tmp_path/'source';p.write_bytes(b'x')
    monkeypatch.setattr(module,'open_locked',lambda path:path.open('rb'))
    cache=LockedHashes()
    def uncacheable(*_):raise SafetyError('cache eligibility')
    def original_rejection(_):raise SafetyError('original validator rejected evidence')
    monkeypatch.setattr(cache,'_identity',uncacheable)
    with pytest.raises(SafetyError,match='original validator'):cache.digest(p,original_rejection)
    assert not cache.entries


@pytest.mark.parametrize('operation',['digest','read_text'])
def test_cached_identity_error_never_falls_back(tmp_path,monkeypatch,operation):
    import flakefinder.locked_hashes as module
    p=tmp_path/'source.json';p.write_bytes(b'{}');cache=LockedHashes()
    monkeypatch.setattr(module,'open_locked',lambda path:path.open('rb'))
    monkeypatch.setattr(cache,'_identity',lambda *_:module.identity(p.stat()))
    try:
        cache.digest(p,lifecycle._file_hash)
        def changed(*_):raise SafetyError('cached identity mutation')
        monkeypatch.setattr(cache,'_identity',changed)
        with pytest.raises(SafetyError,match='cached identity mutation'):
            getattr(cache,operation)(p,lambda _:pytest.fail('must not hide cached mutation'))
    finally:cache.close()


def test_identity_change_during_initial_hash_never_falls_back(tmp_path,monkeypatch):
    import flakefinder.locked_hashes as module
    p=tmp_path/'source';p.write_bytes(b'x');cache=LockedHashes();calls=[]
    monkeypatch.setattr(module,'open_locked',lambda path:path.open('rb'))
    def verify(*_):
        calls.append(1)
        if len(calls)>1:raise SafetyError('changed during hash')
        return module.identity(p.stat())
    monkeypatch.setattr(cache,'_identity',verify)
    with pytest.raises(SafetyError,match='changed during hash'):
        cache.digest(p,lambda _:pytest.fail('must not hide mutation'))
    assert not cache.entries


@pytest.mark.skipif(os.name != 'nt', reason='Windows sharing semantics')
def test_changed_path_identity_refused(tmp_path,monkeypatch):
    path=tmp_path/'source';path.write_bytes(b'a');cache=LockedHashes()
    try:
        cache.digest(path,lifecycle._file_hash)
        monkeypatch.setattr(cache,'_identity',lambda *_:('different',))
        with pytest.raises(SafetyError,match='changed'):cache.digest(path,lifecycle._file_hash)
    finally:cache.close()


def test_semantics_revalidated_and_context_reset_on_failure(tmp_path,monkeypatch):
    from flakefinder import vendor_backlog
    calls=[]
    def validate(_):
        calls.append(1)
        if len(calls)==2:raise SafetyError('owner/inventory changed')
        return {}
    monkeypatch.setattr(vendor_backlog,'validated_reviews',validate)
    scope=SimpleNamespace(keep_vendor_results=False,_vendor_capture_root=tmp_path,
        _vendor_jobs={},_vendor_review_hashes=LockedHashes())
    assert lifecycle.check_pending(scope)['pending_jobs']==0
    with pytest.raises(SafetyError,match='owner/inventory'):lifecycle.check_pending(scope)
    assert len(calls)==2 and lifecycle._review_hashes.get() is None


def test_handle_bound_preserves_hash_reads(tmp_path):
    p=tmp_path/'x';p.write_bytes(b'x');cache=LockedHashes(maximum_files=0)
    assert cache.digest(p,lifecycle._file_hash)==lifecycle._file_hash(p)
    assert cache.entries=={}

@pytest.mark.skipif(os.name != 'nt', reason='Windows sharing semantics')
def test_verified_small_text_reuse_and_release(tmp_path):
    p=tmp_path/'source.json';p.write_bytes(b'\xef\xbb\xbf{"a":1}')
    cache=LockedHashes()
    try:
        # Reads are only reused after the bytes have been hashed under lock.
        assert cache.read_text(p,lambda _: 'unverified')=='unverified'
        cache.digest(p,lifecycle._file_hash)
        assert cache.read_text(p,lambda _:pytest.fail('reread'))=='{"a":1}'
        assert cache.text_bytes==p.stat().st_size
    finally:cache.close()
    assert cache.text_bytes==0

def test_default_adapter_cache_created_lazily(tmp_path,monkeypatch):
    from flakefinder import vendor_backlog
    monkeypatch.setattr(vendor_backlog,'validated_reviews',lambda _: {})
    scope=SimpleNamespace(keep_vendor_results=False,_vendor_capture_root=tmp_path,_vendor_jobs={})
    lifecycle.check_pending(scope)
    assert hasattr(scope,'_vendor_review_hashes')
    if os.name=='nt':
        assert isinstance(scope._vendor_review_hashes,LockedHashes)
        scope._vendor_review_hashes.close()

def test_cache_close_failure_cannot_skip_hardware_and_lease_close():
    from flakefinder.zen import ZenMicroscope
    events=[]
    def cache_close():
        events.append('cache')
        raise OSError('injected cache close failure')
    scope=ZenMicroscope.__new__(ZenMicroscope)
    scope._mtb=SimpleNamespace(close=lambda:events.append('mtb'))
    scope.channel=SimpleNamespace(close=lambda:events.append('channel'))
    scope.lease=SimpleNamespace(close=lambda:events.append('lease'))
    scope._vendor_review_hashes=SimpleNamespace(close=cache_close)
    with pytest.raises(OSError):scope.close()
    assert events==['mtb','channel','cache','lease'] and scope.armed is False
