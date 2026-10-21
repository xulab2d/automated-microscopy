"""Reorder only unassigned pending files, with the copy worker stopped."""
import argparse
import json
import re
from pathlib import Path

import bounded_transfer as transfer
import journal_transfer as journal_api


def prioritize(journal, prefix='data/whole-chip-standardized-01/'):
    journal = Path(journal).resolve()
    with transfer.ledger_lock(journal.parent/'worker-owner.json'):
        connection = journal_api.connect(journal)
        try:
            protected = {index for (record,) in connection.execute('SELECT record FROM batches')
                for index in json.loads(record)['indices']}
            rows = [dict(row) for row in connection.execute(
                "SELECT sequence,relative_path,bytes FROM files WHERE state='pending' AND relative_path LIKE ?",
                (prefix+'%',)) if row['sequence'] not in protected]
            def order(row):
                path = row['relative_path']
                tile = bool(re.search(r'/chip\d+/tiles/', path))
                return (0 if tile else 1 if path.endswith(('manifest.json','batch.json','layout.json')) else 2,
                    path)
            rows.sort(key=order)
            first = connection.execute('SELECT coalesce(min(sequence),0) FROM files').fetchone()[0]-len(rows)
            transition = {'at_utc': transfer.probe.now(), 'reason': 'User needs current working campaign before historical experiments.',
                'prefix': prefix, 'files': len(rows), 'bytes': sum(row['bytes'] for row in rows),
                'assigned_batches_unchanged': True, 'source_deletion_performed': False,
                'mapping': [{'path': row['relative_path'], 'old_sequence': row['sequence'], 'new_sequence': first+i}
                    for i,row in enumerate(rows)]}
            with connection:
                connection.executemany('UPDATE files SET sequence=? WHERE sequence=? AND state=\'pending\'',
                    [(first+i,row['sequence']) for i,row in enumerate(rows)])
                connection.execute('INSERT OR REPLACE INTO metadata VALUES(?,?)',
                    ('current_campaign_priority',json.dumps({k:v for k,v in transition.items() if k!='mapping'})))
            transfer.write_json(journal.parent/'campaign-priority-transition.json',transition)
            return {k:v for k,v in transition.items() if k!='mapping'}
        finally:
            connection.close()


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--journal',required=True)
    args=parser.parse_args()
    print(json.dumps(prioritize(args.journal),indent=2))
