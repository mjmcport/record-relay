"""Venue-independent scrobble eligibility with no network side effects."""
from dataclasses import dataclass
import math


@dataclass
class ListeningInstance:
    track_id: str
    started_at: int
    duration: float | None
    seconds: float = 0
    submitted: bool = False

    def add_interval(self, seconds, *, audible, same_track):
        """Count only observed intervals with matching, audible playback."""
        if not math.isfinite(seconds) or seconds < 0:
            raise ValueError("Listening interval must be finite and non-negative")
        if audible and same_track:
            self.seconds += seconds

    @property
    def required_seconds(self):
        if self.duration is None:
            return 240
        if not math.isfinite(self.duration) or self.duration <= 30:
            return None
        return min(self.duration / 2, 240)

    def eligible(self, *, enabled):
        threshold = self.required_seconds
        return enabled and not self.submitted and threshold is not None and self.seconds >= threshold

    @property
    def receipt_key(self):
        """Stable key for a caller's persistent submission receipt."""
        return (self.track_id, self.started_at)
