from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

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
