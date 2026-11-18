from pathlib import Path
from tempfile import TemporaryDirectory
import time
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

    def test_offline_queue_preserves_original_timestamp_and_sends_once_after_reconnect(self):
        with TemporaryDirectory() as folder:
            store = ReceiptStore(Path(folder) / "receipts.db")
            play = ListeningInstance("track-key", 123, 60, seconds=30, play_id="play-1")
            calls = []
            offline = ScrobbleDelivery(
                store, "simulated-token", queue_enabled=True, online_check=lambda: False,
                wall_clock=time.time, sender=lambda *args, **kwargs: calls.append(args) is None)
            self.assertFalse(offline.observe(play, audible=True, enabled=True,
                                            artist="Fixture artist", title="Fixture song"))
            queued = offline.queued()
            self.assertEqual(queued[0]["listened_at"], 123)
            self.assertEqual(queued[0]["state"], "queued")
            self.assertFalse(calls)

            recovered = ScrobbleDelivery(
                ReceiptStore(Path(folder) / "receipts.db"), "simulated-token", queue_enabled=True,
                online_check=lambda: True, sender=lambda candidate, **kwargs: (
                    calls.append((candidate.started_at, kwargs["artist"], kwargs["title"])) or True))
            self.assertTrue(recovered.flush_queued(still_enabled=lambda: True))
            self.assertEqual(calls, [(123, "Fixture artist", "Fixture song")])
            self.assertFalse(recovered.flush_queued(still_enabled=lambda: True))
            self.assertEqual(recovered.queued(), [])

    def test_ambiguous_queue_send_is_held_after_restart_without_replay(self):
        with TemporaryDirectory() as folder:
            store = ReceiptStore(Path(folder) / "receipts.db")
            play = ListeningInstance("track-key", 123, 60, seconds=30, play_id="play-2")
            offline = ScrobbleDelivery(store, "simulated-token", queue_enabled=True,
                online_check=lambda: False, wall_clock=time.time)
            offline.observe(play, audible=True, enabled=True, artist="Artist", title="Song")
            attempts = []
            def ambiguous(*args, **kwargs):
                attempts.append(1)
                raise TimeoutError("ambiguous response")
            recovered = ScrobbleDelivery(store, "simulated-token", queue_enabled=True,
                online_check=lambda: True, sender=ambiguous)
            self.assertFalse(recovered.flush_queued(still_enabled=lambda: True))
            self.assertEqual(recovered.queued()[0]["state"], "pending")
            self.assertFalse(recovered.flush_queued(still_enabled=lambda: True))
            self.assertEqual(len(attempts), 1)
            self.assertFalse(store.reserve("listenbrainz", "track-key", 123,
                                           account_hash=recovered.account_hash))

    def test_offline_queue_needs_explicit_enablement(self):
        with TemporaryDirectory() as folder:
            delivery = ScrobbleDelivery(ReceiptStore(Path(folder) / "receipts.db"),
                                        "simulated-token", online_check=lambda: False)
            play = ListeningInstance("track", 123, 60, seconds=30)
            self.assertFalse(delivery.observe(play, audible=True, enabled=False,
                                              artist="Artist", title="Song"))
            self.assertEqual(delivery.queued(), [])
