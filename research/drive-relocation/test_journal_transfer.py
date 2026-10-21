from pathlib import Path
import hashlib
import tempfile
import unittest
from unittest.mock import patch

import bounded_transfer as transfer
import journal_transfer as journal


class JournalTests(unittest.TestCase):
    def test_resume_progress_does_not_rewrite_inventory_or_lose_changed_files(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source, shared = root/'source', root/'shared'
            source.mkdir()
            (source/'a').write_bytes(b'abc')
            (source/'b').write_bytes(b'def')
            inventory = root/'inventory.json'
            database = root/'progress.sqlite'
            with patch.object(transfer.probe, 'SHARED', shared):
                transfer.inventory(source, shared, inventory, reserve_bytes=0)
                before = hashlib.sha256(inventory.read_bytes()).hexdigest()
                journal.initialize(inventory, database)
                (source/'b').write_bytes(b'changed')
                connection = journal.connect(database)
                info = journal.metadata(connection)
                batch = journal.select_batch(connection, info, root)
                journal.stage_batch(connection, batch, info, lambda: None)
                self.assertEqual(journal.summary(connection)['source_changed']['files'], 1)
                self.assertEqual(journal.summary(connection)['mounted_copy_verified']['files'], 1)
                connection.close()
                # Reopening resumes the same batch; no file is silently declared uploaded.
                connection = journal.connect(database)
                resumed = journal.select_batch(connection, journal.metadata(connection), root)
                self.assertEqual(resumed['number'], batch['number'])
                self.assertEqual(hashlib.sha256(inventory.read_bytes()).hexdigest(), before)
                self.assertEqual((source/'a').read_bytes(), b'abc')
                self.assertFalse((shared/'b').exists())
                self.assertEqual((shared/'a').read_bytes(), b'abc')
                connection.close()

    def test_copy_ahead_respects_bytes_and_does_not_invent_confirmation(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source, shared = root/'source', root/'shared'
            source.mkdir()
            (source/'a').write_bytes(b'abc')
            (source/'b').write_bytes(b'def')
            inventory, database = root/'inventory.json', root/'progress.sqlite'
            with patch.object(transfer.probe, 'SHARED', shared):
                transfer.inventory(source, shared, inventory, max_bytes=3, reserve_bytes=0)
                journal.initialize(inventory, database)
                connection = journal.connect(database)
                info = journal.metadata(connection)
                first = journal.select_batch(connection, info, root)
                journal.stage_batch(connection, first, info, lambda: None)
                self.assertIsNone(journal.select_batch(connection, info, root, allow_new=True, remaining_bytes=2))
                second = journal.select_batch(connection, info, root, allow_new=True, remaining_bytes=3)
                self.assertNotEqual(first['indices'], second['indices'])
                journal.stage_batch(connection, second, info, lambda: None)
                calls, pending = [0], [2]
                def snapshots(rows):
                    second = calls[0]*10
                    calls[0] += 1
                    return [{'observed_at_utc': f'2026-10-09T16:00:{second:02d}+00:00',
                        'status': 'cached_metadata_observed', 'shared_drive_id': transfer.probe.SHARED_ID,
                        'cached_md5_field48': row['md5'], 'global_pending_operations': pending[0],
                        'cached_md5_matches': True, 'size_matches': True,
                        'ancestry': [{'local_title': row['relative_path'], 'cloud_id': row['relative_path']}],
                        'item': {'id': row['relative_path'], 'cloud_id': row['relative_path'], 'file_size': row['bytes'],
                            'local_only': 0, 'trashed': 0, 'is_tombstone': 0, 'is_folder': 0}}
                        for row in rows]
                with patch.object(transfer.probe, 'metadata_snapshots', side_effect=snapshots), patch.object(journal.time, 'sleep'):
                    report = journal.verify_group(connection, [first, second])
                    self.assertEqual(report['confirmed_batches'], 0)
                    self.assertEqual(report['content_matched_batches'], 2)
                    self.assertEqual(journal.summary(connection)['content_matched']['files'], 2)
                    pending[0] = 0
                    final = journal.verify_group(connection, [first, second])
                    self.assertEqual(final['confirmed_batches'], 2)
                    self.assertEqual(journal.summary(connection)['client_confirmed']['files'], 2)
                connection.close()


if __name__ == '__main__':
    unittest.main()
