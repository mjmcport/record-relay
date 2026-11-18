"""Local policy for scheduling recognition lookups, independent of providers."""
from collections import deque
from dataclasses import dataclass, field
import math


@dataclass
class LookupGate:
    interval: float = 60
    gap: float = 0.8
    fallback_interval: float = 180
    boundary_window: float = 60
    boundary_limit: int = 2
    hourly_limit: int = 24
    boundary_retry_delay: float = 20
    next_allowed: float = field(default=0, init=False)
    pending: bool = field(default=True, init=False)
    in_flight: bool = field(default=False, init=False)
    last_completed: float | None = field(default=None, init=False)
    quiet_since: float | None = field(default=None, init=False)
    misses: int = field(default=0, init=False)
    boundary_pending: bool = field(default=False, init=False)
    retry_until: float = field(default=0, init=False)
    attempts: deque = field(default_factory=deque, init=False)
    boundary_attempts: deque = field(default_factory=deque, init=False)
    retry_boundary_at: float | None = field(default=None, init=False)
    last_trigger: str | None = field(default=None, init=False)
    last_outcome: str | None = field(default=None, init=False)
    waiting_for: str = field(default="disabled", init=False)

    def mark_boundary(self):
        self.pending = True
        self.boundary_pending = True
        self.retry_boundary_at = None

    def observe(self, now, *, enabled, signal, buffered_seconds, mark_quiet_boundary=True):
        """Return True to request one lookup. Use monotonic seconds."""
        if not enabled:
            self.quiet_since = None
            self.pending = True
            self.boundary_pending = False
            self.retry_boundary_at = None
            return self.wait("disabled")
        if not signal:
            if self.quiet_since is None:
                self.quiet_since = now
            return self.wait("quiet")
        if self.quiet_since is not None:
            if mark_quiet_boundary and now - self.quiet_since >= self.gap:
                self.mark_boundary()
            self.quiet_since = None
        periodic = (
            self.last_completed is not None and
            now - self.last_completed >= self.fallback_interval
        )
        while self.attempts and self.attempts[0] <= now - 3600:
            self.attempts.popleft()
        while self.boundary_attempts and self.boundary_attempts[0] <= now - self.boundary_window:
            self.boundary_attempts.popleft()
        if self.in_flight:
            return self.wait("lookup_in_progress")
        if now < self.retry_until:
            return self.wait("provider_backoff")
        if len(self.attempts) >= self.hourly_limit:
            return self.wait("hourly_budget")
        if buffered_seconds < 10:
            return self.wait("fresh_audio")
        if self.boundary_pending or self.retry_boundary_at is not None:
            if self.retry_boundary_at is not None and now < self.retry_boundary_at:
                return self.wait("short_retry")
            if len(self.boundary_attempts) >= self.boundary_limit:
                return self.wait("gap_budget")
            trigger = "gap" if self.boundary_pending else "gap_retry"
            self.boundary_attempts.append(now)
        else:
            if not (self.pending or periodic):
                return self.wait("periodic_check")
            if now < self.next_allowed:
                return self.wait("backoff")
            trigger = "retry" if self.pending and self.misses else ("periodic" if periodic else "initial")
        self.in_flight = True
        self.pending = False
        self.boundary_pending = False
        self.retry_boundary_at = None
        self.attempts.append(now)
        self.next_allowed = now + self.interval
        self.last_trigger = trigger
        self.waiting_for = "lookup_in_progress"
        return True

    def wait(self, reason):
        self.waiting_for = reason
        return False

    def complete(self, now, *, matched, retry_after=0, failed=False):
        """Apply cooldown and retry policy after a result, timeout or rate limit."""
        if not self.in_flight:
            raise RuntimeError("No lookup is outstanding")
        if not math.isfinite(retry_after) or retry_after < 0:
            raise ValueError("Retry delay must be finite and non-negative")
        self.in_flight = False
        self.last_completed = now
        self.last_outcome = ("provider_backoff" if retry_after else
                             "error" if failed else "match" if matched else "no_match")
        if not (matched or failed or retry_after or self.boundary_pending) and self.last_trigger == "gap":
            self.retry_boundary_at = now + self.boundary_retry_delay
        if matched:
            self.misses = 0
        else:
            self.misses = min(self.misses + 1, 4)
            self.pending = True
        backoff = self.interval * 2 ** self.misses if self.misses else 0
        self.retry_until = max(self.retry_until, now + retry_after)
        self.next_allowed = max(self.next_allowed, now + backoff, self.retry_until)
