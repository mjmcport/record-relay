import io
import json
import unittest

from music_assistant import MusicAssistantObserver, player_is_audible


PLAYER = {"player_id": "speaker", "type": "player", "available": True,
          "playback_state": "playing", "active_source": "source:test",
          "volume_muted": False, "volume_level": 30}


class MusicAssistantTests(unittest.TestCase):
    def test_exact_non_muted_physical_target_is_required(self):
        self.assertTrue(player_is_audible(PLAYER, "speaker", "source:test"))
        for changes in (
            {"volume_muted": None}, {"volume_muted": True}, {"volume_level": 0},
            {"volume_level": True}, {"active_source": "source:other"},
            {"playback_state": "paused"}, {"available": False},
            {"type": "group"}, {"group_childs": ["other"]},
        ):
            self.assertFalse(player_is_audible(PLAYER | changes, "speaker", "source:test"))

    def test_observer_uses_only_read_command_and_private_transport(self):
        def simulated(request, timeout):
            self.assertEqual(json.loads(request.data)["command"], "players/all")
            self.assertEqual(request.get_header("Authorization"), "Bearer mock")
            return io.BytesIO(json.dumps([PLAYER]).encode())

        options = ("mock", "speaker", "source:test")
        with self.assertRaises(ValueError):
            MusicAssistantObserver("http://remote.invalid", *options, transport=simulated)
        observer = MusicAssistantObserver("http://127.0.0.1:8095", *options, transport=simulated)
        self.assertTrue(observer.audible())
        observer.transport = lambda request, timeout: io.BytesIO(b'{"error":"unavailable"}')
        self.assertFalse(observer.audible())

    def test_no_delivery_after_opt_out_during_poll_and_no_duplicate_after_silence(self):
        from pathlib import Path
        from tempfile import TemporaryDirectory
        from music_assistant import ScrobbleMonitor
        from receipts import ReceiptStore
        from worker import PlaybackTiming, ScrobbleDelivery

        class Observer:
            def audible(self):
                return True

        with TemporaryDirectory() as temporary:
            sent = []
            clock = iter(range(600))
            delivery = ScrobbleDelivery(
                ReceiptStore(Path(temporary) / "receipts.db"), "mock",
                timing=PlaybackTiming(clock=lambda: next(clock)),
                sender=lambda play, **_metadata: sent.append(play) is None,
            )
            monitor = ScrobbleMonitor(Observer(), delivery, wall_clock=lambda: 100)
            state = {"scrobble": True, "signal": True, "track": {"artist": "A", "title": "B"}}

            for _ in range(240):
                self.assertFalse(monitor.tick(state, lambda _track: True))
            self.assertFalse(monitor.tick(state, lambda _track: False))
            self.assertFalse(monitor.tick(state, lambda _track: True))
            self.assertTrue(monitor.tick(state, lambda _track: True))
            monitor.tick(state | {"signal": False}, lambda _track: True)
            self.assertFalse(monitor.tick(state, lambda _track: True))
            self.assertEqual(len(sent), 1)
