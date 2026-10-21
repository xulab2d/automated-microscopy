"""Execute the explicitly approved, hash-pinned intermediate discard manifest.

This is intentional evidence retirement, NOT uploaded-copy offload. No recursive
deletion, directory globs, Drive hydration, or acquisition-manifest rewriting.
"""
from pathlib import Path
import argparse
import hashlib
import json
import os
import shutil

import bounded_transfer as transfer
import journal_transfer as journal

APPROVED_SHA = '4ef7c5e077e0bc98bbdacb8ef456353ef33f89d2533714937b9b61e3675f2354'
ROOT = Path(r'C:\Users\STM\Documents\ChatGPT\Automated Microscopy')
DRIVE = Path(r'G:\Shared drives\Xu Lab - Microscopy and Instruments\Automated Microscopy')
MANIFEST = DRIVE/'research/storage-prune-audit-01/prune-manifest.json'
FOLDER = ROOT/'research/drive-relocation/prune-state/approved-20261009'
CLOUD = DRIVE/'transfer-evidence/intermediate-retirement-20261009'
JOURNAL = ROOT/'research/drive-relocation/workspace-state/progress.sqlite'
STOP = ROOT/'research/drive-relocation/STOP'


def digest(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def checked_path(value, root, relative, must_exist=True):
    path = Path(value)
    expected = root/relative
    if path != expected or '..' in Path(relative).parts or Path(relative).is_absolute():
        raise ValueError('Path does not match the approved root/relative mapping: '+str(path))
    resolved = path.resolve()
    if resolved != expected.absolute() or not resolved.is_relative_to(root.resolve()):
        raise ValueError('Path escaped approved root: '+str(path))
    # Check every path component, including the leaf. DriveFS virtual files are
    # allowed only if they are ordinary filesystem entries, never reparse links.
    cursor = path
    while cursor != root.parent:
        if cursor.exists() and (cursor.is_symlink() or getattr(cursor.lstat(), 'st_file_attributes', 0) & 1024):
            raise ValueError('Reparse path rejected: '+str(cursor))
        cursor = cursor.parent
    if must_exist and not path.is_file():
        raise ValueError('Expected ordinary file: '+str(path))
    return path


def check_stop():
    if STOP.exists():
        raise InterruptedError('Transfer STOP requested.')


def append_receipt(stream, value):
    stream.write(json.dumps(value)+'\n'); stream.flush(); os.fsync(stream.fileno())


def validate():
    if digest(MANIFEST) != APPROVED_SHA:
        raise ValueError('Approved manifest changed.')
    manifest = transfer.read_json(MANIFEST)
    summary = manifest['summary']
    if Path(summary['source_root']) != ROOT or Path(summary['drive_root']) != DRIVE:
        raise ValueError('Manifest roots changed.')
    plan_path = Path(summary['active_analysis_plan'])
    if digest(plan_path) != summary['active_analysis_plan_sha256']:
        raise ValueError('Active analysis plan changed.')
    plan = transfer.read_json(plan_path)
    protected = set()
    def paths(value):
        if isinstance(value, dict):
            for key, item in value.items():
                paths(key); paths(item)
        elif isinstance(value, list):
            for item in value: paths(item)
        elif isinstance(value, str) and (value.startswith(str(ROOT)) or value.startswith(str(DRIVE))):
            protected.add(str(Path(value)).lower())
    paths(plan)
    for item in manifest.get('retained_or_skipped', []): paths(item)
    seen = set()
    for row in manifest['files']:
        check_stop()
        relative = row['relative_path']
        if relative in seen: raise ValueError('Duplicate manifest path.')
        seen.add(relative)
        local = checked_path(row['local_path'], ROOT, relative)
        # Cloud filesystem checks belong to the later Drive deletion phase;
        # avoid thousands of remote stats delaying local space reclamation.
        if Path(row['drive_path']) != DRIVE/relative:
            raise ValueError('Drive mapping differs from approved relative path.')
        if str(local).lower() in protected or str(Path(row['drive_path'])).lower() in protected:
            raise ValueError('Active dependency or regression fixture in discard manifest: '+relative)
        category = row['category']
        if category == 'zen_vendor_czi':
            if not relative.startswith('data/zen-captures/') or local.suffix.lower() != '.czi':
                raise ValueError('Invalid CZI scope.')
        elif category in ('discovery_routing_tiff', 'discovery_support_tiff', 'search_routing_tiff'):
            if not relative.startswith(('data/whole-chip-standardized-01/discovery/', 'data/whole-chip-standardized-01/search/')) or local.suffix.lower() != '.tiff':
                raise ValueError('Invalid routing TIFF scope.')
        else: raise ValueError('Unapproved category.')
        stat = local.stat()
        if (stat.st_size, stat.st_mtime_ns) != (row['bytes'], row['mtime_ns']):
            raise ValueError('Local source changed: '+relative)
        evidence = row['evidence']
        if category == 'zen_vendor_czi':
            if digest(evidence['vendor_metadata']) != evidence['vendor_metadata_sha256']:
                raise ValueError('Vendor metadata changed: '+relative)
            if not evidence['job_files'] or not all(Path(p).is_file() for p in evidence['job_files']):
                raise ValueError('Missing retained vendor job.')
        else:
            if not Path(evidence['retained_preview']).is_file() or digest(evidence['retained_sidecar']) != evidence['sidecar_sha256']:
                raise ValueError('Missing or changed retained routing evidence: '+relative)
    if len(seen) != 5896 or sum(row['bytes'] for row in manifest['files']) != 166047497198:
        raise ValueError('Approved counts changed.')
    return manifest


def local_phase():
    with transfer.ledger_lock(JOURNAL.parent/'worker-owner.json'):
        if (FOLDER/'local-result.json').exists():
            return transfer.read_json(FOLDER/'local-result.json')
        manifest = validate()
        connection = journal.connect(JOURNAL)
        retirements = []
        for row in manifest['files']:
            saved = connection.execute('SELECT sequence,record FROM files WHERE relative_path=?', (row['relative_path'],)).fetchone()
            if saved is None: raise ValueError('Manifest path absent from transfer journal.')
            old = json.loads(saved['record'])
            if (old['bytes'], old['source_mtime_ns']) != (row['bytes'], row['mtime_ns']):
                raise ValueError('Manifest/journal source mismatch: '+row['relative_path'])
            retirements.append({'sequence': saved['sequence'], 'approved': row, 'previous_transfer_record': old})
        FOLDER.mkdir(parents=True, exist_ok=True); CLOUD.mkdir(parents=True, exist_ok=True)
        receipt = {'schema': 1, 'approved_manifest_sha256': APPROVED_SHA,
            'approved_manifest': str(MANIFEST), 'recorded_at_utc': transfer.probe.now(),
            'authorization': 'Explicit user request to discard unneeded high-resolution intermediates; exact manifest approved by parent on 2026-10-09.',
            'semantics': 'Intentional local and cloud intermediate discard. Not all files are duplicates. Original acquisition manifests remain unchanged; removed raw evidence cannot be reclassified or used to resume old discovery.',
            'active_analysis_plan_sha256': manifest['summary']['active_analysis_plan_sha256'],
            'files': retirements}
        transfer.write_json(FOLDER/'retirement-plan.json', receipt)
        transfer.write_json(CLOUD/'retirement-plan.json', receipt)
        if digest(FOLDER/'retirement-plan.json') != digest(CLOUD/'retirement-plan.json'):
            raise ValueError('Durable cloud plan readback differs.')
        with connection:
            for item in retirements:
                record = dict(item['previous_transfer_record'])
                record.update(state='retired_intentionally', intermediate_retirement={
                    'state': 'prepared', 'approved_manifest_sha256': APPROVED_SHA,
                    'plan': str(FOLDER/'retirement-plan.json'), 'cloud_plan': str(CLOUD/'retirement-plan.json'),
                    'previous_state': record['state'], 'prepared_at_utc': transfer.probe.now()})
                connection.execute('UPDATE files SET state=?,record=? WHERE sequence=?', ('retired_intentionally', json.dumps(record), item['sequence']))
            connection.execute('INSERT OR REPLACE INTO metadata VALUES(?,?)', ('intentional_intermediate_retirement', json.dumps({k:v for k,v in receipt.items() if k != 'files'})))
        result = {'state': 'local_removal_in_progress', 'files_removed': 0, 'bytes_removed': 0, 'failures': [], 'started_at_utc': transfer.probe.now()}
        with (FOLDER/'local-removals.jsonl').open('a', encoding='utf-8') as stream:
            for item in retirements:
                check_stop(); row = item['approved']
                path = checked_path(row['local_path'], ROOT, row['relative_path'])
                stat = path.stat()
                if (stat.st_size, stat.st_mtime_ns) != (row['bytes'], row['mtime_ns']):
                    raise ValueError('Source changed immediately before removal: '+str(path))
                append_receipt(stream, {'state': 'remove_prepared', 'relative_path': row['relative_path'], 'bytes': row['bytes'], 'at_utc': transfer.probe.now()})
                path.unlink()
                append_receipt(stream, {'state': 'removed', 'relative_path': row['relative_path'], 'bytes': row['bytes'], 'at_utc': transfer.probe.now()})
                record = json.loads(connection.execute('SELECT record FROM files WHERE sequence=?', (item['sequence'],)).fetchone()[0])
                record['intermediate_retirement'].update(state='local_removed', local_removed_at_utc=transfer.probe.now())
                record['source_retained'] = False
                journal.store_file(connection, item['sequence'], record)
                result['files_removed'] += 1; result['bytes_removed'] += row['bytes']
                if result['files_removed'] % 100 == 0:
                    result['free_cache_volume_bytes'] = shutil.disk_usage(ROOT.anchor).free
                    transfer.write_json(FOLDER/'local-progress.json', result)
                    print(json.dumps({k:v for k,v in result.items() if k != 'failures'}), flush=True)
        result.update(state='local_complete', completed_at_utc=transfer.probe.now(), free_cache_volume_bytes=shutil.disk_usage(ROOT.anchor).free)
        transfer.write_json(FOLDER/'local-result.json', result)
        transfer.write_json(CLOUD/'local-result.json', result)
        shutil.copyfile(FOLDER/'local-removals.jsonl', CLOUD/'local-removals.jsonl')
        connection.close()
        return result


def drive_phase():
    with transfer.ledger_lock(JOURNAL.parent/'worker-owner.json'):
        receipt = transfer.read_json(FOLDER/'retirement-plan.json')
        if receipt['approved_manifest_sha256'] != APPROVED_SHA or digest(MANIFEST) != APPROVED_SHA:
            raise ValueError('Retirement plan changed.')
        connection = journal.connect(JOURNAL)
        result = {'state': 'drive_removal_in_progress', 'removed': 0, 'bytes_removed': 0, 'absent': 0, 'skipped': [], 'started_at_utc': transfer.probe.now()}
        # One cached-metadata transaction, no reading raw Drive contents.
        expected = [dict(item['previous_transfer_record'], md5=item['previous_transfer_record'].get('md5', '')) for item in receipt['files']]
        snapshots = transfer.probe.metadata_snapshots(expected)
        transfer.write_json(FOLDER/'drive-preflight.json', {'observed_at_utc': transfer.probe.now(), 'files': [{'relative_path': r['relative_path'], 'snapshot': s} for r,s in zip(expected,snapshots)]})
        with (FOLDER/'drive-removals.jsonl').open('a', encoding='utf-8') as stream:
            for item, snapshot in zip(receipt['files'], snapshots):
                check_stop(); row=item['approved']; old=item['previous_transfer_record']
                path=Path(row['drive_path'])
                if path != DRIVE/row['relative_path']:
                    raise ValueError('Drive path mapping changed.')
                if not path.exists():
                    result['absent'] += 1; continue
                path=checked_path(row['drive_path'], DRIVE, row['relative_path'])
                stat=path.stat(); remote=snapshot.get('item', {})
                checks={'size':stat.st_size==row['bytes'], 'journal_hash':bool(old.get('sha256')) and old.get('sha256')==old.get('mounted_sha256'),
                    'cached_md5':bool(old.get('md5')) and snapshot.get('cached_md5_field48')==old.get('md5'),
                    'metadata_size':remote.get('file_size')==row['bytes'], 'cloud_identity':bool(remote.get('cloud_id')) and remote.get('id')==remote.get('cloud_id'),
                    'same_recorded_identity':remote.get('cloud_id')==old.get('client_confirmation',{}).get('cloud_id'),
                    'not_folder':remote.get('is_folder')==0, 'not_deleted':remote.get('trashed')==0 and remote.get('is_tombstone')==0}
                if not all(checks.values()):
                    result['skipped'].append({'relative_path':row['relative_path'],'checks':checks});continue
                append_receipt(stream, {'state':'remove_prepared','relative_path':row['relative_path'],'cloud_id':remote['cloud_id'],'checks':checks,'at_utc':transfer.probe.now()})
                path.unlink()
                append_receipt(stream, {'state':'removed','relative_path':row['relative_path'],'cloud_id':remote['cloud_id'],'bytes':row['bytes'],'at_utc':transfer.probe.now()})
                record=json.loads(connection.execute('SELECT record FROM files WHERE sequence=?',(item['sequence'],)).fetchone()[0])
                record['intermediate_retirement'].update(state='local_and_drive_removed',drive_removed_at_utc=transfer.probe.now(),retired_cloud_id=remote['cloud_id'])
                journal.store_file(connection,item['sequence'],record)
                result['removed']+=1;result['bytes_removed']+=row['bytes']
                if result['removed']%100==0:
                    transfer.write_json(FOLDER/'drive-progress.json',result);print(json.dumps({k:v for k,v in result.items() if k!='skipped'}),flush=True)
        result.update(state='drive_complete_with_skips' if result['skipped'] else 'drive_complete',completed_at_utc=transfer.probe.now())
        transfer.write_json(FOLDER/'drive-result.json',result);transfer.write_json(CLOUD/'drive-result.json',result)
        shutil.copyfile(FOLDER/'drive-removals.jsonl',CLOUD/'drive-removals.jsonl')
        connection.close();return result


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__);parser.add_argument('phase',choices=['preflight','local','drive']);args=parser.parse_args()
    if args.phase=='preflight':
        m=validate();print(json.dumps({'files':len(m['files']),'bytes':sum(r['bytes'] for r in m['files']),'manifest_sha256':APPROVED_SHA}))
    else: print(json.dumps(local_phase() if args.phase=='local' else drive_phase(),indent=2))
