"""Explicit, version-preserving refresh of already inventoried project paths.

Only destinations matching their prior verified upload SHA256 may be replaced.
The previous bytes and receipt are retained locally AND in Drive. Scientific
originals are never removed. Conflicting destinations are untouched.
"""
from pathlib import Path
import argparse
import datetime
import hashlib
import json
import os
import shutil
import sqlite3

import bounded_transfer as transfer


def exact_copy(source, destination, expected_sha256):
    source, destination = Path(source), Path(destination)
    destination.parent.mkdir(parents=True, exist_ok=True)
    if destination.exists():
        observed = transfer.probe.hashes(destination)
        if observed['sha256'] != expected_sha256:
            raise ValueError('Revision/staging destination conflicts with expected bytes.')
        return observed
    if shutil.disk_usage(transfer.probe.DB.anchor).free < 8*transfer.GIB+2*source.stat().st_size:
        raise OSError('Insufficient disk reserve for versioned refresh.')
    with source.open('rb') as src, destination.open('xb') as dst:
        shutil.copyfileobj(src, dst, length=transfer.MIB)
        dst.flush()
        os.fsync(dst.fileno())
    observed = transfer.probe.hashes(destination)
    if observed['sha256'] != expected_sha256:
        raise ValueError('Versioned copy checksum mismatch; all sources retained.')
    return observed


def refresh(journal, proposal, output_folder, only=()):
    journal, proposal, output_folder = Path(journal).resolve(), Path(proposal).resolve(), Path(output_folder).resolve()
    if output_folder.exists():
        raise FileExistsError('Use a new refresh directory; never overwrite revision evidence.')
    plan = transfer.read_json(proposal)
    source_root, destination_root = Path(plan['source_root']).resolve(), transfer.ensure_destination(plan['destination_root'])
    connection = sqlite3.connect(journal.as_uri()+'?mode=ro', uri=True, timeout=2)
    connection.execute('PRAGMA query_only=ON')
    run_id = datetime.datetime.now(datetime.timezone.utc).strftime('%Y%m%dT%H%M%SZ')
    selected = [row for row in plan['new_files']+plan['changed_files'] if not only or row['relative_path'] in only]
    report = {'schema': 1, 'run_id': run_id, 'mode': 'explicit_versioned_canonical_refresh',
        'source_deletion_performed': False, 'proposal': str(proposal),
        'proposal_sha256': hashlib.sha256(proposal.read_bytes()).hexdigest(), 'files': [], 'conflicts': []}
    output_folder.mkdir(parents=True)
    confirmed_rows = []
    try:
        for planned in selected:
            relative = Path(planned['relative_path'])
            if relative.is_absolute() or '..' in relative.parts:
                raise ValueError('Invalid project-relative refresh path.')
            source, destination = source_root/relative, destination_root/relative
            transfer.ensure_destination(destination)
            stat = source.stat()
            if (stat.st_size, stat.st_mtime_ns) != (planned['bytes'], planned['source_mtime_ns']):
                report['conflicts'].append({'relative_path': relative.as_posix(), 'reason': 'Source changed after refresh proposal; deferred.'})
                continue
            expected = transfer.probe.hashes(source)
            old = connection.execute('SELECT record,state,sequence FROM files WHERE relative_path=?', (relative.as_posix(),)).fetchone()
            previous = json.loads(old[0]) if old else None
            receipt = {'relative_path': relative.as_posix(), 'source': str(source), 'destination': str(destination),
                'new_hashes': expected, 'previous_transfer_receipt': previous, 'source_retained': True,
                'started_at_utc': transfer.probe.now()}
            if destination.exists():
                actual = transfer.probe.hashes(destination)
                if actual['sha256'] == expected['sha256']:
                    receipt['action'] = 'already_matches_current_source'
                elif not old or old[1] != 'client_confirmed' or actual['sha256'] != previous.get('sha256'):
                    report['conflicts'].append({'relative_path': relative.as_posix(), 'reason': 'Unexpected/unverified destination bytes; preserved untouched.',
                        'actual_destination_sha256': actual['sha256'], 'previous_verified_sha256': previous.get('sha256') if previous else None})
                    continue
                else:
                    local_backup = output_folder/'previous'/relative
                    drive_backup = destination_root/'transfer-revisions'/run_id/'previous'/relative
                    exact_copy(destination, local_backup, actual['sha256'])
                    exact_copy(local_backup, drive_backup, actual['sha256'])
                    backup_receipt = local_backup.with_name(local_backup.name+'.receipt.json')
                    transfer.write_json(backup_receipt, {'canonical_destination': str(destination),
                        'previous_hashes': actual, 'previous_verified_upload': previous,
                        'archived_at_utc': transfer.probe.now(), 'source_deletion_performed': False})
                    exact_copy(backup_receipt, drive_backup.with_name(drive_backup.name+'.receipt.json'),
                        transfer.probe.hashes(backup_receipt)['sha256'])
                    receipt.update(action='replace_verified_old_version', previous_hashes=actual,
                        local_previous_version=str(local_backup), drive_previous_version=str(drive_backup))
            else:
                receipt['action'] = 'create_new_canonical_path'
            if receipt['action'] != 'already_matches_current_source':
                staged = destination_root/'transfer-revisions'/run_id/'staged'/relative
                exact_copy(source, staged, expected['sha256'])
                # Both resolved paths stay inside this exact project destination.
                staged.resolve().relative_to(destination_root)
                destination.resolve().relative_to(destination_root)
                destination.parent.mkdir(parents=True, exist_ok=True)
                if destination.exists():
                    if 'previous_hashes' not in receipt:
                        raise ValueError('A destination appeared during new-file staging; preserved for review.')
                    recheck = transfer.probe.hashes(destination)
                    if recheck['sha256'] != receipt['previous_hashes']['sha256']:
                        raise ValueError('Destination changed during revision preparation; canonical update stopped.')
                os.replace(staged, destination)
            current = transfer.probe.hashes(destination)
            if current['sha256'] != expected['sha256']:
                raise ValueError('Canonical replacement readback failed; local versions retained.')
            receipt.update(state='mounted_copy_verified', completed_at_utc=transfer.probe.now(), mounted_sha256=current['sha256'])
            report['files'].append(receipt)
            confirmed_rows.append({'relative_path': relative.as_posix(), 'source': str(source), 'destination': str(destination),
                'source_mtime_ns': expected['mtime_ns'], **expected, 'state': 'mounted_copy_verified',
                'mounted_sha256': current['sha256'], 'source_retained': True, 'revision_receipt': str(output_folder/'revisions.json')})
            transfer.write_json(output_folder/'revisions.json', report)
    finally:
        connection.close()
        transfer.write_json(output_folder/'revisions.json', report)
    ledger = {'schema': 1, 'mode': 'explicit_versioned_refresh', 'created_at_utc': transfer.probe.now(),
        'source_root': str(source_root), 'destination_root': str(destination_root),
        'max_batch_bytes': 256*transfer.MIB, 'reserve_bytes': 8*transfer.GIB,
        'mapping_sha256': transfer.mapping_digest(confirmed_rows), 'files': confirmed_rows,
        'file_count': len(confirmed_rows), 'total_bytes': sum(row['bytes'] for row in confirmed_rows),
        'source_deletion_performed': False, 'active_batch': 1,
        'batches': [{'number': 1, 'state': 'awaiting_client_confirmation', 'indices': list(range(len(confirmed_rows))),
            'bytes': sum(row['bytes'] for row in confirmed_rows), 'report': str(output_folder/'client-confirmation.json')}]}
    transfer.write_json(output_folder/'ledger.json', ledger)
    exact_copy(output_folder/'revisions.json', destination_root/'transfer-revisions'/run_id/'revisions.json',
        transfer.probe.hashes(output_folder/'revisions.json')['sha256'])
    return {'files_refreshed': len(confirmed_rows), 'conflicts': len(report['conflicts']),
        'bytes': ledger['total_bytes'], 'ledger': str(output_folder/'ledger.json'), 'revisions': str(output_folder/'revisions.json')}


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--journal', required=True)
    parser.add_argument('--proposal', required=True)
    parser.add_argument('--output-folder', required=True)
    parser.add_argument('--only', action='append', default=[])
    args = parser.parse_args()
    print(json.dumps(refresh(args.journal, args.proposal, args.output_folder, args.only), indent=2))
