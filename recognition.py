"""Local policy for scheduling recognition lookups, independent of providers."""
from dataclasses import dataclass, field
import math


@dataclass
class LookupGate:
    interval: float = 60
    gap: float = 0.8
    fallback_interval: float = 180
    next_allowed: float = field(default=0, init=False)
    pending: bool = field(default=True, init=False)
    in_flight: bool = field(default=False, init=False)
    last_completed: float | None = field(default=None, init=False)
    quiet_since: float | None = field(default=None, init=False)
    misses: int = field(default=0, init=False)

    def observe(self, now, *, enabled, signal, buffered_seconds):
        """Return True to request one lookup. Use monotonic seconds."""
        if not enabled:
            self.quiet_since = None
            self.pending = True
            return False
        if not signal:
            if self.quiet_since is None:
                self.quiet_since = now
            return False
        if self.quiet_since is not None:
            self.pending |= now - self.quiet_since >= self.gap
            self.quiet_since = None
        periodic = (
            self.last_completed is not None and
            now - self.last_completed >= self.fallback_interval
        )
        if (self.in_flight or now < self.next_allowed or buffered_seconds < 10
                or not (self.pending or periodic)):
            return False
        self.in_flight = True
        self.pending = False
        self.next_allowed = now + self.interval
        return True

    def complete(self, now, *, matched, retry_after=0):
        """Apply cooldown and retry policy after a result, timeout or rate limit."""
        if not self.in_flight:
            raise RuntimeError("No lookup is outstanding")
        if not math.isfinite(retry_after) or retry_after < 0:
            raise ValueError("Retry delay must be finite and non-negative")
        self.in_flight = False
        self.last_completed = now
        if matched:
            self.misses = 0
        else:
            self.misses = min(self.misses + 1, 4)
            self.pending = True
        backoff = self.interval * 2 ** self.misses if self.misses else 0
        self.next_allowed = max(self.next_allowed, now + backoff, now + retry_after)
