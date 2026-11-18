"""Recognition and submission work scheduled outside the PCM thread."""
from threading import Event, Thread
from listening import PlaybackTiming
from dataclasses import dataclass
from hashlib import sha256
import socket
import time


@dataclass
class QueuedPlay:
    track_id: str
    started_at: int
    artist: str
    title: str
    album: str | None = None

    @property
    def receipt_key(self):
        return self.track_id, self.started_at

    def eligible(self, *, enabled):
        return enabled


class RecognitionWorker:
    def __init__(self, lookup, enabled, *, interval=0.25):
        self.stop_event = Event()
        self.error = None

        def run():
            while not self.stop_event.wait(interval):
                if enabled():
                    try:
                        lookup()
                        self.error = None
                    except Exception as error:
                        # Provider errors can contain request data. Report only the class.
                        self.error = type(error).__name__

        self.thread = Thread(target=run, daemon=True)
        self.thread.start()

    def stop(self):
        self.stop_event.set()
        self.thread.join(12)


class ScrobbleDelivery:
    """Bridge observed playback to ListenBrainz via a persistent receipt store."""

    def __init__(self, receipts, token, *, timing=None, sender=None, queue_enabled=False,
                 online_check=None, wall_clock=time.time):
        from providers import send_listen

        self.receipts = receipts
        self.token = token
        self.timing = timing if timing is not None else PlaybackTiming()
        self.sender = sender if sender is not None else send_listen
        self.account_hash = sha256(token.encode()).hexdigest()
        self.queue_enabled = bool(queue_enabled)
        self.online_check = online_check or self._listenbrainz_reachable
        self.wall_clock = wall_clock

    @staticmethod
    def _listenbrainz_reachable():
        """Preflight reachability without sending play metadata or credentials."""
        try:
            with socket.create_connection(("api.listenbrainz.org", 443), timeout=1):
                return True
        except OSError:
            return False

    def observe(self, play, *, audible, enabled, artist, title, album=None, still_enabled=None):
        from submission import submit_once

        self.timing.observe(play, audible=audible)
        if self.queue_enabled:
            allowed = enabled and audible and (still_enabled is None or still_enabled())
            if not play.eligible(enabled=allowed):
                return False
            now = int(self.wall_clock())
            key = play.play_id or f"{play.track_id}:{play.started_at}"
            self.receipts.queue_listen(
                play_id=key, account_hash=self.account_hash, track=play.track_id,
                artist=artist, title=title, album=album, listened_at=play.started_at,
                eligible_at=now, expires_at=now + 7 * 86400)
            self.receipts.update_queued_metadata(key, self.account_hash, track=play.track_id,
                                                 artist=artist, title=title, album=album)
            return self.flush_queued(still_enabled=lambda: enabled if still_enabled is None else still_enabled())
        return submit_once(play, enabled=enabled and audible, receipts=self.receipts,
                           still_enabled=still_enabled,
                           service="listenbrainz", account_hash=self.account_hash, send=lambda candidate: self.sender(
                               candidate, artist=artist, title=title, album=album,
                               token=self.token, enabled=True))

    def queued(self, *, limit=25):
        return self.receipts.queued_listens(self.account_hash, limit=limit)

    def delete_queued(self, play_id):
        return self.receipts.delete_queued_listen(play_id, self.account_hash)

    def cancel_queued(self):
        for item in self.queued(limit=100):
            if item["state"] == "queued":
                self.delete_queued(item["play_id"])

    def flush_queued(self, *, still_enabled):
        """Submit one definitely-unsent row; ambiguous sends remain held."""
        if not self.queue_enabled or not still_enabled():
            return False
        items = self.queued()
        item = next((candidate for candidate in items if candidate["state"] == "queued"), None)
        if item is None:
            return False
        try:
            if not self.online_check():
                return False
        except Exception:
            return False
        if item is None or not still_enabled():
            return False
        reserved = self.receipts.claim_queued_listen(item["play_id"], self.account_hash)
        if reserved is None:
            return False
        if not still_enabled():
            # The row is deliberately left pending: a later consent cannot
            # infer whether this reserved send reached the provider.
            return False
        candidate = QueuedPlay(reserved["track"], reserved["listened_at"],
                               reserved["artist"], reserved["title"], reserved["album"])
        try:
            accepted = self.sender(candidate, artist=candidate.artist, title=candidate.title,
                                   album=candidate.album, token=self.token, enabled=True)
        except Exception:
            # Once the request may have left this process, pending is not retried.
            return False
        if accepted is True:
            self.receipts.accept_queued_listen(item["play_id"], self.account_hash)
            return True
        return False
