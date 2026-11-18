"""Run an optional recorder behind a bounded, nonblocking PCM queue."""
import json
from pathlib import Path
from tempfile import NamedTemporaryFile
from collections import deque
from queue import Empty, Full, Queue
from threading import Event, Thread

from recording import record_side


class BackgroundRecording:
    def __init__(self, destination, maximum_seconds, *, blocks=30, prefix=()):
        if blocks < 1:
            raise ValueError("Queue capacity must be positive")
        self.queue = Queue(maxsize=blocks)
        self.closing = Event()
        self.finished = Event()
        self.overrun = Event()
        self.error = None
        self.frames = 0
        self.prefix = deque(prefix)
        self.offered_frames = sum(len(block) // 4 for block in prefix)
        self.cues = []
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
            self.offered_frames += len(block) // 4
        except Full:
            self.overrun.set()
            self.closing.set()

    def add_match(self, frame, match):
        if self.closing.is_set():
            return
        tags = {key: value[:300] for key in ("title", "artist", "album")
                if isinstance(value := match.get(key), str) and value.strip()}
        if not tags.get("title") or not tags.get("artist"):
            return
        if len(self.cues) < 512 and (not self.cues or self.cues[-1]["tags"] != tags):
            self.cues.append({"frame": max(0, frame), "tags": tags})

    def _write_cues(self, destination):
        cues = [cue for cue in self.cues if cue["frame"] < self.frames]
        if not cues:
            return
        sidecar = destination.with_suffix(".matches.json")
        with NamedTemporaryFile(dir=destination.parent, delete=False) as output:
            scratch = Path(output.name)
            try:
                output.write(json.dumps(cues).encode())
                output.close()
                sidecar.hardlink_to(scratch)
            finally:
                scratch.unlink(missing_ok=True)

    def read(self, size):
        while not self.pending:
            if self.overrun.is_set():
                raise OSError("Recording fell behind playback")
            if self.prefix:
                self.pending = self.prefix.popleft()
                continue
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
        else:
            try:
                self._write_cues(destination)
            except OSError:
                # Optional tags never invalidate an already-published WAV.
                pass
        finally:
            self.finished.set()

    def _verify_complete(self):
        self.closing.set()
        if self.overrun.is_set():
            raise OSError("Recording fell behind playback")
