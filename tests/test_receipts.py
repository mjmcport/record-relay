from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
import sqlite3
import time

from receipts import ReceiptStore


class ReceiptTests(unittest.TestCase):
    def test_unknown_outcome_blocks_retry_after_restart(self):
        with TemporaryDirectory() as folder:
            path = Path(folder) / "receipts.db"
            key = ("test-service", "recording-id", 123)
            self.assertTrue(ReceiptStore(path).reserve(*key))
            store = ReceiptStore(path)
            self.assertFalse(store.reserve(*key))
            self.assertTrue(store.mark_accepted(*key))
            self.assertFalse(store.reserve(*key))
            self.assertFalse(store.mark_accepted(*key))
            self.assertTrue(store.reserve("test-service", "recording-id", 456))

    def test_account_scoped_queue_and_receipts_do_not_collide(self):
        with TemporaryDirectory() as folder:
            store = ReceiptStore(Path(folder) / "receipts.db")
            now = int(time.time())
            for account in ("account-a", "account-b"):
                self.assertTrue(store.queue_listen(
                    play_id="same-occurrence", account_hash=account, track="song",
                    artist="Artist", title="Song", album=None, listened_at=100,
                    eligible_at=now, expires_at=now + 3600))
            a = store.queued_listens("account-a")
            b = store.queued_listens("account-b")
            self.assertEqual(len(a), 1)
            self.assertEqual(len(b), 1)
            self.assertIsNotNone(store.claim_queued_listen(a[0]["play_id"], "account-a"))
            self.assertIsNotNone(store.claim_queued_listen(b[0]["play_id"], "account-b"))

    def test_legacy_queue_and_receipts_migrate_without_dropping_ambiguous_pending(self):
        with TemporaryDirectory() as folder:
            path = Path(folder) / "receipts.db"
            with sqlite3.connect(path) as db:
                db.execute("CREATE TABLE receipt(service TEXT,track TEXT,started_at INTEGER,state TEXT,PRIMARY KEY(service,track,started_at))")
                db.execute("INSERT INTO receipt VALUES('listenbrainz','legacy',123,'pending')")
                db.execute("CREATE TABLE listen_queue(play_id TEXT PRIMARY KEY,account_hash TEXT,track TEXT,artist TEXT,title TEXT,album TEXT,listened_at INTEGER,eligible_at INTEGER,expires_at INTEGER,state TEXT,updated_at INTEGER)")
                now = int(time.time())
                db.execute("INSERT INTO listen_queue VALUES('p','acct','track','Artist','Title',NULL,1,?,?,'queued',?)",
                           (now, now + 3600, now))
            migrated = ReceiptStore(path)
            self.assertFalse(migrated.reserve("listenbrainz", "legacy", 123,
                                               account_hash="new-account"))
            self.assertEqual(migrated.queued_listens("acct")[0]["play_id"], "p")
            self.assertEqual(migrated.queued_listens("acct")[0]["state"], "queued")
