import unittest
import json
from pathlib import Path
import sys
from tempfile import TemporaryDirectory
from unittest.mock import patch

from listening import ListeningCoordinator, ListeningInstance, PlaybackTiming


class ListeningTests(unittest.TestCase):
    def test_counts_only_matching_audible_intervals(self):
        play = ListeningInstance("test-track", 42, 120)
        play.add_interval(40, audible=True, same_track=True)
        play.add_interval(80, audible=False, same_track=True)
        play.add_interval(80, audible=True, same_track=False)
        self.assertFalse(play.eligible(enabled=True))
        play.add_interval(20, audible=True, same_track=True)
        self.assertFalse(play.eligible(enabled=False))
        self.assertTrue(play.eligible(enabled=True))
        play.submitted = True
        self.assertFalse(play.eligible(enabled=True))

    def test_unknown_duration_and_short_track(self):
        unknown = ListeningInstance("test-track", 42, None)
        short = ListeningInstance("test-track", 43, 30)
        for play in (unknown, short):
            play.add_interval(240, audible=True, same_track=True)
        self.assertTrue(unknown.eligible(enabled=True))
        self.assertFalse(short.eligible(enabled=True))
        self.assertNotEqual(unknown.receipt_key, short.receipt_key)

    def test_standalone_local_evidence_uses_coordinator_without_ma(self):
        sent = []
        timing = PlaybackTiming(clock=iter(range(100)).__next__)
        class Delivery:
            def __init__(self):
                self.timing = timing
            def observe(self, play, **kwargs):
                timing.observe(play, audible=kwargs["audible"])
                if play.eligible(enabled=kwargs["enabled"]):
                    sent.append(play.receipt_key)
                    play.submitted = True
                    return True
                return False
        coordinator = ListeningCoordinator(None, Delivery(), wall_clock=lambda: 500)
        status = {"scrobble": True, "signal": True, "local_listening": True,
                  "track_epoch": 4, "track": {"artist": "Artist", "title": "Song",
                                               "duration_seconds": 60}}
        for _ in range(32):
            coordinator.tick(status, lambda _track, local: local)
        self.assertEqual(len(sent), 1)
        self.assertEqual(coordinator.play.started_at, 500)

    def test_rechecks_play_identity_after_observer_returns(self):
        sent = []
        state = {"epoch": 1}
        timing = PlaybackTiming(clock=iter(range(100)).__next__)
        class Observer:
            def audible(self):
                state["epoch"] = 2
                return True
        class Delivery:
            def __init__(self):
                self.timing = timing
            def observe(self, play, **kwargs):
                if kwargs["enabled"] and kwargs["audible"]:
                    sent.append(play)
                return False
        coordinator = ListeningCoordinator(Observer(), Delivery(), wall_clock=lambda: 1)
        status = {"scrobble": True, "signal": True, "local_listening": False,
                  "track_epoch": 1, "track": {"artist": "Artist", "title": "Song"}}
        coordinator.tick(status, lambda _track, _local: state["epoch"] == 1)
        self.assertFalse(sent)  # delivery is gated after observer I/O

    def test_scrobble_setup_writes_listenbrainz_only_config(self):
        from integrations import setup_scrobble
        with TemporaryDirectory() as folder:
            output = Path(folder) / "listen.json"
            with patch.object(sys, "argv", ["setup_scrobble.py", str(output)]), \
                    patch.object(sys.stdin, "isatty", return_value=True), \
                    patch.object(setup_scrobble, "getpass", return_value="simulated-token"):
                setup_scrobble.main()
            self.assertEqual(json.loads(output.read_text()),
                             {"listenbrainz_token": "simulated-token"})
            self.assertEqual(output.stat().st_mode & 0o777, 0o600)
