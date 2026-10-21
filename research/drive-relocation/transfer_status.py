"""Read-only live progress, including file-by-file work inside a large batch."""
from pathlib import Path
import argparse
import json
import shutil
import sqlite3

import drivefs_transfer_probe as probe


def status(journal):
    journal = Path(journal).resolve()
    connection = sqlite3.connect(journal.as_uri()+'?mode=ro', uri=True, timeout=2)
    connection.execute('PRAGMA query_only=ON')
    try:
        progress = {state: {'files': count, 'bytes': size or 0} for state, count, size in connection.execute(
            'SELECT state,count(*),sum(bytes) FROM files GROUP BY state')}
        latest = connection.execute("SELECT max(json_extract(record,'$.copied_at_utc')) FROM files").fetchone()[0]
        batches = [json.loads(row[0]) for row in connection.execute(
            "SELECT record FROM batches WHERE state!='client_confirmed' ORDER BY id")]
        result = {'observed_at_utc': probe.now(), 'progress': progress, 'latest_file_copy_utc': latest,
            'outstanding_batches': [{key: batch.get(key) for key in
                ('number', 'state', 'bytes', 'started_at_utc', 'copied_at_utc', 'checked_at_utc')} for batch in batches],
            'free_cache_volume_bytes': shutil.disk_usage(probe.DB.anchor).free,
            'source_deletion_performed': False}
        return result
    finally:
        connection.close()


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--journal', required=True)
    args = parser.parse_args()
    print(json.dumps(status(args.journal), indent=2))
