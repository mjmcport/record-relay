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

    def eligible(self, *, enabled):
        # Unknown length requires four minutes of verified listening.
        if self.duration is None:
            threshold = 240
        elif not math.isfinite(self.duration) or self.duration <= 30:
            return False
        else:
            threshold = min(self.duration / 2, 240)
        return enabled and not self.submitted and self.seconds >= threshold

    @property
    def receipt_key(self):
        """Stable key for a caller's persistent submission receipt."""
        return (self.track_id, self.started_at)
