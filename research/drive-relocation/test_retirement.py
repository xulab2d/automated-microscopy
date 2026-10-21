import json
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import bounded_transfer as transfer
import journal_transfer as journal
import offload_verified as offload
import refresh_plan
import retire_intermediates as retire


class RetirementTests(unittest.TestCase):
    def test_inactive_scope_preserves_active_campaign_code_and_traversal(self):
        for path in ['data/whole-chip-standardized-01/chip001/tiles/tile.tiff',
                     'flakefinder/analysis.py', 'configs/storage.json',
                     'data/../configs/config.json', 'research/active/mask.png']:
            self.assertFalse(offload.allowed(path, 'inactive_data'), path)
        self.assertTrue(offload.allowed('data/chip-batch-01/old/scan.tiff', 'inactive_data'))

    def test_retired_row_never_recopied_or_reclassified_missing(self):
        with tempfile.TemporaryDirectory() as folder:
            connection=sqlite3.connect(':memory:');connection.row_factory=sqlite3.Row
            connection.executescript('CREATE TABLE files(sequence INTEGER PRIMARY KEY,state TEXT,record TEXT); CREATE TABLE batches(id INTEGER PRIMARY KEY,state TEXT,record TEXT);')
            row={'state':'retired_intentionally','source':str(Path(folder)/'absent'),'bytes':100,'relative_path':'data/old.tiff'}
            connection.execute('INSERT INTO files VALUES(1,?,?)',(row['state'],json.dumps(row)))
            batch={'number':1,'indices':[1],'state':'copying','report':str(Path(folder)/'batch.json')}
            with patch.object(transfer,'copy_one',side_effect=AssertionError('Must not copy')):
                journal.stage_batch(connection,batch,{'reserve_bytes':0},lambda:None)
            self.assertEqual(connection.execute('SELECT state FROM files').fetchone()[0],'retired_intentionally')

    def test_checked_path_rejects_mapping_escape(self):
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder);path=root/'kept';path.write_text('safe')
            self.assertEqual(retire.checked_path(path,root,'kept'),path)
            with self.assertRaises(ValueError):retire.checked_path(path,root,'../kept')
            with self.assertRaises(ValueError):retire.checked_path(path,root,'other')
            self.assertEqual(path.read_text(),'safe')

    def test_recreated_retired_source_requires_review(self):
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder)/'source';root.mkdir();old=root/'old.tiff';old.write_bytes(b'new')
            database=Path(folder)/'progress.sqlite';connection=sqlite3.connect(database)
            connection.executescript('CREATE TABLE metadata(key TEXT,value TEXT); CREATE TABLE files(relative_path TEXT,bytes INTEGER,state TEXT,record TEXT);')
            connection.executemany('INSERT INTO metadata VALUES(?,?)', [('source_root',json.dumps(str(root))),('destination_root',json.dumps(str(Path(folder)/'drive')))])
            connection.execute('INSERT INTO files VALUES(?,?,?,?)',('old.tiff',3,'retired_intentionally',json.dumps({'source_mtime_ns':old.stat().st_mtime_ns})))
            connection.commit();connection.close()
            with self.assertRaisesRegex(ValueError,'retired path was recreated'):
                refresh_plan.audit(database,Path(folder)/'proposal.json')


if __name__=='__main__': unittest.main()
