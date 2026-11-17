import unittest

from listening import ListeningInstance


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
