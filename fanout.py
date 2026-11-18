"""Run an optional recorder behind a bounded, nonblocking PCM queue."""
from queue import Empty, Full, Queue
from threading import Event, Thread

from recording import record_side


class BackgroundRecording:
    def __init__(self, destination, maximum_seconds, *, blocks=30):
        if blocks < 1:
            raise ValueError("Queue capacity must be positive")
        self.queue = Queue(maxsize=blocks)
        self.closing = Event()
        self.finished = Event()
        self.overrun = Event()
        self.error = None
        self.frames = 0
        self.pending = b""
        self.thread = Thread(
            target=self._run, args=(destination, maximum_seconds), daemon=True,
        )
        self.thread.start()

    def offer(self, block):
        """Accept immutable PCM from the single playback thread without waiting."""
        if self.finished.is_set() or self.closing.is_set():
            return
        try:
            self.queue.put_nowait(block)
        except Full:
            self.overrun.set()
            self.closing.set()

    def read(self, size):
        while not self.pending:
            if self.overrun.is_set():
                raise OSError("Recording fell behind playback")
            try:
                self.pending = self.queue.get(timeout=0.05)
            except Empty:
                if self.closing.is_set():
                    return b""
        block, self.pending = self.pending[:size], self.pending[size:]
        return block

    def stop(self, timeout=5):
        """Call after capture stops offering samples. Drain at most the bounded queue."""
        self.closing.set()
        self.thread.join(timeout)
        if self.thread.is_alive():
            raise TimeoutError("Recorder is still finalizing")
        if self.error:
            raise self.error
        return self.frames

    def _run(self, destination, maximum_seconds):
        try:
            self.frames = record_side(
                self, destination, maximum_seconds, enabled=True,
                before_publish=self._verify_complete,
            )
        except Exception as error:
            self.error = error
        finally:
            self.finished.set()

    def _verify_complete(self):
        self.closing.set()
        if self.overrun.is_set():
            raise OSError("Recording fell behind playback")
