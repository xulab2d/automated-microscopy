"""Journaled workspace transfer with explicitly enabled verified local offload.

The SQLite file is OUR transfer journal, never a Google Drive database. Progress
updates affect only a few rows; the large inventory is never rewritten. Explicit
intermediate retirement is separate from uploaded-copy offload. Conflicting
destinations are retained for review.
"""
from pathlib import Path
import argparse
import collections
import ctypes
import hashlib
import json
import os
import shutil
import sqlite3
import sys
import time

import bounded_transfer as transfer


def connect(path):
    connection = sqlite3.connect(path, timeout=2)
    connection.row_factory = sqlite3.Row
    connection.execute('PRAGMA journal_mode=WAL')
    connection.execute('PRAGMA synchronous=FULL')
    return connection


def initialize(inventory, journal):
    inventory, journal = Path(inventory).resolve(), Path(journal).resolve()
    if journal.exists():
        raise FileExistsError('Existing journal must be resumed.')
    data = transfer.read_json(inventory)
    transfer.validate_ledger(data)
    connection = connect(journal)
    connection.executescript('''
        CREATE TABLE metadata(key TEXT PRIMARY KEY, value TEXT NOT NULL);
        CREATE TABLE files(sequence INTEGER PRIMARY KEY, relative_path TEXT UNIQUE NOT NULL,
            state TEXT NOT NULL, bytes INTEGER NOT NULL, phase TEXT NOT NULL, record TEXT NOT NULL);
        CREATE INDEX file_state_order ON files(state,sequence);
        CREATE TABLE batches(id INTEGER PRIMARY KEY, state TEXT NOT NULL, record TEXT NOT NULL);
    ''')
    metadata = {key: value for key, value in data.items() if key not in ('files', 'batches')}
    metadata.update(inventory_file=str(inventory), inventory_sha256=hashlib.sha256(inventory.read_bytes()).hexdigest(),
        journal_created_at_utc=transfer.probe.now(), source_deletion_performed=False)
    with connection:
        connection.executemany('INSERT INTO metadata VALUES(?,?)',
            [(key, json.dumps(value)) for key, value in metadata.items()])
        connection.executemany('INSERT INTO files VALUES(?,?,?,?,?,?)',
            [(i, row['relative_path'], 'pending', row['bytes'], row.get('phase', 'unspecified'), json.dumps(row))
             for i, row in enumerate(data['files'])])
    connection.close()
    return {'journal': str(journal), 'file_count': len(data['files']), 'total_bytes': data['total_bytes']}


def metadata(connection):
    return {row['key']: json.loads(row['value']) for row in connection.execute('SELECT * FROM metadata')}


def append_inventory(journal, inventory):
    """Append NEW paths at priority; existing paths require a separate revision plan."""
    journal, inventory = Path(journal).resolve(), Path(inventory).resolve()
    data = transfer.read_json(inventory)
    transfer.validate_ledger(data)
    with transfer.ledger_lock(journal.parent/'worker-owner.json'):
        connection = connect(journal)
        info = metadata(connection)
        source_root, destination_root = Path(info['source_root']), Path(info['destination_root'])
        additions, existing, excluded = [], [], []
        for row in data['files']:
            relative = Path(row['source']).relative_to(source_root)
            if '__pycache__' in relative.parts or Path(row['source']).suffix == '.pyc':
                excluded.append(relative.as_posix())
                continue
            if Path(row['destination']) != destination_root/relative:
                raise ValueError('Delta destination does not preserve project-relative path.')
            old = connection.execute('SELECT state,record FROM files WHERE relative_path=?', (relative.as_posix(),)).fetchone()
            if old:
                existing.append(relative.as_posix())
                continue
            additions.append({**row, 'relative_path': relative.as_posix(), 'phase': '0_priority_delta',
                'delta_inventory': str(inventory), 'state': 'pending'})
        first = connection.execute('SELECT coalesce(min(sequence),0) FROM files').fetchone()[0]-len(additions)
        transition = {'recorded_at_utc': transfer.probe.now(), 'inventory': str(inventory),
            'inventory_sha256': hashlib.sha256(inventory.read_bytes()).hexdigest(),
            'files_added': len(additions), 'bytes_added': sum(row['bytes'] for row in additions),
            'existing_paths_not_overwritten': existing, 'excluded_runtime_cache_paths': excluded}
        history = info.get('delta_inventories', [])+[transition]
        with connection:
            connection.executemany('INSERT INTO files VALUES(?,?,?,?,?,?)',
                [(first+i, row['relative_path'], 'pending', row['bytes'], row['phase'], json.dumps(row))
                 for i, row in enumerate(additions)])
            connection.execute('INSERT OR REPLACE INTO metadata VALUES(?,?)', ('delta_inventories', json.dumps(history)))
        connection.close()
        transfer.write_json(inventory.parent/'journal-import.json', transition)
        return transition


def summary(connection):
    return {row['state']: {'files': row['n'], 'bytes': row['size'] or 0}
        for row in connection.execute('SELECT state,count(*) AS n,sum(bytes) AS size FROM files GROUP BY state')}


def store_file(connection, sequence, record):
    with connection:
        connection.execute('UPDATE files SET state=?,record=? WHERE sequence=?',
            (record['state'], json.dumps(record), sequence))


def store_batch(connection, batch):
    with connection:
        connection.execute('INSERT INTO batches VALUES(?,?,?) ON CONFLICT(id) DO UPDATE SET state=excluded.state,record=excluded.record',
            (batch['number'], batch['state'], json.dumps(batch)))


def batch_rows(connection, batch):
    return [json.loads(connection.execute('SELECT record FROM files WHERE sequence=?', (i,)).fetchone()[0])
        for i in batch['indices']]


def save_report(connection, batch, evidence=None):
    rows=evidence if evidence is not None else batch_rows(connection,batch)
    transfer.write_json(batch['report'], {'schema': 2, 'state': batch['state'], 'batch': batch,
        'files': rows,
        'source_deletion_performed':any(row.get('source_retained') is False for row in rows),
        'intentional_retirement_present':any(row['state']=='retired_intentionally' for row in rows),
        'independent_server_verified': False})


def select_batch(connection, info, folder, allow_new=False, remaining_bytes=None):
    active = connection.execute("SELECT record FROM batches WHERE state!='client_confirmed' ORDER BY id DESC LIMIT 1").fetchone()
    if active and not allow_new:
        return json.loads(active[0])
    indices, size = [], 0
    for row in connection.execute("SELECT sequence,bytes FROM files WHERE state='pending' ORDER BY sequence"):
        if indices and (len(indices) >= info.get('max_batch_files', 256) or size+row['bytes'] > info['max_batch_bytes']):
            break
        indices.append(row['sequence'])
        size += row['bytes']
    if not indices:
        return None
    if remaining_bytes is not None and size > remaining_bytes:
        return None
    if size > info['max_batch_bytes'] and (len(indices) != 1 or not info.get('allow_single_oversized_file')):
        raise ValueError('Oversized batch lacks explicit single-file authorization.')
    if shutil.disk_usage(transfer.probe.DB.anchor).free < info['reserve_bytes']+2*size:
        raise OSError('Insufficient cache-volume reserve before batch.')
    number = connection.execute('SELECT coalesce(max(id),0)+1 FROM batches').fetchone()[0]
    batch = {'number': number, 'state': 'copying', 'indices': indices, 'bytes': size,
        'single_oversized_file': size > info['max_batch_bytes'], 'started_at_utc': transfer.probe.now(),
        'report': str(folder/f'batch-{number:05d}.json')}
    store_batch(connection, batch)
    return batch


def stage_batch(connection, batch, info, check_cancel):
    if batch['state'] == 'awaiting_client_confirmation':
        return
    batch.update(state='copying')
    store_batch(connection, batch)
    for sequence in batch['indices']:
        check_cancel()
        row = json.loads(connection.execute('SELECT record FROM files WHERE sequence=?', (sequence,)).fetchone()[0])
        if row['state'] in ('mounted_copy_verified', 'content_matched', 'client_confirmed', 'source_changed', 'source_missing', 'retired_intentionally'):
            continue
        source = Path(row['source'])
        if not source.exists():
            row.update(state='source_missing', error='Source absent; not assumed transferred.', observed_at_utc=transfer.probe.now())
            store_file(connection, sequence, row)
            continue
        current = source.stat()
        if (current.st_size, current.st_mtime_ns) != (row['bytes'], row['source_mtime_ns']):
            row.update(state='source_changed', error='Source differs from immutable inventory; requires recorded refresh.',
                observed_bytes=current.st_size, observed_mtime_ns=current.st_mtime_ns, observed_at_utc=transfer.probe.now())
            store_file(connection, sequence, row)
            continue
        result = transfer.copy_one(row, info['reserve_bytes'])
        row.update(result, state='mounted_copy_verified', source_retained=True,
            copied_at_utc=transfer.probe.now(), batch_number=batch['number'])
        store_file(connection, sequence, row)
    batch.update(state='awaiting_client_confirmation', copied_at_utc=transfer.probe.now())
    store_batch(connection, batch)
    save_report(connection, batch)


def verify_batch(connection, batch, separation_seconds=10):
    rows = batch_rows(connection, batch)
    eligible = [row for row in rows if row['state'] in ('mounted_copy_verified', 'client_confirmed')]
    first = transfer.probe.metadata_snapshots(eligible)
    time.sleep(separation_seconds)
    second = transfer.probe.metadata_snapshots(eligible)
    evidence, confirmed = [], 0
    by_path = {row['relative_path']: sequence for row, sequence in zip(rows, batch['indices'])}
    for row, a, b in zip(eligible, first, second):
        gate = transfer.client_gate(a, b, row, separation_seconds)
        gate['mounted_readback_matches'] = row.get('mounted_sha256') == row.get('sha256')
        gate['client_confirmed'] &= gate['mounted_readback_matches']
        row['client_confirmation'] = gate
        if gate['client_confirmed']:
            row['state'] = 'client_confirmed'
            confirmed += 1
        store_file(connection, by_path[row['relative_path']], row)
        evidence.append({**row, 'metadata_snapshots': [a, b]})
    evidence.extend(row for row in rows if row['state'] not in ('mounted_copy_verified', 'client_confirmed'))
    batch.update(state='client_confirmed' if confirmed == len(eligible) else 'awaiting_client_confirmation',
        checked_at_utc=transfer.probe.now(), client_confirmed_files=confirmed,
        deferred_source_files=len(rows)-len(eligible))
    store_batch(connection, batch)
    save_report(connection, batch, evidence)
    return batch['state'] == 'client_confirmed'


def verify_group(connection, batches, separation_seconds=10):
    """Queue drain is global; collect one shared snapshot, avoiding futile sleeps."""
    all_rows = [(batch, sequence, row) for batch in batches
        for sequence, row in zip(batch['indices'], batch_rows(connection, batch))]
    eligible = [(batch, sequence, row) for batch, sequence, row in all_rows
        if row['state'] in ('mounted_copy_verified', 'content_matched', 'client_confirmed')]
    first = transfer.probe.metadata_snapshots([row for _, _, row in eligible])
    pending = max((snapshot.get('global_pending_operations', 0) for snapshot in first), default=0)
    matching = sum(snapshot.get('cached_md5_matches') and snapshot.get('size_matches')
        and snapshot.get('item', {}).get('local_only') == 0 for snapshot in first)
    time.sleep(separation_seconds)
    second = transfer.probe.metadata_snapshots([row for _, _, row in eligible])
    evidence = collections.defaultdict(list)
    with connection:
        for (batch, sequence, row), a, b in zip(eligible, first, second):
            gate = transfer.client_gate(a, b, row, separation_seconds)
            gate['mounted_readback_matches'] = row.get('mounted_sha256') == row.get('sha256')
            gate['client_confirmed'] &= gate['mounted_readback_matches']
            content_matches = all(value for key, value in gate['snapshot1_checks'].items() if key != 'no_pending_operations')
            content_matches &= all(value for key, value in gate['snapshot2_checks'].items() if key != 'no_pending_operations')
            content_matches &= gate['cloud_ancestry_stable'] and gate['elapsed_seconds'] >= separation_seconds and gate['mounted_readback_matches']
            gate['copy_only_content_matched'] = bool(content_matches)
            gate['source_removal_authorized'] = False
            row['client_confirmation'] = gate
            if gate['client_confirmed']:
                row['state'] = 'client_confirmed'
            elif content_matches:
                row['state'] = 'content_matched'
            connection.execute('UPDATE files SET state=?,record=? WHERE sequence=?',
                (row['state'], json.dumps(row), sequence))
            evidence[batch['number']].append({**row, 'metadata_snapshots': [a, b]})
    for batch, _, row in all_rows:
        if row['state'] not in ('mounted_copy_verified', 'content_matched', 'client_confirmed'):
            evidence[batch['number']].append(row)
    confirmed_batches, content_batches = 0, 0
    for batch in batches:
        rows = evidence[batch['number']]
        counted = [row for row in rows if row['state'] in ('mounted_copy_verified', 'content_matched', 'client_confirmed')]
        confirmed = sum(row['state'] == 'client_confirmed' for row in counted)
        matched = sum(row['state'] in ('content_matched', 'client_confirmed') for row in counted)
        done = confirmed == len(counted)
        content_done = matched == len(counted)
        batch.update(state='client_confirmed' if done else 'content_matched' if content_done else 'awaiting_client_confirmation',
            checked_at_utc=transfer.probe.now(), client_confirmed_files=confirmed,
            copy_only_content_matched_files=matched,
            intentionally_retired_files=sum(row['state']=='retired_intentionally' for row in rows),
            deferred_source_files=sum(row['state'] not in ('mounted_copy_verified','content_matched','client_confirmed','retired_intentionally') for row in rows))
        confirmed_batches += done
        content_batches += content_done
        store_batch(connection, batch)
        save_report(connection, batch, rows)
    return {'confirmed_batches': confirmed_batches, 'content_matched_batches': content_batches, 'queued_files': len(eligible),
        'cached_content_matches': matching,
        'global_pending_operations': max((s.get('global_pending_operations', 0) for s in second), default=0)}


def run_pipeline(journal, stop_file, max_files=4096, max_outstanding_mib=1024,
                 reserve_wait_minutes=10, upload_wait_minutes=120, offload_eligible=False,
                 offload_target_free_gib=18, offload_max_total_gib=15):
    journal, stop_file = Path(journal).resolve(), Path(stop_file).resolve()
    if max_files < 1 or max_outstanding_mib < 256:
        raise ValueError('Invalid bounded pipeline limits.')
    with transfer.ledger_lock(journal.parent/'worker-owner.json'):
        connection = connect(journal)
        info = metadata(connection)
        old_max_files = info.get('max_batch_files', 256)
        info['max_batch_files'] = max_files
        policy = {'recorded_at_utc': transfer.probe.now(), 'previous_max_files': old_max_files,
            'max_files_per_batch': max_files, 'max_batch_bytes': info['max_batch_bytes'],
            'max_outstanding_bytes': max_outstanding_mib*transfer.MIB, 'max_tracked_incomplete_batches': 16,
                'source_retention': True, 'confirmation_gate': 'unchanged_two_snapshots_and_empty_global_queue',
            'copy_ahead_release_gate': 'two_snapshot_owned_file_content_match_without_claiming_global_completion',
            'source_removal_authorized': False}
        with connection:
            connection.execute('INSERT OR REPLACE INTO metadata VALUES(?,?)', ('pipeline_policy', json.dumps(policy)))
        transfer.write_json(journal.parent/'pipeline-policy.json', policy)
        if offload_eligible:
            policy['local_offload']={'enabled':True,'allowed_continuation_prefix':'data/',
                'protected_prefixes':['data/whole-chip-standardized-01/','flakefinder/'],
                'mode':'routine_after_strong_confirmation_not_only_low_disk',
                'max_cumulative_offload_bytes':int(offload_max_total_gib*transfer.GIB),
                'gate':'matching local SHA to mounted readback plus two fresh cloud MD5/identity/size snapshots with quiet queue',
                'cloud_copies_retained':True,'local_sources_may_be_removed_with_per_file_receipts':True}
            policy['source_retention']='current campaign and active dependencies retained; other verified uploaded local data routinely offloaded'
            policy['source_removal_authorized']=True
            with connection:
                connection.execute('INSERT OR REPLACE INTO metadata VALUES(?,?)',('pipeline_policy',json.dumps(policy)))
            transfer.write_json(journal.parent/'pipeline-policy.json',policy)
        started, disk_wait, upload_wait = transfer.probe.now(), None, None
        def check_cancel():
            if stop_file.exists():
                raise transfer.RelocationStopped('Relocation STOP requested; originals retained.')
        def state(value, **details):
            offloaded=connection.execute("SELECT count(*),coalesce(sum(bytes),0) FROM files WHERE json_extract(record,'$.source_offload.state')='completed' AND json_extract(record,'$.source_retained')=0").fetchone()
            retired=summary(connection).get('retired_intentionally',{'files':0,'bytes':0})
            output = {'state': value, 'pid': os.getpid(), 'started_at_utc': started,
                'updated_at_utc': transfer.probe.now(), 'journal': str(journal), 'stop_file': str(stop_file),
                'progress': summary(connection), 'source_deletion_performed': bool(offloaded[0] or retired['files']),
                'all_source_originals_retained':not bool(offloaded[0] or retired['files']),'local_copies_offloaded':{'files':offloaded[0],'bytes':offloaded[1]},
                'intentionally_retired_intermediates':retired,
                'all_cloud_copies_preserved':not bool(retired['files']),'retained_scope_cloud_copies_preserved':True,'current_final_scientific_inputs_preserved':True,
                'free_cache_volume_bytes': shutil.disk_usage(transfer.probe.DB.anchor).free, **details}
            transfer.write_json(journal.parent/'worker-state.json', output)
            print(json.dumps(output), flush=True)
            from publish_transfer_status import publish_if_changed
            publish_if_changed(info, journal.parent, output)
        def offload_checkpoint():
            if not offload_eligible:
                return False
            removed=connection.execute("SELECT coalesce(sum(bytes),0) FROM files WHERE json_extract(record,'$.source_offload.state')='completed' AND json_extract(record,'$.source_retained')=0").fetchone()[0]
            remaining=int(offload_max_total_gib*transfer.GIB)-removed
            if remaining<=0:return False
            from offload_verified import run as offload
            result=offload(journal,review_files=256,include_initial_vendor=False,quiet_wait_seconds=30,
                scope='inactive_data',max_bytes=min(512*transfer.MIB,remaining),include_mounted=False,_connection=connection)
            state('uploaded_local_copies_offloaded' if result.get('bytes_removed',0) else 'offload_waiting_for_verification',offload=result)
            return bool(result.get('bytes_removed',0))
        try:
            if sys.platform == 'win32':
                ctypes.windll.kernel32.SetPriorityClass(ctypes.windll.kernel32.GetCurrentProcess(), 0x4000)
            state('running', policy=policy)
            while True:
                check_cancel()
                batches = [json.loads(row[0]) for row in connection.execute(
                    "SELECT record FROM batches WHERE state IN ('copying','awaiting_client_confirmation') ORDER BY id")]
                for batch in batches:
                    if batch['state'] != 'awaiting_client_confirmation':
                        stage_batch(connection, batch, info, check_cancel)
                reserve_error = None
                while len(batches) < 16:
                    check_cancel()
                    outstanding = sum(row['bytes'] for batch in batches for row in batch_rows(connection, batch)
                        if row['state'] not in ('content_matched', 'client_confirmed', 'source_changed', 'source_missing', 'retired_intentionally'))
                    allowance = max_outstanding_mib*transfer.MIB-outstanding
                    if allowance <= 0:
                        break
                    try:
                        batch = select_batch(connection, info, journal.parent, allow_new=True,
                            remaining_bytes=allowance if batches else None)
                        if batch is None:
                            break
                        state('copying', batch=batch['number'], outstanding_bytes=outstanding)
                        stage_batch(connection, batch, info, check_cancel)
                        batches.append(batch)
                        disk_wait = None
                    except OSError as exc:
                        if 'reserve' not in str(exc).lower():
                            raise
                        reserve_error = str(exc)
                        disk_wait = disk_wait or time.monotonic()
                        break
                if not batches:
                    if reserve_error:
                        state('waiting_for_disk_reserve', error=reserve_error)
                        if time.monotonic()-disk_wait >= reserve_wait_minutes*60:
                            state('blocked_disk_reserve', error=reserve_error)
                            return 2
                        time.sleep(15)
                        continue
                    deferred_audit = [json.loads(row[0]) for row in connection.execute(
                        "SELECT record FROM batches WHERE state='content_matched' ORDER BY id LIMIT 4")]
                    if deferred_audit:
                        upload_wait = upload_wait or time.monotonic()
                        result = verify_group(connection, deferred_audit)
                        state('finalizing_upload_audit', upload=result)
                        if result['confirmed_batches']:
                            upload_wait = None
                            offload_checkpoint()
                        elif time.monotonic()-upload_wait >= upload_wait_minutes*60:
                            state('blocked_upload_confirmation', upload=result)
                            return 3
                        else:
                            time.sleep(15)
                        continue
                    states = summary(connection)
                    if offload_checkpoint():
                        continue
                    refresh = any(states.get(key, {}).get('files', 0) for key in ('source_changed', 'source_missing'))
                    state('complete_refresh_required' if refresh else 'complete')
                    return 4 if refresh else 0
                upload_wait = upload_wait or time.monotonic()
                check_cancel()
                result = verify_group(connection, batches)
                if result['confirmed_batches']:
                    offload_checkpoint()
                state('batch_client_confirmed' if result['confirmed_batches'] else 'content_matched_copy_only' if result['content_matched_batches'] else 'uploading',
                    upload=result, outstanding_batches=[batch['number'] for batch in batches],
                    outstanding_bytes=sum(row['bytes'] for batch in batches for row in batch_rows(connection, batch)
                        if row['state'] not in ('content_matched', 'client_confirmed', 'source_changed', 'source_missing', 'retired_intentionally')), reserve_error=reserve_error)
                if result['content_matched_batches']:
                    upload_wait = None
                    continue
                if time.monotonic()-upload_wait >= upload_wait_minutes*60:
                    state('blocked_upload_confirmation', upload=result)
                    return 3
                time.sleep(15)
        except transfer.RelocationStopped as exc:
            state('stopped', error=str(exc))
            return 0
        except OSError as exc:
            if 'reserve' in str(exc).lower():
                state('blocked_disk_reserve', error=str(exc))
                return 2
            state('failed', error=repr(exc))
            raise
        except Exception as exc:
            state('failed', error=repr(exc))
            raise
        finally:
            connection.close()


def run(journal, stop_file, max_batches=None, reserve_wait_minutes=10, upload_wait_minutes=30):
    journal, stop_file = Path(journal).resolve(), Path(stop_file).resolve()
    with transfer.ledger_lock(journal.parent/'worker-owner.json'):
        connection = connect(journal)
        info = metadata(connection)
        started, completed, disk_wait = transfer.probe.now(), 0, None
        def state(value, **details):
            output = {'state': value, 'pid': os.getpid(), 'started_at_utc': started,
                'updated_at_utc': transfer.probe.now(), 'journal': str(journal), 'stop_file': str(stop_file),
                'progress': summary(connection), 'source_deletion_performed': False,
                'free_cache_volume_bytes': shutil.disk_usage(transfer.probe.DB.anchor).free, **details}
            transfer.write_json(journal.parent/'worker-state.json', output)
            print(json.dumps(output), flush=True)
        def check_cancel():
            if stop_file.exists():
                raise transfer.RelocationStopped('Relocation STOP requested; originals retained.')
        try:
            if sys.platform == 'win32':
                ctypes.windll.kernel32.SetPriorityClass(ctypes.windll.kernel32.GetCurrentProcess(), 0x4000)
            state('running')
            while max_batches is None or completed < max_batches:
                check_cancel()
                try:
                    batch = select_batch(connection, info, journal.parent)
                    if batch is None:
                        states = summary(connection)
                        pending_refresh = any(states.get(key, {}).get('files', 0) for key in ('source_changed', 'source_missing'))
                        state('complete_refresh_required' if pending_refresh else 'complete')
                        return 4 if pending_refresh else 0
                    state('copying', batch=batch['number'])
                    stage_batch(connection, batch, info, check_cancel)
                    disk_wait = None
                except OSError as exc:
                    if 'reserve' not in str(exc).lower():
                        raise
                    disk_wait = disk_wait or time.monotonic()
                    state('waiting_for_disk_reserve', error=str(exc))
                    if time.monotonic()-disk_wait >= reserve_wait_minutes*60:
                        state('blocked_disk_reserve', error=str(exc))
                        return 2
                    time.sleep(15)
                    continue
                state('waiting_for_client_confirmation', batch=batch['number'])
                upload_started = time.monotonic()
                while True:
                    check_cancel()
                    if verify_batch(connection, batch):
                        state('batch_client_confirmed', batch=batch['number'])
                        completed += 1
                        break
                    if time.monotonic()-upload_started >= upload_wait_minutes*60:
                        state('blocked_upload_confirmation', batch=batch['number'])
                        return 3
                    time.sleep(15)
            state('bounded_run_complete')
            return 0
        except transfer.RelocationStopped as exc:
            state('stopped', error=str(exc))
            return 0
        except Exception as exc:
            state('failed', error=repr(exc))
            raise
        finally:
            connection.close()


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest='command', required=True)
    p = sub.add_parser('initialize')
    p.add_argument('--inventory', required=True)
    p.add_argument('--journal', required=True)
    p = sub.add_parser('append-inventory')
    p.add_argument('--inventory', required=True)
    p.add_argument('--journal', required=True)
    p = sub.add_parser('run')
    p.add_argument('--journal', required=True)
    p.add_argument('--stop-file', required=True)
    p.add_argument('--max-batches', type=int)
    p.add_argument('--reserve-wait-minutes', type=float, default=10)
    p.add_argument('--upload-wait-minutes', type=float, default=30)
    p = sub.add_parser('pipeline')
    p.add_argument('--journal', required=True)
    p.add_argument('--stop-file', required=True)
    p.add_argument('--max-files', type=int, default=4096)
    p.add_argument('--max-outstanding-mib', type=int, default=1024)
    p.add_argument('--reserve-wait-minutes', type=float, default=10)
    p.add_argument('--upload-wait-minutes', type=float, default=120)
    p.add_argument('--offload-eligible', action='store_true')
    p.add_argument('--offload-target-free-gib',type=float,default=18)
    p.add_argument('--offload-max-total-gib',type=float,default=15)
    args = parser.parse_args()
    if args.command == 'initialize':
        print(json.dumps(initialize(args.inventory, args.journal), indent=2))
    elif args.command == 'append-inventory':
        print(json.dumps(append_inventory(args.journal, args.inventory), indent=2))
    elif args.command == 'run':
        raise SystemExit(run(args.journal, args.stop_file, args.max_batches,
            args.reserve_wait_minutes, args.upload_wait_minutes))
    else:
        raise SystemExit(run_pipeline(args.journal, args.stop_file, args.max_files,
            args.max_outstanding_mib, args.reserve_wait_minutes, args.upload_wait_minutes,
            args.offload_eligible,args.offload_target_free_gib,args.offload_max_total_gib))
