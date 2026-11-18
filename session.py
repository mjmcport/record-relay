"""Control optional recording and recognition during a running PCM relay."""
from pathlib import Path
import math
from threading import Lock
from time import monotonic
import struct

from capture import FRAME_BYTES, RATE, RecentAudio, relay
from fanout import BackgroundRecording
from recognition import LookupGate


class Session:
    """A single PCM writer with independent control and recognition callers."""

    def __init__(self, library):
        self.library = Path(library)
        self.recent = RecentAudio(seconds=12)
        self.lock = Lock()
        self.recording = None
        self.identify = False
        self.scrobble = False
        self.track_seen = None
        self.recognition_epoch = 0
        self.gate = LookupGate()
        self.track = None
        self.seconds = 0
        self.signal = False
        self.last_audio = None

    def set_identify(self, enabled):
        with self.lock:
            self.identify = bool(enabled)
            self.recognition_epoch += 1
            self.track = None
            if not self.identify:
                self.scrobble = False
            self.gate.observe(monotonic(), enabled=False, signal=False, buffered_seconds=0)

    def set_scrobble(self, enabled):
        with self.lock:
            if enabled and not self.identify:
                raise ValueError("Enable Identify before enabling Scrobble")
            self.scrobble = enabled

    def start_recording(self, name, maximum_seconds=1800):
        if Path(name).name != name or not name.lower().endswith(".wav"):
            raise ValueError("Choose a plain WAV filename")
        if type(maximum_seconds) is not int or not 1 <= maximum_seconds <= 3600:
            raise ValueError("Recording limit must be 1–3600 seconds")
        with self.lock:
            if self.recording is not None:
                raise RuntimeError("Stop or finalize the previous recording first")
            destination = self.library / name
            if destination.exists() or destination.is_symlink():
                raise FileExistsError(destination)
            self.recording = BackgroundRecording(destination, maximum_seconds)

    def stop_recording(self):
        with self.lock:
            recorder = self.recording
            if recorder is None:
                return 0
            recorder.closing.set()
        try:
            return recorder.stop()
        finally:
            with self.lock:
                if self.recording is recorder and recorder.finished.is_set():
                    self.recording = None

    def offer(self, block):
        """Observe PCM after playback accepted it. Never perform disk or network I/O here."""
        if not block:
            return
        energy = sum(sample * sample for (sample,) in struct.iter_unpack("<h", block))
        rms = math.sqrt(energy / (len(block) // 2))
        with self.lock:
            self.seconds = min(12, self.seconds + len(block) / (FRAME_BYTES * RATE))
            self.last_audio = monotonic()
            self.signal = rms >= 32768 * 10 ** (-45 / 20)
            if self.recording is not None:
                self.recording.offer(block)

    def lookup_if_due(self, recognize, *, clock=monotonic):
        """Run in a worker. The injected recognizer receives a short WAV snapshot."""
        with self.lock:
            signal = self.signal and self.last_audio is not None and monotonic() - self.last_audio < 1
            if not self.gate.observe(clock(), enabled=self.identify, signal=signal,
                                     buffered_seconds=self.seconds):
                return False
            epoch = self.recognition_epoch
        result = None
        try:
            result = recognize(self.recent.wav_snapshot())
            return bool(result)
        finally:
            with self.lock:
                self.gate.complete(clock(), matched=bool(result))
                if self.identify and self.recognition_epoch == epoch and result:
                    self.track = result
                    self.track_seen = monotonic()

    def status(self):
        with self.lock:
            recording = self.recording
            if recording is None:
                state = "idle"
            elif recording.finished.is_set():
                state = "failed" if recording.error else "finished"
            else:
                state = "recording"
            now = monotonic()
            signal = self.signal and self.last_audio is not None and now - self.last_audio < 1
            track = self.track if self.track_seen is not None and now - self.track_seen < 210 else None
            return {"identify": self.identify, "scrobble": self.scrobble,
                    "signal": signal, "recording": state, "track": track}

    def run(self, source, playback):
        return relay(source, playback, self.recent, recorder=self)
