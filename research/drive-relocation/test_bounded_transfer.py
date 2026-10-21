import copy
import tempfile
import sqlite3
import unittest
from pathlib import Path
from unittest.mock import patch

import bounded_transfer as transfer


class TransferTests(unittest.TestCase):
    def snapshot(self, second=0):
        return {'observed_at_utc': f'2026-10-09T16:00:{second:02d}+00:00',
            'status': 'cached_metadata_observed', 'shared_drive_id': transfer.probe.SHARED_ID,
            'cached_md5_field48': 'ab'*16, 'global_pending_operations': 0,
            'ancestry': [{'local_title': 'file', 'cloud_id': 'cloud-id'}],
            'item': {'id': 'cloud-id', 'cloud_id': 'cloud-id', 'file_size': 3,
                'local_only': 0, 'trashed': 0, 'is_tombstone': 0, 'is_folder': 0}}

    def test_two_stable_snapshots_confirm_client_only(self):
        gate = transfer.client_gate(self.snapshot(), self.snapshot(10), {'bytes': 3, 'md5': 'ab'*16})
        self.assertTrue(gate['client_confirmed'])
        self.assertFalse(gate['independent_server_verified'])

    def test_missing_md5_pending_and_changed_cloud_fail(self):
        original = self.snapshot(10)
        for field, value in [('cached_md5_field48', None), ('global_pending_operations', 1)]:
            second = copy.deepcopy(original)
            second[field] = value
            self.assertFalse(transfer.client_gate(self.snapshot(), second, {'bytes': 3, 'md5': 'ab'*16})['client_confirmed'])
        second = copy.deepcopy(original)
        second['ancestry'][0]['cloud_id'] = 'different'
        self.assertFalse(transfer.client_gate(self.snapshot(), second, {'bytes': 3, 'md5': 'ab'*16})['client_confirmed'])

    def test_snapshot_interval_enforced(self):
        self.assertFalse(transfer.client_gate(self.snapshot(), self.snapshot(2), {'bytes': 3, 'md5': 'ab'*16})['client_confirmed'])

    def test_copy_resume_and_collision_preserve_both(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source, destination = root/'source', root/'shared'/'copy'
            source.write_bytes(b'abc')
            row = {'source': str(source), 'destination': str(destination), 'bytes': 3,
                'source_mtime_ns': source.stat().st_mtime_ns}
            with patch.object(transfer.probe, 'SHARED', root/'shared'):
                first = transfer.copy_one(row, 0, lambda: 100*transfer.MIB)
                second = transfer.copy_one(row, 0, lambda: 100*transfer.MIB)
                self.assertFalse(first['reused_existing_destination'])
                self.assertTrue(second['reused_existing_destination'])
                self.assertEqual(first['sha256'], second['sha256'])
                destination.write_bytes(b'bad')
                with self.assertRaises(ValueError):
                    transfer.copy_one(row, 0, lambda: 100*transfer.MIB)
                self.assertEqual(source.read_bytes(), b'abc')
                self.assertEqual(destination.read_bytes(), b'bad')

    def test_disk_reserve_prevents_creating_destination(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source, destination = root/'source', root/'shared'/'copy'
            source.write_bytes(b'abc')
            row = {'source': str(source), 'destination': str(destination), 'bytes': 3,
                'source_mtime_ns': source.stat().st_mtime_ns}
            with patch.object(transfer.probe, 'SHARED', root/'shared'):
                with self.assertRaises(OSError):
                    transfer.copy_one(row, 100, lambda: 105)
                self.assertFalse(destination.exists())

    def test_midcopy_reserve_failure_retains_partial_without_poisoning_canonical(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source, destination = root/'source', root/'shared'/'copy'
            source.write_bytes(b'abc')
            row = {'source': str(source), 'destination': str(destination), 'bytes': 3,
                'source_mtime_ns': source.stat().st_mtime_ns}
            values = iter([100*transfer.MIB, 0])
            with patch.object(transfer.probe, 'SHARED', root/'shared'):
                with self.assertRaisesRegex(OSError, 'staged partial'):
                    transfer.copy_one(row, 0, lambda: next(values))
                self.assertFalse(destination.exists())
                partials = list(destination.parent.glob('.t*'))
                self.assertEqual(len(partials), 1)
                result = transfer.copy_one(row, 0, lambda: 100*transfer.MIB)
                self.assertEqual(destination.read_bytes(), b'abc')
                self.assertEqual(source.read_bytes(), b'abc')
                self.assertTrue(partials[0].exists())
                self.assertFalse(result['reused_existing_destination'])

    def test_long_destination_keeps_staging_under_drivefs_path_limit(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root/'source'
            source.write_bytes(b'abc')
            parent = root/'shared'/('a'*70)/('b'*60)
            # Reproduce the failing canonical length: the old suffix made261.
            destination = parent/('f'*(211-len(str(parent))-1))
            self.assertEqual(len(str(destination)), 211)
            row = {'source':str(source),'destination':str(destination),'bytes':3,
                'source_mtime_ns':source.stat().st_mtime_ns}
            original_open = Path.open
            staging_paths = []
            def drivefs_open(path, mode='r', *args, **kwargs):
                if mode == 'xb':
                    staging_paths.append(path)
                    if len(str(path)) >= 260:
                        raise FileNotFoundError(2, 'DriveFS path limit', str(path))
                return original_open(path, mode, *args, **kwargs)
            with patch.object(transfer.probe,'SHARED',root/'shared'), patch.object(Path,'open',drivefs_open):
                result = transfer.copy_one(row,0,lambda:100*transfer.MIB)
            self.assertEqual(destination.read_bytes(),b'abc')
            self.assertEqual(result['sha256'],result['mounted_sha256'])
            self.assertEqual(staging_paths[0].parent,destination.parent)
            self.assertLess(len(str(staging_paths[0])),260)
            self.assertFalse(staging_paths[0].exists())

    def test_inventory_rejects_oversize_and_mapping_tamper(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root/'source').mkdir()
            (root/'source'/'one').write_bytes(b'abc')
            with patch.object(transfer.probe, 'SHARED', root/'shared'):
                with self.assertRaises(ValueError):
                    transfer.inventory(root/'source', root/'shared', root/'ledger', max_bytes=2)
                result = transfer.inventory(root/'source', root/'shared', root/'ledger', max_bytes=3)
                transfer.validate_ledger(result)
                result['files'][0]['destination'] = str(root/'outside')
                with self.assertRaises(ValueError):
                    transfer.validate_ledger(result)

    def test_priority_inventory_preserves_raw_and_isolates_oversize(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root/'source'
            for name in ['flakefinder/tool.py', 'data/chip/analysis/masks.npz', 'data/chip/raw.tiff',
                         'data/zen-captures/vendor.czi', '.venv/runtime.py', 'research/report.txt']:
                path = source/name
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(b'abc')
            with patch.object(transfer.probe, 'SHARED', root/'shared'):
                result = transfer.inventory(source, root/'shared', root/'ledger', max_bytes=2,
                    allow_oversized=True, workspace_phases=True, exclude_directories=['.venv'])
            self.assertEqual(result['file_count'], 5)
            self.assertEqual(result['files'][0]['phase'], '1_code_research_configs')
            self.assertEqual(result['files'][-1]['phase'], '4_vendor_data')
            self.assertTrue(any(row['relative_path'].endswith('raw.tiff') for row in result['files']))

    def test_batched_metadata_uses_exact_cached_md5_and_path(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            database = root/'metadata'
            connection = sqlite3.connect(database)
            connection.executescript('''
                CREATE TABLE items(stable_id INTEGER,id TEXT,local_title TEXT,file_size INTEGER,
                    is_folder INTEGER,trashed INTEGER,is_tombstone INTEGER,team_drive_stable_id INTEGER,proto BLOB);
                CREATE TABLE stable_parents(item_stable_id INTEGER,parent_stable_id INTEGER);
                CREATE TABLE stable_ids(stable_id INTEGER,cloud_id TEXT);
                CREATE TABLE local_stable_ids(stable_id INTEGER);
                CREATE TABLE operations(id INTEGER);
            ''')
            # field48, wire2 => varint386; length32 and exact hexadecimal MD5.
            blob = b'\x82\x03\x20' + b'ab'*16
            connection.execute('INSERT INTO items VALUES(1,?,"drive",0,1,0,0,1,?)',
                (transfer.probe.SHARED_ID, b''))
            connection.execute('INSERT INTO items VALUES(2,"file-id","one",3,0,0,0,1,?)', (blob,))
            connection.execute('INSERT INTO stable_parents VALUES(2,1)')
            connection.execute('INSERT INTO stable_ids VALUES(2,"file-id")')
            connection.commit()
            connection.close()
            result = transfer.probe.metadata_snapshots([{'destination': str(root/'shared'/'one'),
                'bytes': 3, 'md5': 'ab'*16}], db=database, shared=root/'shared')[0]
            self.assertTrue(result['cached_md5_matches'])
            self.assertEqual(result['item']['cloud_id'], 'file-id')
            self.assertEqual(result['global_pending_operations'], 0)


if __name__ == '__main__':
    unittest.main()
