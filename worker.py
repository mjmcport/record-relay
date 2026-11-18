"""Recognition and submission work scheduled outside the PCM thread."""
from threading import Event, Thread
from time import monotonic


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


class PlaybackTiming:
    """Count short intervals only while the intended sink reports audible playback."""

    def __init__(self, *, clock=monotonic, maximum_interval=2):
        self.clock = clock
        self.maximum_interval = maximum_interval
        self.previous = None

    def observe(self, play, *, audible):
        now = self.clock()
        previous = self.previous
        self.previous = (now, play.receipt_key, audible)
        if previous is not None:
            stamp, track, was_audible = previous
            elapsed = now - stamp
            if 0 <= elapsed <= self.maximum_interval:
                play.add_interval(elapsed, audible=audible and was_audible,
                                  same_track=track == play.receipt_key)


class ScrobbleDelivery:
    """Bridge observed playback to ListenBrainz via a persistent receipt store."""

    def __init__(self, receipts, token, *, timing=None, sender=None):
        from providers import send_listen

        self.receipts = receipts
        self.token = token
        self.timing = timing if timing is not None else PlaybackTiming()
        self.sender = sender if sender is not None else send_listen

    def observe(self, play, *, audible, enabled, artist, title, album=None):
        from submission import submit_once

        self.timing.observe(play, audible=audible)
        return submit_once(play, enabled=enabled and audible, receipts=self.receipts,
                           service="listenbrainz", send=lambda candidate: self.sender(
                               candidate, artist=artist, title=title, album=album,
                               token=self.token, enabled=True))
