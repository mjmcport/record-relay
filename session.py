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

    def __init__(self, library, *, clock=monotonic):
        self.library = Path(library)
        self.clock = clock
        self.recent = RecentAudio(seconds=12)
        self.lock = Lock()
        self.recording = None
        self.identify = False
        self.scrobble = False
        self.local_until = None
        self.track_seen = None
        self.recognition_epoch = 0
        self.track_epoch = 0
        self.gate = LookupGate()
        self.track = None
        self.frames_since_gap = 0
        self.quiet_frames = 0
        self.last_boundary = None
        self.last_recognition_duration = None
        self.signal = False
        self.last_audio = None

    def set_identify(self, enabled):
        with self.lock:
            self.identify = bool(enabled)
            self.recognition_epoch += 1
            self.track = None
            if not self.identify:
                self.scrobble = False
                self.local_until = None
            self.gate.observe(self.clock(), enabled=False, signal=False, buffered_seconds=0)

    def set_scrobble(self, enabled):
        with self.lock:
            if enabled and not self.identify:
                raise ValueError("Enable Identify before enabling Scrobble")
            self.scrobble = enabled
            if not enabled:
                self.local_until = None

    def set_local_listening(self, enabled):
        with self.lock:
            if enabled and not (self.identify and self.scrobble):
                raise ValueError("Enable Identify and Scrobble first")
            self.local_until = self.clock() + 1800 if enabled else None

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
            if self.track is not None:
                self.recording.add_match(0, self.track)

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
            frames = len(block) // FRAME_BYTES
            self.last_audio = self.clock()
            self.signal = rms >= 32768 * 10 ** (-45 / 20)
            if self.signal:
                self.quiet_frames = 0
            else:
                self.quiet_frames += frames
            boundary = int(self.gate.gap * RATE)
            if self.quiet_frames >= boundary:
                if self.quiet_frames - frames < boundary:
                    self.recognition_epoch += 1
                    self.track_epoch += 1
                    self.last_boundary = self.last_audio
                    self.track = None
                    self.gate.mark_boundary()
                self.frames_since_gap = 0
            else:
                self.frames_since_gap = min(12 * RATE, self.frames_since_gap + frames)
            if self.recording is not None:
                self.recording.offer(block)

    def lookup_if_due(self, recognize, *, clock=None):
        """Run in a worker. The injected recognizer receives a short WAV snapshot."""
        clock = clock or self.clock
        with self.lock:
            signal = self.signal and self.last_audio is not None and self.clock() - self.last_audio < 1
            if not self.gate.observe(clock(), enabled=self.identify, signal=signal,
                                     buffered_seconds=self.frames_since_gap / RATE,
                                     mark_quiet_boundary=False):
                return False
            epoch = self.recognition_epoch
            frames = self.frames_since_gap
            recorder = self.recording
            sample_frame = max(0, recorder.offered_frames - frames) if recorder else 0
        result = None
        retry_after = 0
        failed = False
        started = clock()
        try:
            result = recognize(self.recent.wav_snapshot(frames=frames))
            return bool(result)
        except Exception as error:
            failed = True
            if getattr(error, "status", getattr(error, "code", None)) == 429:
                headers = getattr(error, "headers", None) or {}
                value = headers.get("Retry-After", "")
                retry_after = float(value) if value.isascii() and value.isdecimal() and len(value) < 10 else 300
            raise
        finally:
            with self.lock:
                finished = clock()
                self.last_recognition_duration = max(0, finished - started)
                self.gate.complete(finished, matched=bool(result), retry_after=retry_after, failed=failed)
                if self.identify and self.recognition_epoch == epoch and result:
                    self.track = result
                    self.track_seen = self.clock()
                    if recorder is not None and recorder is self.recording:
                        recorder.add_match(sample_frame, result)

    def status(self):
        with self.lock:
            recording = self.recording
            if recording is None:
                state = "idle"
            elif recording.finished.is_set():
                state = "failed" if recording.error else "finished"
            else:
                state = "recording"
            now = self.clock()
            signal = self.signal and self.last_audio is not None and now - self.last_audio < 1
            track = self.track if self.track_seen is not None and now - self.track_seen < 210 else None
            return {"identify": self.identify, "scrobble": self.scrobble,
                    "signal": signal, "recording": state, "track": track,
                    "local_listening": self.local_until is not None and self.local_until > now,
                    "track_epoch": self.track_epoch,
                    "recognition_timing": {
                        "seconds_since_boundary": None if self.last_boundary is None else
                        round(now - self.last_boundary, 2),
                        "last_lookup_seconds": self.last_recognition_duration,
                        "last_trigger": self.gate.last_trigger,
                        "last_outcome": self.gate.last_outcome,
                        "waiting_for": self.gate.waiting_for if self.identify else "disabled",
                        "attempts_this_hour": sum(stamp > now - 3600 for stamp in self.gate.attempts),
                        "gap_attempts_this_minute": sum(
                            stamp > now - self.gate.boundary_window for stamp in self.gate.boundary_attempts),

                    }}

    def run(self, source, playback):
        return relay(source, playback, self.recent, recorder=self)
