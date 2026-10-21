"""Resumable, COPY-ONLY vendor-data transfer with bounded DriveFS staging.

No source removal, cache manipulation, credentials, or Drive database writes.
Client confirmation is explicitly distinct from independent server verification.
"""
from pathlib import Path
import argparse
import contextlib
import fnmatch
import hashlib
import json
import os
import shutil
import time
import uuid

import drivefs_transfer_probe as probe

MIB = 1024 ** 2
GIB = 1024 ** 3


class RelocationStopped(RuntimeError):
    pass


def read_json(path):
    return json.loads(Path(path).read_text(encoding='utf-8-sig'))


def write_json(path, data):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + '.' + uuid.uuid4().hex + '.tmp')
    with temporary.open('x', encoding='utf-8') as stream:
        json.dump(data, stream, indent=2)
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(temporary, path)


@contextlib.contextmanager
def ledger_lock(path):
    """Persistent lock file; no cleanup/removal and no stale-PID guessing."""
    import msvcrt
    lock = Path(path).with_suffix('.lock')
    with lock.open('a+b') as stream:
        if stream.tell() == 0:
            stream.write(b'0')
            stream.flush()
        stream.seek(0)
        msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
        try:
            yield
        finally:
            stream.seek(0)
            msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)


def ensure_destination(path):
    path = Path(path).resolve()
    path.relative_to(probe.SHARED.resolve())
    return path


def mapping_digest(entries):
    immutable = [{key: row[key] for key in
        ('relative_path', 'source', 'destination', 'bytes', 'source_mtime_ns')}
        for row in entries]
    return hashlib.sha256(json.dumps(immutable, sort_keys=True).encode()).hexdigest()


def inventory(source, destination, ledger, max_bytes=256*MIB, reserve_bytes=8*GIB,
              exclude_directories=(), defer_patterns=(), defer_oversized=False,
              priority_prefixes=(), allow_oversized=False, workspace_phases=False, max_files=256):
    source = Path(source).resolve()
    if max_bytes < 1 or max_files < 1 or reserve_bytes < 0:
        raise ValueError('Invalid batch or reserve limits.')
    destination = ensure_destination(destination)
    ledger = Path(ledger).resolve()
    if ledger.exists():
        raise FileExistsError('Existing relocation ledger must be resumed, never replaced.')
    if source.is_relative_to(probe.DB.parent.parent.resolve()):
        raise ValueError('DriveFS cache/configuration is excluded.')
    entries, deferred, excluded_directories = [], [], []
    for folder, directories, names in os.walk(source, followlinks=False):
        kept = []
        for name in directories:
            relative_directory = (Path(folder)/name).relative_to(source).as_posix()
            if any(fnmatch.fnmatchcase(name, pattern) or fnmatch.fnmatchcase(relative_directory, pattern)
                   for pattern in exclude_directories):
                excluded_directories.append(relative_directory)
            else:
                kept.append(name)
        directories[:] = kept
        for name in directories + names:
            item = Path(folder) / name
            info = item.lstat()
            if item.is_symlink() or getattr(info, 'st_file_attributes', 0) & 1024:
                raise ValueError('Reparse/symlink source requires separate review: ' + str(item))
        for name in sorted(names):
            item = Path(folder) / name
            relative = item.relative_to(source)
            info = item.stat()
            matched = next((pattern for pattern in defer_patterns
                if fnmatch.fnmatchcase(relative.as_posix().lower(), pattern.lower())), None)
            if matched or (info.st_size > max_bytes and defer_oversized):
                deferred.append({'relative_path': relative.as_posix(), 'bytes': info.st_size,
                    'source_mtime_ns': info.st_mtime_ns,
                    'reason': 'deferred_pattern:'+matched if matched else 'deferred_oversized'})
                continue
            if info.st_size > max_bytes and not allow_oversized:
                raise ValueError('Single file exceeds bounded batch: ' + str(item))
            entries.append({'relative_path': relative.as_posix(), 'source': str(item),
                'destination': str(destination / relative), 'bytes': info.st_size,
                'source_mtime_ns': info.st_mtime_ns, 'state': 'pending'})
    def priority(row):
        if workspace_phases:
            path = Path(row['relative_path'])
            parts = path.parts
            if row['relative_path'].startswith('data/zen-captures/'):
                phase = '4_vendor_data'
            elif path.suffix.lower() in ('.tif', '.tiff', '.czi', '.raw'):
                phase = '3_scientific_raw'
            elif not parts or parts[0] not in ('data', 'external'):
                phase = '1_code_research_configs'
            else:
                phase = '2_analyses_previews_and_resources'
            row['phase'] = phase
            return phase, row['relative_path']
        rank = next((i for i, prefix in enumerate(priority_prefixes)
            if row['relative_path'] == prefix or row['relative_path'].startswith(prefix.rstrip('/')+'/')),
            len(priority_prefixes))
        return rank, row['relative_path']
    entries.sort(key=priority)
    result = {'schema': 1, 'mode': 'copy_only', 'created_at_utc': probe.now(),
        'source_root': str(source), 'destination_root': str(destination),
        'max_batch_bytes': max_bytes, 'reserve_bytes': reserve_bytes,
        'max_batch_files': max_files, 'allow_single_oversized_file': allow_oversized,
        'file_count': len(entries), 'total_bytes': sum(row['bytes'] for row in entries),
        'mapping_sha256': mapping_digest(entries), 'files': entries, 'batches': [],
        'source_deletion_performed': False, 'active_batch': None,
        'inventory_policy': {'exclude_directory_patterns': list(exclude_directories),
            'defer_file_patterns': list(defer_patterns), 'defer_oversized': defer_oversized,
            'priority_prefixes': list(priority_prefixes), 'workspace_phases': workspace_phases},
        'excluded_directories_not_inventoried': excluded_directories,
        'deferred_files': deferred, 'deferred_bytes': sum(row['bytes'] for row in deferred)}
    write_json(ledger, result)
    return result


def validate_ledger(data):
    if mapping_digest(data['files']) != data['mapping_sha256']:
        raise ValueError('Immutable source/destination inventory changed.')
    source_root = Path(data['source_root']).resolve()
    destination_root = ensure_destination(data['destination_root'])
    for row in data['files']:
        relative = Path(row['relative_path'])
        if relative.is_absolute() or '..' in relative.parts:
            raise ValueError('Invalid relative inventory path.')
        if os.path.normcase(os.path.abspath(row['source'])) != os.path.normcase(str(source_root / relative)):
            raise ValueError('Source inventory mapping changed.')
        if os.path.normcase(os.path.abspath(row['destination'])) != os.path.normcase(str(destination_root / relative)):
            raise ValueError('Destination inventory mapping changed.')


def copy_one(row, reserve_bytes, free_bytes=None):
    free_bytes = free_bytes or (lambda: shutil.disk_usage(probe.DB.anchor).free)
    source = Path(row['source'])
    destination = ensure_destination(row['destination'])
    before = source.stat()
    if (before.st_size, before.st_mtime_ns) != (row['bytes'], row['source_mtime_ns']):
        raise ValueError('Source changed since inventory; do not silently copy a different file.')
    if destination.exists():
        expected = probe.hashes(source)
        observed = probe.hashes(destination)
        if expected['bytes'] != observed['bytes'] or expected['sha256'] != observed['sha256']:
            raise ValueError('Existing/partial destination differs; both copies retained for review.')
        return {**expected, 'reused_existing_destination': True, 'mounted_sha256': observed['sha256']}
    if free_bytes() < reserve_bytes + max(2*before.st_size, 4*MIB):
        raise OSError('Insufficient cache-volume reserve before copy.')
    destination.parent.mkdir(parents=True, exist_ok=True)
    # Never expose an incomplete canonical file. A failed staging file is kept
    # for audit, while the original destination remains absent and retryable.
    # DriveFS can still enforce MAX_PATH even when Python supports long paths.
    # Do not append the canonical basename: a valid 211-character destination
    # previously became an unopenable 261-character temporary path. Keep the
    # full UUID and same directory so publication remains an exclusive rename.
    staged = destination.with_name('.t'+uuid.uuid4().hex)
    if os.name == 'nt' and len(str(staged)) >= 260:
        raise OSError('Transfer staging path exceeds the supported DriveFS path limit: '+str(staged))
    ensure_destination(staged)
    sha, md5 = hashlib.sha256(), hashlib.md5()
    with source.open('rb') as src, staged.open('xb') as dst:
        while True:
            block = src.read(MIB)
            if not block:
                break
            if free_bytes() < reserve_bytes + 4*MIB:
                raise OSError('Cache-volume reserve reached; source retained; staged partial: '+str(staged))
            dst.write(block)
            sha.update(block)
            md5.update(block)
        dst.flush()
        os.fsync(dst.fileno())
    after = source.stat()
    if (before.st_size, before.st_mtime_ns) != (after.st_size, after.st_mtime_ns):
        raise ValueError('Source changed during copy; both copies retained.')
    observed = probe.hashes(staged)
    if observed['bytes'] != before.st_size or observed['sha256'] != sha.hexdigest():
        raise ValueError('Mounted destination readback does not match source.')
    if destination.exists():
        raise ValueError('Destination appeared during staging; both complete copies retained for review.')
    # Windows rename fails if the target appeared; it does not replace it.
    staged.rename(destination)
    return {'bytes': before.st_size, 'mtime_ns': after.st_mtime_ns,
        'sha256': sha.hexdigest(), 'md5': md5.hexdigest(),
        'mounted_sha256': observed['sha256'], 'reused_existing_destination': False,
        'commit_method': 'windows_exclusive_rename_after_complete_staging_readback'}


def copy_next(ledger, check_cancel=None):
    ledger = Path(ledger)
    with ledger_lock(ledger):
        data = read_json(ledger)
        validate_ledger(data)
        active = data.get('active_batch')
        if active:
            batch = data['batches'][active-1]
            if batch['state'] == 'client_confirmed':
                data['active_batch'] = None
            elif batch['state'] not in ('copying', 'copy_stopped'):
                return {'state': 'awaiting_client_confirmation', 'batch': active,
                    'report': batch['report']}
        if data.get('active_batch') is None:
            indices, size = [], 0
            for index, row in enumerate(data['files']):
                if row['state'] != 'pending':
                    continue
                if indices and len(indices) >= data.get('max_batch_files', 256):
                    break
                if indices and size + row['bytes'] > data['max_batch_bytes']:
                    break
                indices.append(index)
                size += row['bytes']
            if not indices:
                return {'state': 'all_files_client_confirmed', 'files': len(data['files'])}
            if shutil.disk_usage(probe.DB.anchor).free < data['reserve_bytes'] + 2*size:
                raise OSError('Insufficient cache-volume reserve for bounded batch.')
            number = len(data['batches']) + 1
            batch = {'number': number, 'indices': indices, 'bytes': size, 'state': 'copying',
                'single_oversized_file': size > data['max_batch_bytes'],
                'started_at_utc': probe.now(), 'report': str(ledger.parent / f'{ledger.stem}-batch-{number:05d}.json')}
            if size > data['max_batch_bytes'] and (len(indices) != 1 or not data.get('allow_single_oversized_file')):
                raise ValueError('Oversized batch lacks explicit single-file authorization.')
            data['batches'].append(batch)
            data['active_batch'] = number
            write_json(ledger, data)
        else:
            batch = data['batches'][data['active_batch']-1]
        try:
            for index in batch['indices']:
                if check_cancel:
                    check_cancel()
                row = data['files'][index]
                result = copy_one(row, data['reserve_bytes'])
                row.update(result, state='mounted_copy_verified', source_retained=True,
                    copied_at_utc=probe.now(), batch_number=batch['number'])
                # Keep checkpoints bounded: the immutable full-tree ledger can contain
                # tens of thousands of files. A restarted batch reuses verified copies.
                write_json(batch['report'], {'schema': 1, 'state': 'copying', 'batch': batch,
                    'files': [data['files'][i] for i in batch['indices']],
                    'source_deletion_performed': False})
        except Exception as exc:
            batch.update(state='copy_stopped', error=str(exc))
            write_json(ledger, data)
            raise
        batch.update(state='awaiting_client_confirmation', copied_at_utc=probe.now())
        write_json(batch['report'], {'schema': 1, 'state': batch['state'],
            'batch': batch, 'files': [data['files'][index] for index in batch['indices']],
            'source_deletion_performed': False})
        write_json(ledger, data)
        return {'state': batch['state'], 'batch': batch['number'], 'bytes': batch['bytes'],
            'files': len(batch['indices']), 'report': batch['report']}


def client_gate(first, second, expected, separation_seconds=10):
    """Conservative client evidence; queue must be globally empty in both views."""
    def checks(snapshot):
        item = snapshot.get('item', {})
        return {'metadata_present': snapshot.get('status') == 'cached_metadata_observed',
            'shared_drive': snapshot.get('shared_drive_id') == probe.SHARED_ID,
            'cloud_identity': bool(item.get('cloud_id')) and item.get('id') == item.get('cloud_id'),
            'size': item.get('file_size') == expected['bytes'],
            'cached_md5': snapshot.get('cached_md5_field48') == expected['md5'],
            'not_local_only': item.get('local_only') == 0,
            'not_trashed': item.get('trashed') == 0 and item.get('is_tombstone') == 0,
            'not_folder': item.get('is_folder') == 0,
            'no_pending_operations': snapshot.get('global_pending_operations') == 0}
    a, b = checks(first), checks(second)
    identity = lambda snapshot: [(entry.get('local_title'), entry.get('cloud_id'))
        for entry in snapshot.get('ancestry', [])]
    stable = bool(identity(first)) and identity(first) == identity(second)
    import datetime
    elapsed = (datetime.datetime.fromisoformat(second['observed_at_utc']) -
        datetime.datetime.fromisoformat(first['observed_at_utc'])).total_seconds()
    result = all(a.values()) and all(b.values()) and stable and elapsed >= separation_seconds
    return {'client_confirmed': result, 'independent_server_verified': False,
        'snapshot1_checks': a, 'snapshot2_checks': b, 'cloud_ancestry_stable': stable,
        'elapsed_seconds': elapsed, 'minimum_separation_seconds': separation_seconds,
        'cloud_id': second.get('item', {}).get('cloud_id'),
        'qualification': 'DriveFS cached metadata field 48 is empirically identified as MD5, not a documented public schema. Two stable snapshots and an empty local queue provide client confirmation, not independent server verification. Combine with user-visible Drive Done/sync-complete status before authorizing removal.'}


def verify_batch(ledger, separation_seconds=10):
    if not 1 <= separation_seconds <= 60:
        raise ValueError('Snapshot separation must be 1..60 seconds.')
    ledger = Path(ledger)
    with ledger_lock(ledger):
        data = read_json(ledger)
        validate_ledger(data)
        active = data.get('active_batch')
        if not active:
            return {'state': 'no_active_batch'}
        batch = data['batches'][active-1]
        if batch['state'] not in ('awaiting_client_confirmation', 'client_confirmed'):
            raise ValueError('Copy has not completed; resume it before verification.')
        rows = [data['files'][index] for index in batch['indices']]
        first = probe.metadata_snapshots(rows)
        time.sleep(separation_seconds)
        second = probe.metadata_snapshots(rows)
        evidence = []
        for row, a, b in zip(rows, first, second):
            gate = client_gate(a, b, row, separation_seconds)
            # Mounted SHA256 was computed at durable copy completion, not inferred from metadata.
            gate['mounted_readback_matches'] = row.get('mounted_sha256') == row.get('sha256')
            gate['client_confirmed'] &= gate['mounted_readback_matches']
            row['client_confirmation'] = gate
            if gate['client_confirmed']:
                row['state'] = 'client_confirmed'
            evidence.append({**row, 'metadata_snapshots': [a, b]})
        confirmed = sum(row['client_confirmation']['client_confirmed'] for row in rows)
        batch.update(state='client_confirmed' if confirmed == len(rows) else 'awaiting_client_confirmation',
            verified_at_utc=probe.now(), client_confirmed_files=confirmed)
        write_json(batch['report'], {'schema': 1, 'state': batch['state'], 'batch': batch,
            'files': evidence, 'source_deletion_performed': False,
            'independent_server_verified': False})
        write_json(ledger, data)
        return {'state': batch['state'], 'batch': active, 'files': len(rows),
            'client_confirmed_files': confirmed, 'bytes': batch['bytes'], 'report': batch['report']}


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest='command', required=True)
    p = commands.add_parser('inventory')
    p.add_argument('--source', required=True)
    p.add_argument('--destination', required=True)
    p.add_argument('--ledger', required=True)
    p.add_argument('--max-mib', type=int, default=256)
    p.add_argument('--reserve-gib', type=float, default=8)
    p.add_argument('--exclude-directory', action='append', default=[])
    p.add_argument('--defer-pattern', action='append', default=[])
    p.add_argument('--defer-oversized', action='store_true')
    p.add_argument('--priority-prefix', action='append', default=[])
    p.add_argument('--allow-oversized-files', action='store_true')
    p.add_argument('--workspace-phases', action='store_true')
    p.add_argument('--max-files', type=int, default=256)
    for name in ('copy-next', 'verify-batch'):
        p = commands.add_parser(name)
        p.add_argument('--ledger', required=True)
        if name == 'verify-batch':
            p.add_argument('--separation-seconds', type=int, default=10)
    args = parser.parse_args()
    if args.command == 'inventory':
        data = inventory(args.source, args.destination, args.ledger,
            args.max_mib*MIB, int(args.reserve_gib*GIB), args.exclude_directory,
            args.defer_pattern, args.defer_oversized, args.priority_prefix,
            args.allow_oversized_files, args.workspace_phases, args.max_files)
        result = {key: data[key] for key in ('file_count', 'total_bytes', 'mapping_sha256', 'deferred_bytes')}
        result['deferred_file_count'] = len(data['deferred_files'])
    elif args.command == 'copy-next':
        result = copy_next(args.ledger)
    else:
        result = verify_batch(args.ledger, args.separation_seconds)
    print(json.dumps(result, indent=2))
