"""Venue-independent scrobble eligibility with no network side effects."""
from dataclasses import dataclass
import json
import math
from time import monotonic
from uuid import NAMESPACE_URL, uuid5


@dataclass
class ListeningInstance:
    track_id: str
    started_at: int
    duration: float | None
    seconds: float = 0
    submitted: bool = False
    play_id: str | None = None
    capture_generation: str | None = None

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


class PlaybackTiming:
    """Count verified playback intervals no longer than the observation window."""

    def __init__(self, *, clock=monotonic, maximum_interval=2):
        self.clock = clock
        self.maximum_interval = maximum_interval
        self.previous = None

    def observe(self, play, *, audible):
        now = self.clock()
        previous = self.previous
        self.previous = (now, play.receipt_key, audible)
        if previous is not None:
            stamp, key, was_audible = previous
            elapsed = now - stamp
            if 0 <= elapsed <= self.maximum_interval:
                play.add_interval(elapsed, audible=audible and was_audible,
                                  same_track=key == play.receipt_key)


class ListeningCoordinator:
    """Provider-neutral play/evidence coordinator with optional MA observation."""

    def __init__(self, observer, delivery, *, wall_clock):
        self.observer = observer
        self.delivery = delivery
        self.wall_clock = wall_clock
        self.play = None
        self.track_epoch = None
        self.local_listening = False
        self.clock_trusted = False

    @staticmethod
    def identity(track):
        artist, title = track.get("artist"), track.get("title")
        if not all(isinstance(value, str) and value.strip() for value in (artist, title)):
            return None
        return json.dumps([artist.casefold().strip(), title.casefold().strip()])

    def progress(self, status):
        play = self.play
        track = status.get("track")
        if (not status.get("scrobble") or not isinstance(track, dict)
                or play is None or self.track_epoch != status.get("track_epoch")
                or self.identity(track) != play.track_id):
            return None
        return {"seconds": int(play.seconds), "required_seconds": play.required_seconds,
                "submitted": play.submitted}

    def tick(self, status, still_enabled, *, queue_consent=None):
        track = status.get("track")
        if not status.get("scrobble"):
            if hasattr(self.delivery, "cancel_queued"):
                self.delivery.cancel_queued()
            self.delivery.timing.previous = None
            return False
        wall_now = int(self.wall_clock())
        self.clock_trusted = wall_now >= 1_577_836_800
        if not self.clock_trusted:
            self.delivery.timing.previous = None
            return False
        if not status.get("signal") or not isinstance(track, dict):
            self.delivery.timing.previous = None
            if hasattr(self.delivery, "flush_queued"):
                self.delivery.flush_queued(still_enabled=lambda: (
                    queue_consent() if queue_consent is not None else bool(status.get("scrobble"))))
            return False
        identity = self.identity(track)
        if identity is None:
            self.delivery.timing.previous = None
            if hasattr(self.delivery, "flush_queued"):
                self.delivery.flush_queued(still_enabled=lambda: (
                    queue_consent() if queue_consent is not None else bool(status.get("scrobble"))))
            return False
        epoch = status.get("track_epoch")
        capture_generation = status.get("capture_generation")
        duration = track.get("duration_seconds")
        if type(duration) not in (int, float) or not math.isfinite(duration) or duration <= 0:
            duration = None
        if (self.play is None or self.track_epoch != epoch
                or self.play.capture_generation != capture_generation):
            occurrence = f"{capture_generation or 'unknown'}:{epoch}"
            self.play = ListeningInstance(
                identity, int(self.wall_clock()), duration,
                play_id=str(uuid5(NAMESPACE_URL, occurrence)),
                capture_generation=capture_generation)
            self.track_epoch = epoch
        else:
            # A later metadata refresh can correct a title without creating a
            # second listening occurrence or resetting its verified intervals.
            self.play.track_id = identity
            if self.play.duration is None:
                self.play.duration = duration
        local = status.get("local_listening") is True
        if local != self.local_listening:
            self.delivery.timing.previous = None
            self.local_listening = local
        try:
            audible = local or (self.observer is not None and self.observer.audible())
        except Exception:
            self.delivery.timing.previous = None
            raise
        # Revalidate current consent and play after potentially blocking observer I/O.
        enabled = still_enabled(track, local)
        if not enabled:
            self.delivery.timing.previous = None
        return self.delivery.observe(self.play, audible=audible and enabled,
                                     enabled=enabled, artist=track["artist"],
                                     title=track["title"], album=track.get("album"),
                                     still_enabled=lambda: still_enabled(track, local))
