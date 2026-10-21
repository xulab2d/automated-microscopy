"""Priority workspace inventory and sequential COPY-ONLY relocation worker."""
from pathlib import Path
import argparse
import collections
import json
import os
import shutil
import sys
import time

import bounded_transfer as transfer


EXCLUDE_DIRECTORIES = ['.git', '.venv*', 'venv', '__pycache__', '.pytest_cache',
    '.mypy_cache', '.ruff_cache', 'node_modules', '.test-*', '.tox', '.nox',
    'research/drive-relocation/state', 'research/drive-relocation/workspace-state',
    'laptop-viewer']
EXCLUDE_FILES = ['*.pyc', '*.pyo', '.env', '.env.*',
    '*/.env', '*/.env.*', '*credentials.json', '*client_secret*.json', '*token.json',
    '*token.pickle', '*.pem', '*.pfx', '*.key',
    'research/drive-relocation/*ledger*.json', 'research/drive-relocation/*batch*.json',
    'research/drive-relocation/*.lock', 'research/drive-relocation/*.tmp',
    'research/drive-relocation/worker-state.json', 'research/drive-relocation/STOP',
    'Laptop Review.zip', 'Laptop Review.zip.*']


def inventory_workspace(source, destination, ledger):
    data = transfer.inventory(source, destination, ledger, exclude_directories=EXCLUDE_DIRECTORIES,
        defer_patterns=EXCLUDE_FILES, allow_oversized=True, workspace_phases=True)
    phases = collections.defaultdict(lambda: {'files': 0, 'bytes': 0})
    oversized = []
    for row in data['files']:
        phases[row['phase']]['files'] += 1
        phases[row['phase']]['bytes'] += row['bytes']
        if row['bytes'] > data['max_batch_bytes']:
            oversized.append({key: row[key] for key in ('relative_path', 'bytes', 'phase')})
    result = {'created_at_utc': transfer.probe.now(), 'ledger': str(Path(ledger).resolve()),
        'file_count': data['file_count'], 'total_bytes': data['total_bytes'], 'phases': phases,
        'excluded_directories': data['excluded_directories_not_inventoried'],
        'excluded_or_deferred_files': data['deferred_files'], 'oversized_files': oversized,
        'source_deletion_performed': False,
        'qualification': 'Only explicit environment/Git/cache/dot-prefixed .test-*/lock/credential patterns are excluded. Ordinary data/test-* outputs are included; do not describe all test outputs as excluded. Scientific images, vendor outputs, analyses, research, generated artifacts, code and dependency declarations remain included. Live relocation journals are retained separately to avoid copying a changing self-inventory. laptop-viewer and Laptop Review.zip use separately recorded inventories.'}
    transfer.write_json(Path(ledger).parent/'inventory-summary.json', result)
    return result


def run(ledger, stop_file, poll_seconds=15, reserve_wait_minutes=10, upload_wait_minutes=30,
        max_batches=None):
    # A separate lifetime lock prevents two workers from alternating ledger phases.
    with transfer.ledger_lock(Path(ledger).parent/'worker-owner.json'):
        return _run(ledger, stop_file, poll_seconds, reserve_wait_minutes, upload_wait_minutes, max_batches)


def _run(ledger, stop_file, poll_seconds=15, reserve_wait_minutes=10, upload_wait_minutes=30,
        max_batches=None):
    ledger, stop_file = Path(ledger).resolve(), Path(stop_file).resolve()
    state_path = ledger.parent/'worker-state.json'
    started, completed, wait_started = transfer.probe.now(), 0, None
    def state(value, **details):
        result = {'state': value, 'pid': os.getpid(), 'started_at_utc': started,
            'updated_at_utc': transfer.probe.now(), 'ledger': str(ledger),
            'stop_file': str(stop_file), 'source_deletion_performed': False,
            'completed_batches_this_run': completed,
            'free_cache_volume_bytes': shutil.disk_usage(transfer.probe.DB.anchor).free, **details}
        transfer.write_json(state_path, result)
        print(json.dumps(result), flush=True)
    def check_cancel():
        if stop_file.exists():
            raise transfer.RelocationStopped('Relocation STOP requested; original files retained.')
    try:
        if sys.platform == 'win32':
            import ctypes
            ctypes.windll.kernel32.SetPriorityClass(ctypes.windll.kernel32.GetCurrentProcess(), 0x4000)
        state('running')
        while max_batches is None or completed < max_batches:
            check_cancel()
            try:
                copied = transfer.copy_next(ledger, check_cancel=check_cancel)
                wait_started = None
            except OSError as exc:
                if 'reserve' not in str(exc).lower():
                    raise
                wait_started = wait_started or time.monotonic()
                state('waiting_for_disk_reserve', error=str(exc))
                if time.monotonic()-wait_started >= reserve_wait_minutes*60:
                    state('blocked_disk_reserve', error=str(exc))
                    return 2
                time.sleep(poll_seconds)
                continue
            if copied['state'] == 'all_files_client_confirmed':
                state('complete', result=copied)
                return 0
            state('waiting_for_client_confirmation', result=copied)
            upload_started = time.monotonic()
            while True:
                check_cancel()
                verified = transfer.verify_batch(ledger)
                if verified['state'] == 'client_confirmed':
                    completed += 1
                    state('batch_client_confirmed', result=verified)
                    break
                if time.monotonic()-upload_started >= upload_wait_minutes*60:
                    state('blocked_upload_confirmation', result=verified)
                    return 3
                state('waiting_for_client_confirmation', result=verified)
                time.sleep(poll_seconds)
        state('bounded_run_complete')
        return 0
    except transfer.RelocationStopped as exc:
        state('stopped', error=str(exc))
        return 0
    except Exception as exc:
        state('failed', error=repr(exc))
        raise


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest='command', required=True)
    p = sub.add_parser('inventory-workspace')
    p.add_argument('--source', required=True)
    p.add_argument('--destination', required=True)
    p.add_argument('--ledger', required=True)
    p = sub.add_parser('run')
    p.add_argument('--ledger', required=True)
    p.add_argument('--stop-file', required=True)
    p.add_argument('--max-batches', type=int)
    p.add_argument('--poll-seconds', type=int, default=15)
    p.add_argument('--reserve-wait-minutes', type=float, default=10)
    p.add_argument('--upload-wait-minutes', type=float, default=30)
    args = parser.parse_args()
    if args.command == 'inventory-workspace':
        summary = inventory_workspace(args.source, args.destination, args.ledger)
        print(json.dumps({key: summary[key] for key in
            ('file_count', 'total_bytes', 'phases', 'oversized_files', 'excluded_directories')}, indent=2))
    else:
        raise SystemExit(run(args.ledger, args.stop_file, args.poll_seconds,
            args.reserve_wait_minutes, args.upload_wait_minutes, args.max_batches))
