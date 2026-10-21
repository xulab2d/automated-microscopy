import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

import bounded_transfer as transfer
import complete_transfer
import journal_transfer


class CompletionTests(unittest.TestCase):
    def fixture(self,folder):
        source,shared=folder/'source',folder/'shared'
        source.mkdir(); shared.mkdir()
        (source/'code.py').write_bytes(b'old')
        inventory=folder/'inventory.json'
        with patch.object(transfer.probe,'SHARED',shared):
            transfer.inventory(source,shared,inventory)
        journal=folder/'journal.sqlite'
        with patch.object(transfer.probe,'SHARED',shared):
            journal_transfer.initialize(inventory,journal)
        (source/'code.py').write_bytes(b'new')
        (shared/'code.py').write_bytes(b'new')
        hashes=transfer.probe.hashes(source/'code.py')
        row={'relative_path':'code.py','source':str(source/'code.py'),'destination':str(shared/'code.py'),
            **hashes,'source_mtime_ns':hashes['mtime_ns'],'mounted_sha256':hashes['sha256'],
            'state':'client_confirmed','client_confirmation':{'client_confirmed':True}}
        ledger=folder/'revision.json'
        transfer.write_json(ledger,{'source_root':str(source),'destination_root':str(shared),'files':[row],
            'mapping_sha256':transfer.mapping_digest([row])})
        return source,shared,journal,ledger

    def test_reconcile_preserves_old_receipt_and_current_sources(self):
        with tempfile.TemporaryDirectory() as tmp:
            source,shared,journal,ledger=self.fixture(Path(tmp))
            with patch.object(transfer.probe,'SHARED',shared):
                self.assertEqual(complete_transfer.reconcile_verified(journal,ledger),1)
            connection=sqlite3.connect(journal)
            state,record=connection.execute('SELECT state,record FROM files').fetchone()
            connection.close()
            self.assertEqual(state,'client_confirmed')
            self.assertEqual(len(json.loads(record)['revision_history']),1)
            self.assertEqual((source/'code.py').read_bytes(),b'new')
            self.assertEqual((shared/'code.py').read_bytes(),b'new')

    def test_unverified_or_changed_destination_never_reconciles(self):
        with tempfile.TemporaryDirectory() as tmp:
            _,shared,journal,ledger=self.fixture(Path(tmp))
            (shared/'code.py').write_bytes(b'conflict')
            with patch.object(transfer.probe,'SHARED',shared):
                with self.assertRaisesRegex(ValueError,'differs'):
                    complete_transfer.reconcile_verified(journal,ledger)
            self.assertEqual((shared/'code.py').read_bytes(),b'conflict')
            data=transfer.read_json(ledger)
            data['files'][0]['client_confirmation']['client_confirmed']=False
            transfer.write_json(ledger,data)
            with patch.object(transfer.probe,'SHARED',shared):
                with self.assertRaisesRegex(ValueError,'without client confirmation'):
                    complete_transfer.reconcile_verified(journal,ledger)

    def test_freeze_is_explicit(self):
        with tempfile.TemporaryDirectory() as tmp:
            path=Path(tmp)/'freeze.json'
            self.assertFalse(complete_transfer.freeze_ready(path,tmp))
            transfer.write_json(path,{'schema':1,'ready':False,'frozen_at_utc':'now'})
            with self.assertRaises(ValueError):
                complete_transfer.freeze_ready(path,tmp)
            transfer.write_json(path,{'schema':1,'ready':True,'frozen_at_utc':'now'})
            self.assertTrue(complete_transfer.freeze_ready(path,tmp))


if __name__=='__main__':
    unittest.main()
