from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from listening import ListeningInstance
from receipts import ReceiptStore
from worker import PlaybackTiming, ScrobbleDelivery


class WorkerTests(unittest.TestCase):
    def test_delivery_waits_for_verified_playback_and_explicit_enablement(self):
        with TemporaryDirectory() as folder:
            times = iter(range(40))
            calls = []
            delivery = ScrobbleDelivery(
                ReceiptStore(Path(folder) / "receipts.db"), "simulated",
                timing=PlaybackTiming(clock=lambda: next(times)),
                sender=lambda *args, **kwargs: calls.append(kwargs) is None,
            )
            play = ListeningInstance("test", 123, 60)
            for _ in range(35):
                self.assertFalse(delivery.observe(play, audible=True, enabled=False,
                                                  artist="Test", title="Song"))
            self.assertFalse(calls)
            self.assertFalse(delivery.observe(play, audible=False, enabled=True,
                                              artist="Test", title="Song"))
            self.assertTrue(delivery.observe(play, audible=True, enabled=True,
                                             artist="Test", title="Song"))
            self.assertFalse(delivery.observe(play, audible=True, enabled=True,
                                              artist="Test", title="Song"))
            self.assertEqual(len(calls), 1)

    def test_long_observation_gap_is_not_counted(self):
        times = iter([0, 1, 20, 21])
        timing = PlaybackTiming(clock=lambda: next(times))
        play = ListeningInstance("test", 123, 60)
        for _ in range(4):
            timing.observe(play, audible=True)
        self.assertEqual(play.seconds, 2)
