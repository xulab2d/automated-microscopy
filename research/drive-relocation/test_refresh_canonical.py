import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

import bounded_transfer as transfer
import refresh_canonical


class RefreshTests(unittest.TestCase):
    def fixture(self, root, destination_bytes):
        source, shared = root/'source', root/'shared'
        source.mkdir()
        shared.mkdir()
        (source/'code.py').write_bytes(b'NEW')
        (shared/'code.py').write_bytes(destination_bytes)
        expected_old = __import__('hashlib').sha256(b'OLD').hexdigest()
        db = root/'journal.sqlite'
        connection = sqlite3.connect(db)
        connection.execute('CREATE TABLE files(relative_path TEXT,record TEXT,state TEXT,sequence INTEGER)')
        connection.execute('INSERT INTO files VALUES(?,?,?,?)', ('code.py', json.dumps({'sha256': expected_old}), 'client_confirmed', 1))
        connection.commit()
        connection.close()
        plan = root/'plan.json'
        transfer.write_json(plan, {'source_root': str(source), 'destination_root': str(shared), 'new_files': [],
            'changed_files': [{'relative_path': 'code.py', 'bytes': 3, 'source_mtime_ns': (source/'code.py').stat().st_mtime_ns}]})
        return source, shared, db, plan

    def test_explicit_refresh_archives_old_bytes_and_receipt(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source, shared, db, plan = self.fixture(root, b'OLD')
            with patch.object(transfer.probe, 'SHARED', shared):
                result = refresh_canonical.refresh(db, plan, root/'refresh')
            self.assertEqual(result['files_refreshed'], 1)
            self.assertEqual((shared/'code.py').read_bytes(), b'NEW')
            self.assertEqual((source/'code.py').read_bytes(), b'NEW')
            self.assertEqual((root/'refresh'/'previous'/'code.py').read_bytes(), b'OLD')
            archives = list((shared/'transfer-revisions').glob('*/previous/code.py'))
            self.assertEqual(archives[0].read_bytes(), b'OLD')
            self.assertTrue(archives[0].with_name('code.py.receipt.json').exists())

    def test_unexpected_destination_is_preserved(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source, shared, db, plan = self.fixture(root, b'OTHER')
            with patch.object(transfer.probe, 'SHARED', shared):
                result = refresh_canonical.refresh(db, plan, root/'refresh')
            self.assertEqual(result['conflicts'], 1)
            self.assertEqual(result['files_refreshed'], 0)
            self.assertEqual((shared/'code.py').read_bytes(), b'OTHER')
            self.assertEqual((source/'code.py').read_bytes(), b'NEW')


if __name__ == '__main__':
    unittest.main()
