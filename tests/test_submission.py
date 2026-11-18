from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from listening import ListeningInstance
from receipts import ReceiptStore
from submission import submit_once


class SubmissionTests(unittest.TestCase):
    def test_timeout_does_not_retry_after_restart(self):
        with TemporaryDirectory() as folder:
            path = Path(folder) / "receipt.db"
            play = ListeningInstance("test", 1, 60, seconds=30)

            def timeout(_play):
                raise TimeoutError

            params = {"enabled": True, "service": "test"}
            with self.assertRaises(TimeoutError):
                submit_once(play, receipts=ReceiptStore(path), send=timeout, **params)
            self.assertFalse(submit_once(play, receipts=ReceiptStore(path), send=timeout, **params))

    def test_opt_out_and_confirmed_submission(self):
        with TemporaryDirectory() as folder:
            store = ReceiptStore(Path(folder) / "receipt.db")
            play = ListeningInstance("test", 1, 60, seconds=30)
            calls = []

            def send(candidate):
                calls.append(candidate)
                return True

            args = {"receipts": store, "service": "test", "send": send}
            self.assertFalse(submit_once(play, enabled=False, **args))
            self.assertTrue(submit_once(play, enabled=True, **args))
            self.assertFalse(submit_once(play, enabled=True, **args))
            self.assertEqual(calls, [play])
