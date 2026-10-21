import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from flakefinder import storage


@pytest.fixture
def installation(tmp_path, monkeypatch):
    local = tmp_path/'checkout'
    cloud = tmp_path/'mounted-shared-drive'/'Automated Microscopy'
    local.mkdir(); cloud.mkdir(parents=True)
    policy = {'schema':1, 'mode':'drive_required', 'project_id':'test-project', 'project_root':str(cloud)}
    (cloud/'.flakefinder-project.json').write_text(json.dumps({'project_id':'test-project'}))
    monkeypatch.setattr(storage, 'WORKSPACE', local)
    monkeypatch.setattr(storage, 'load_policy', lambda: policy)
    monkeypatch.delenv('FLAKEFINDER_PROJECT_ROOT', raising=False)
    return local, cloud, policy


def test_legacy_output_maps_to_shared_relative_path(installation):
    local, cloud, _ = installation
    assert storage.resolve_output_path(local/'data/run-a/analysis') == cloud/'data/run-a/analysis'
    assert not (local/'data').exists()
    assert storage.resolve_output_path(cloud/'data/run-a') == cloud/'data/run-a'


def test_disconnected_mount_does_not_create_local_fallback(installation):
    local, cloud, _ = installation
    (cloud/'.flakefinder-project.json').unlink()
    with pytest.raises(storage.StorageUnavailable, match='unavailable'):
        storage.resolve_output_path(local/'data/new-run')
    assert not (local/'data').exists()


def test_wrong_shared_folder_refused(installation):
    local, cloud, _ = installation
    (cloud/'.flakefinder-project.json').write_text('{"project_id":"wrong"}')
    with pytest.raises(storage.StorageUnavailable, match='identity mismatch'):
        storage.resolve_output_path(local/'data/new-run')


def test_escape_and_unrelated_cli_output_refused(installation, tmp_path):
    local, cloud, _ = installation
    with pytest.raises(storage.StorageUnavailable):
        storage.resolve_output_path(cloud/'../escaped')
    with pytest.raises(storage.StorageUnavailable):
        storage.resolve_output_path(tmp_path/'other-local-folder')


def test_explicit_independent_library_fixture_stays_separate(installation, tmp_path):
    target = tmp_path/'independent-library-fixture'
    assert storage.managed_output_path(target)==target
    assert not target.exists()


def test_hold_precedes_storage_probe_or_hardware_import(installation, monkeypatch):
    local, cloud, _ = installation
    (local/'OPERATOR-HOLD.json').write_text('{"acquisition_authorized":false,"reason":"Chips removed"}')
    from flakefinder.zen import ZenMicroscope
    with pytest.raises(storage.StorageUnavailable, match='Chips removed'):
        ZenMicroscope()
    assert not list(cloud.glob('.write-check-*'))


def test_probe_cleans_only_its_owned_temporary_file(installation):
    _, cloud, policy = installation
    existing=cloud/'.write-check-somebody-else'
    existing.write_text('preserve')
    assert storage.verify_project(policy,write_probe=True)==cloud
    assert list(cloud.glob('.write-check-*'))==[existing]


def test_laptop_mount_override_needs_same_project_identity(installation, tmp_path, monkeypatch):
    local, cloud, _ = installation
    other=tmp_path/'LaptopMount';other.mkdir()
    (other/'.flakefinder-project.json').write_text('{"project_id":"test-project"}')
    monkeypatch.setenv('FLAKEFINDER_PROJECT_ROOT', str(other))
    assert storage.resolve_output_path(local/'data/analysis')==other/'data/analysis'


def test_explicit_missing_or_empty_policy_cannot_disable_drive(tmp_path, monkeypatch):
    monkeypatch.setenv('FLAKEFINDER_STORAGE_POLICY', str(tmp_path/'missing-policy.json'))
    with pytest.raises(storage.StorageUnavailable, match='policy unavailable'):
        storage.load_policy()
    monkeypatch.setenv('FLAKEFINDER_STORAGE_POLICY', '')
    with pytest.raises(storage.StorageUnavailable, match='empty'):
        storage.load_policy()


@pytest.mark.parametrize('content', ['[]', '{}', '{invalid'])
def test_invalid_policy_is_explicit_storage_failure(tmp_path, monkeypatch, content):
    path=tmp_path/'policy.json';path.write_text(content)
    monkeypatch.setenv('FLAKEFINDER_STORAGE_POLICY', str(path))
    with pytest.raises(storage.StorageUnavailable):
        storage.load_policy()


def test_write_probe_collision_preserves_unowned_file(installation, monkeypatch):
    _, cloud, policy=installation
    monkeypatch.setattr(storage.uuid, 'uuid4', lambda: SimpleNamespace(hex='fixed-collision'))
    existing=cloud/'.write-check-fixed-collision';existing.write_text('preserve this file')
    with pytest.raises(storage.StorageUnavailable, match='not writable'):
        storage.verify_project(policy, write_probe=True)
    assert existing.read_text()=='preserve this file'


def test_upload_cache_accounts_for_next_capture(installation, monkeypatch):
    _, _, policy=installation
    policy.update(cache_reserve_bytes=1000, cache_capture_allowance_bytes=100)
    monkeypatch.setattr(storage.shutil, 'disk_usage', lambda path: SimpleNamespace(free=1099))
    with pytest.raises(storage.StorageUnavailable, match='next capture'):
        storage.verify_project(policy, acquisition=True)
    monkeypatch.setattr(storage.shutil, 'disk_usage', lambda path: SimpleNamespace(free=1100))
    storage.verify_project(policy, acquisition=True)
