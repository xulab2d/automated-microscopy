"""Installation storage routing. Scientific outputs never fall back from Drive.

The code checkout and OS hardware locks remain local. Drive for Desktop still
uses its own managed upload cache; a successful file write is not an upload
receipt. Historical source paths and acquisition identities are not rewritten.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import shutil
import uuid

WORKSPACE = Path(__file__).resolve().parents[1]
POLICY_FILE = WORKSPACE / 'configs/storage.json'


class StorageUnavailable(RuntimeError):
    pass


def load_policy():
    explicit = os.environ.get('FLAKEFINDER_STORAGE_POLICY')
    if explicit is not None and not explicit.strip():
        raise StorageUnavailable('Explicit storage policy path is empty; refusing a local fallback.')
    path = Path(explicit if explicit is not None else POLICY_FILE)
    if not path.is_file():
        if explicit is not None:
            raise StorageUnavailable(f'Configured storage policy unavailable at {path}; refusing a local fallback.')
        return None  # Generic installations have no lab-specific mount policy.
    try:
        policy = json.loads(path.read_text(encoding='utf-8-sig'))
    except (OSError, ValueError) as exc:
        raise StorageUnavailable('Storage policy unreadable; refusing a local fallback.') from exc
    if (not isinstance(policy, dict) or policy.get('schema') != 1 or policy.get('mode') != 'drive_required'
            or not isinstance(policy.get('project_id'), str) or not policy['project_id'].strip()
            or not isinstance(policy.get('project_root'), str) or not policy['project_root'].strip()):
        raise StorageUnavailable('Invalid storage policy; refusing a local fallback.')
    return policy


def project_root(policy=None):
    policy = load_policy() if policy is None else policy
    if policy is None:
        return WORKSPACE
    root = Path(os.environ.get('FLAKEFINDER_PROJECT_ROOT', policy['project_root']))
    if not root.is_absolute():
        raise StorageUnavailable('FLAKEFINDER_PROJECT_ROOT must be an absolute mounted Drive path.')
    return root


def verify_project(policy=None, *, write_probe=False, acquisition=False):
    policy = load_policy() if policy is None else policy
    root = project_root(policy)
    if policy is None:
        return root
    marker = root / '.flakefinder-project.json'
    try:
        identity = json.loads(marker.read_text(encoding='utf-8-sig'))
    except (OSError, ValueError) as exc:
        raise StorageUnavailable(f'Drive project unavailable at {root}; no local fallback.') from exc
    if identity.get('project_id') != policy['project_id']:
        raise StorageUnavailable('Drive project identity mismatch; no output was created.')
    if acquisition:
        cache = Path(policy['cache_volume']) if os.name == 'nt' and policy.get('cache_volume') else root
        reserve = int(policy.get('cache_reserve_bytes', 10 * 1024**3))
        # The next vendor/raw capture may consume local Drive cache before it
        # uploads. Keep the same bounded capture allowance as the ZEN guard.
        allowance = int(policy.get('cache_capture_allowance_bytes', 512 * 1024**2))
        if reserve < 0 or allowance < 0:
            raise StorageUnavailable('Invalid negative Drive-cache reserve or capture allowance.')
        required = reserve + allowance
        if shutil.disk_usage(cache).free < required:
            raise StorageUnavailable('Drive upload-cache volume cannot preserve its reserve through the next capture.')
    if write_probe:
        probe = root / ('.write-check-' + uuid.uuid4().hex)
        created = False
        try:
            with probe.open('xb') as stream:
                created = True
                stream.write(b'FlakeFinder storage preflight\n')
                stream.flush()
                os.fsync(stream.fileno())
        except OSError as exc:
            raise StorageUnavailable(f'Drive project is not writable: {root}') from exc
        finally:
            # Only the exact uniquely owned probe may be removed.
            if created and probe.is_file():
                probe.unlink()
    return root


def resolve_output_path(path, *, require_managed=True, verify=True):
    """Map this checkout's logical paths to the mounted shared project.

    CLI calls are strict. Library callers can allow unrelated explicitly
    supplied paths (e.g. independent installations/test fixtures), while every
    path under this checkout is still routed through the required Drive mount.
    """
    requested = Path(path).expanduser().absolute()
    policy = load_policy()
    if policy is None:
        return requested.resolve()
    root = project_root(policy).absolute()
    if requested.is_relative_to(root):
        target = requested
    elif requested.is_relative_to(WSPACE := WORKSPACE.absolute()):
        target = root / requested.relative_to(WSPACE)
    elif require_managed:
        raise StorageUnavailable(f'Output must be inside the shared Drive project: {root}')
    else:
        return requested.resolve()
    # Reject traversal and links that redirect a managed output outside the root.
    if not target.resolve().is_relative_to(root.resolve()):
        raise StorageUnavailable('Output escapes the shared project.')
    if verify:
        verify_project(policy)
    return target.resolve()


def managed_output_path(path):
    return resolve_output_path(path, require_managed=False)


def assert_acquisition_allowed():
    """Honor the operator's no-scan hold before loading a hardware client."""
    paths = {WORKSPACE / 'OPERATOR-HOLD.json'}
    policy = load_policy()
    if policy:
        paths.add(project_root(policy) / 'OPERATOR-HOLD.json')
    for path in paths:
        if path.exists():
            hold = json.loads(path.read_text(encoding='utf-8-sig'))
            if hold.get('acquisition_authorized') is not True:
                raise StorageUnavailable('Acquisition is on operator hold. ' + hold.get('reason', ''))


def prepare_acquisition_storage(job_output_dir):
    assert_acquisition_allowed()
    verify_project(write_probe=True, acquisition=True)
    return resolve_output_path(job_output_dir)
