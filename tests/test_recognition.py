import unittest

from recognition import LookupGate


class LookupGateTests(unittest.TestCase):
    def test_opt_in_signal_and_buffer_are_required(self):
        gate = LookupGate()
        self.assertFalse(gate.observe(1, enabled=False, signal=True, buffered_seconds=15))
        self.assertFalse(gate.observe(2, enabled=True, signal=False, buffered_seconds=15))
        self.assertFalse(gate.observe(3, enabled=True, signal=True, buffered_seconds=6))
        self.assertTrue(gate.observe(10, enabled=True, signal=True, buffered_seconds=15))
        self.assertFalse(gate.observe(90, enabled=True, signal=True, buffered_seconds=15))

    def test_gap_is_retained_through_cooldown(self):
        gate = LookupGate()
        self.assertTrue(gate.observe(10, enabled=True, signal=True, buffered_seconds=15))
        gate.complete(11, matched=True)
        self.assertFalse(gate.observe(30, enabled=True, signal=False, buffered_seconds=15))
        self.assertFalse(gate.observe(31, enabled=True, signal=True, buffered_seconds=15))
        self.assertTrue(gate.observe(70, enabled=True, signal=True, buffered_seconds=15))

    def test_retry_after_and_gapless_periodic_check(self):
        gate = LookupGate()
        self.assertTrue(gate.observe(10, enabled=True, signal=True, buffered_seconds=15))
        gate.complete(11, matched=False, retry_after=300)
        self.assertFalse(gate.observe(310, enabled=True, signal=True, buffered_seconds=15))
        self.assertTrue(gate.observe(311, enabled=True, signal=True, buffered_seconds=15))
        gate.complete(312, matched=True)
        self.assertFalse(gate.observe(491, enabled=True, signal=True, buffered_seconds=15))
        self.assertTrue(gate.observe(492, enabled=True, signal=True, buffered_seconds=15))

    def test_confirmed_boundary_gets_faster_attempt_without_overriding_rate_limit(self):
        gate = LookupGate()
        self.assertTrue(gate.observe(0, enabled=True, signal=True, buffered_seconds=12))
        gate.complete(2, matched=False)
        gate.mark_boundary()
        self.assertFalse(gate.observe(24, enabled=True, signal=True, buffered_seconds=12))
        self.assertTrue(gate.observe(25, enabled=True, signal=True, buffered_seconds=12))
        gate.complete(27, matched=False, retry_after=100)
        gate.mark_boundary()
        self.assertFalse(gate.observe(126, enabled=True, signal=True, buffered_seconds=12))
        self.assertTrue(gate.observe(127, enabled=True, signal=True, buffered_seconds=12))

    def test_hourly_limit_applies_even_to_repeated_boundaries(self):
        gate = LookupGate(hourly_limit=2)
        for now in (0, 25):
            gate.mark_boundary()
            self.assertTrue(gate.observe(now, enabled=True, signal=True, buffered_seconds=12))
            gate.complete(now + 1, matched=True)
        gate.mark_boundary()
        self.assertFalse(gate.observe(3599, enabled=True, signal=True, buffered_seconds=12))
        self.assertTrue(gate.observe(3600, enabled=True, signal=True, buffered_seconds=12))
