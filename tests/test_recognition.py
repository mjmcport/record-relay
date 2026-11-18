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
        self.assertFalse(gate.observe(31, enabled=True, signal=True, buffered_seconds=1))
        self.assertTrue(gate.observe(40, enabled=True, signal=True, buffered_seconds=10))

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
        self.assertFalse(gate.observe(14, enabled=True, signal=True, buffered_seconds=9))
        self.assertTrue(gate.observe(15, enabled=True, signal=True, buffered_seconds=10))
        gate.complete(17, matched=False, retry_after=100)
        gate.mark_boundary()
        self.assertFalse(gate.observe(116, enabled=True, signal=True, buffered_seconds=12))
        self.assertTrue(gate.observe(117, enabled=True, signal=True, buffered_seconds=12))

    def test_hourly_limit_applies_even_to_repeated_boundaries(self):
        gate = LookupGate(hourly_limit=2)
        for now in (0, 25):
            gate.mark_boundary()
            self.assertTrue(gate.observe(now, enabled=True, signal=True, buffered_seconds=12))
            gate.complete(now + 1, matched=True)
        gate.mark_boundary()
        self.assertFalse(gate.observe(3599, enabled=True, signal=True, buffered_seconds=12))
        self.assertTrue(gate.observe(3600, enabled=True, signal=True, buffered_seconds=12))

    def test_rapid_false_gaps_hit_the_boundary_burst_limit(self):
        gate = LookupGate()
        for now in (11, 23):
            gate.mark_boundary()
            self.assertTrue(gate.observe(now, enabled=True, signal=True, buffered_seconds=10))
            gate.complete(now + 1, matched=True)
        gate.mark_boundary()
        self.assertFalse(gate.observe(35, enabled=True, signal=True, buffered_seconds=12))
        self.assertTrue(gate.observe(71, enabled=True, signal=True, buffered_seconds=12))

    def test_one_quick_retry_for_a_gap_miss_and_no_retry_for_errors(self):
        gate = LookupGate()
        gate.mark_boundary()
        self.assertTrue(gate.observe(10, enabled=True, signal=True, buffered_seconds=10))
        gate.complete(12, matched=False)
        self.assertEqual(gate.last_outcome, "no_match")
        self.assertFalse(gate.observe(31, enabled=True, signal=True, buffered_seconds=12))
        self.assertEqual(gate.waiting_for, "short_retry")
        self.assertTrue(gate.observe(32, enabled=True, signal=True, buffered_seconds=12))
        self.assertEqual(gate.last_trigger, "gap_retry")
        gate.complete(33, matched=False)
        self.assertFalse(gate.observe(53, enabled=True, signal=True, buffered_seconds=12))
        self.assertEqual(gate.waiting_for, "backoff")

        gate = LookupGate()
        gate.mark_boundary()
        self.assertTrue(gate.observe(10, enabled=True, signal=True, buffered_seconds=10))
        gate.complete(12, matched=False, failed=True)
        self.assertEqual(gate.last_outcome, "error")
        self.assertFalse(gate.observe(32, enabled=True, signal=True, buffered_seconds=12))
        self.assertEqual(gate.waiting_for, "backoff")
