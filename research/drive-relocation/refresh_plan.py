"""Read-only audit of new/changed source files after the immutable copy inventory.

This writes a proposed refresh manifest only. It never updates a destination,
changes the live transfer journal, deletes originals, or copies unfinished code.
"""
from pathlib import Path
import argparse
import fnmatch
import json
import os
import sqlite3

import bounded_transfer as transfer
import run_transfer


def audit(journal, output, exclude_in_progress=()):
    journal = Path(journal).resolve()
    connection = sqlite3.connect(journal.as_uri()+'?mode=ro', uri=True, timeout=2)
    connection.execute('PRAGMA query_only=ON')
    try:
        info = {key: json.loads(value) for key, value in connection.execute('SELECT key,value FROM metadata')}
        known = {path: (size, mtime, state) for path, size, mtime, state in connection.execute(
            "SELECT relative_path,bytes,json_extract(record,'$.source_mtime_ns'),state FROM files")}
        source_root, destination_root = Path(info['source_root']), Path(info['destination_root'])
        directories_to_skip = [pattern for pattern in run_transfer.EXCLUDE_DIRECTORIES if pattern != 'laptop-viewer']
        directories_to_skip += ['research/drive-relocation/*-state']
        new, changed, deferred, reparse = [], [], [], []
        for folder, directories, names in os.walk(source_root, followlinks=False):
            kept = []
            for name in directories:
                path = Path(folder)/name
                relative = path.relative_to(source_root).as_posix()
                if any(fnmatch.fnmatchcase(name, pattern) or fnmatch.fnmatchcase(relative, pattern) for pattern in directories_to_skip):
                    continue
                if path.is_symlink() or getattr(path.lstat(), 'st_file_attributes', 0) & 1024:
                    reparse.append(relative)
                    continue
                kept.append(name)
            directories[:] = kept
            for name in names:
                path = Path(folder)/name
                relative = path.relative_to(source_root).as_posix()
                if any(fnmatch.fnmatchcase(relative.lower(), pattern.lower()) for pattern in run_transfer.EXCLUDE_FILES):
                    continue
                if any(fnmatch.fnmatchcase(relative, pattern) for pattern in exclude_in_progress):
                    deferred.append(relative)
                    continue
                stat = path.stat()
                old = known.get(relative)
                if old and old[2]=='retired_intentionally':
                    raise ValueError('Intentionally retired path was recreated; review before mirroring: '+relative)
                if old and (old[0], old[1]) == (stat.st_size, stat.st_mtime_ns):
                    continue
                row = {'relative_path': relative, 'source': str(path), 'destination': str(destination_root/relative),
                    'bytes': stat.st_size, 'source_mtime_ns': stat.st_mtime_ns,
                    'previous_transfer_state': old[2] if old else None,
                    'previous_bytes': old[0] if old else None, 'previous_mtime_ns': old[1] if old else None}
                if old:
                    row['action'] = 'versioned_destination_update_required' if old[2] in ('client_confirmed', 'mounted_copy_verified') else 'record_new_source_revision_before_copy'
                    changed.append(row)
                else:
                    row['action'] = 'append_new_path_to_priority_inventory'
                    new.append(row)
        result = {'schema': 1, 'mode': 'read_only_refresh_proposal', 'observed_at_utc': transfer.probe.now(),
            'source_root': str(source_root), 'destination_root': str(destination_root),
            'new_files': new, 'changed_files': changed, 'in_progress_files_deferred': deferred,
            'reparse_paths_deferred': reparse, 'source_deletion_performed': False,
            'destination_writes_performed': False,
            'activation_policy': 'Freeze edited sources first. New paths may be appended with a signed inventory. For existing uploaded paths, preserve the verified old destination in a revision archive and verify that archive before an explicitly recorded canonical update. Never treat a source_changed row as completed.'}
        transfer.write_json(output, result)
        return {'new_files': len(new), 'new_bytes': sum(row['bytes'] for row in new),
            'changed_files': len(changed), 'changed_bytes': sum(row['bytes'] for row in changed),
            'in_progress_deferred': len(deferred), 'output': str(Path(output).resolve())}
    finally:
        connection.close()


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--journal', required=True)
    parser.add_argument('--output', required=True)
    parser.add_argument('--exclude-in-progress', action='append', default=[])
    args = parser.parse_args()
    print(json.dumps(audit(args.journal, args.output, args.exclude_in_progress), indent=2))
