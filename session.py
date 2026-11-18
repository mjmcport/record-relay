"""Control optional recording and recognition during a running PCM relay."""
from pathlib import Path
import re
from datetime import datetime
import math
from threading import Lock
from time import monotonic
import struct
import uuid
from queue import Empty, Full, Queue
from threading import Event, Thread

from capture import FRAME_BYTES, RATE, RecentAudio, relay
from fanout import BackgroundRecording
from recognition import LookupGate


class Session:
    """A single PCM writer with independent control and recognition callers."""

    def __init__(self, library, *, clock=monotonic):
        self.library = Path(library)
        self.clock = clock
        self.recent = RecentAudio(seconds=12)
        self.turntable_buffer = RecentAudio(seconds=600)
        self.last_signal_at = None
        self.lock = Lock()
        self.recording = None
        self.recording_id = None
        self.recording_side = None
        self.recording_generation = None
        self.recording_overlap_uncertain = False
        self.recording_started_at = None
        self.recording_prefix_frames = 0
        self.recording_limit_frames = 0
        self.recording_start_frame = None
        self.last_side_a_capture_generation = None
        self.last_side_a_end_frame = None
        self.last_side_a_generation = None
        self.boot_generation = str(uuid.uuid4())
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
        self.live_view = False
        self.visual_sequence = 0
        self.visual_samples = None
        self.visual_dropped_frames = 0
        self.visual_frame_time = None
        self.visual_generation = 0
        self._visual_blocks = Queue(maxsize=1)
        self._visual_stop = Event()
        self._visual_thread = None

    def set_live_view(self, enabled):
        with self.lock:
            self.live_view = bool(enabled)
            if not self.live_view:
                self.visual_generation += 1
                self.visual_samples = None
                self.visual_frame_time = None
                while True:
                    try:
                        self._visual_blocks.get_nowait()
                    except Empty:
                        break
            elif self._visual_thread is None or not self._visual_thread.is_alive():
                self._visual_stop.clear()
                self._visual_thread = Thread(target=self._visual_loop, daemon=True,
                                             name="record-relay-visualizer")
                self._visual_thread.start()

    def _visual_loop(self):
        while not self._visual_stop.is_set():
            try:
                block = self._visual_blocks.get(timeout=0.1)
            except Empty:
                continue
            with self.lock:
                enabled = self.live_view
                generation = self.visual_generation
            if not enabled:
                continue
            frame_count = len(block) // FRAME_BYTES
            samples = []
            for bucket in range(64):
                first = bucket * frame_count // 64
                last = max(first + 1, (bucket + 1) * frame_count // 64)
                peak = 0
                for left, right in struct.iter_unpack("<hh", block[first * FRAME_BYTES:last * FRAME_BYTES]):
                    peak = max(peak, abs((left + right) / 2))
                samples.append(round(peak / 32768, 4))
            with self.lock:
                if self.live_view and self.visual_generation == generation:
                    self.visual_sequence += 1
                    self.visual_samples = samples
                    self.visual_frame_time = self.clock()

    def close_live_view(self):
        self._visual_stop.set()
        if self._visual_thread is not None:
            self._visual_thread.join(1)

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

    def start_recording(self, name, maximum_seconds=1800, *, buffered=False, request_id=None, side=None,
                        buffered_start_frame=None, expected_capture_generation=None, overlap_ack=False,
                        side_a_boundary_frame=None):
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
            if self.last_signal_at is not None and self.clock() - self.last_signal_at >= 600:
                self.turntable_buffer.clear()
                self.last_signal_at = None
            bounds = self.turntable_buffer.bounds()
            capture_generation = f"{self.boot_generation}:{bounds['generation']}"
            if expected_capture_generation is not None and expected_capture_generation != capture_generation:
                raise ValueError("Capture generation changed; refresh the selected buffer interval.")
            prefix = ()
            selection = None
            overlap_uncertain = False
            if buffered:
                if bounds["oldest_frame"] >= bounds["newest_frame"]:
                    raise ValueError("There is no buffered turntable audio to save.")
                start_frame = bounds["oldest_frame"]
                if side == "B":
                    if self.last_side_a_end_frame is not None and self.last_side_a_generation == bounds["generation"]:
                        if self.last_side_a_end_frame < bounds["oldest_frame"]:
                            overlap_uncertain = True
                        start_frame = max(start_frame, self.last_side_a_end_frame)
                    else:
                        overlap_uncertain = True
                if buffered_start_frame is not None:
                    if type(buffered_start_frame) is not int:
                        raise ValueError("Choose an exact buffered start frame.")
                    if (side == "B" and self.last_side_a_end_frame is not None
                            and self.last_side_a_generation == bounds["generation"]
                            and buffered_start_frame < self.last_side_a_end_frame and not overlap_ack):
                        raise ValueError("Confirm the intentional overlap with Side A.")
                    start_frame = buffered_start_frame
                    if (side == "B" and type(side_a_boundary_frame) is int
                            and expected_capture_generation == capture_generation):
                        overlap_uncertain = buffered_start_frame < side_a_boundary_frame
                if start_frame >= bounds["newest_frame"]:
                    raise ValueError("There is no buffered audio after the last saved Side A boundary.")
                selection, prefix = self.turntable_buffer.snapshot_selection(
                    start_frame, generation=bounds["generation"])
            if buffered and not prefix:
                raise ValueError("There is no buffered turntable audio to save.")
            if sum(map(len, prefix)) >= maximum_seconds * RATE * FRAME_BYTES:
                raise ValueError("Choose a recording limit longer than the buffered audio.")
            self.recording = BackgroundRecording(destination, maximum_seconds, blocks=300 if buffered else 30, prefix=prefix)
            self.recording_id = request_id
            self.recording_side = side
            self.recording_generation = bounds["generation"]
            self.recording_overlap_uncertain = overlap_uncertain
            self.recording_started_at = self.clock()
            self.recording_prefix_frames = self.recording.offered_frames
            self.recording_limit_frames = maximum_seconds * RATE
            self.recording_start_frame = (selection["start_frame"] if selection
                                          else bounds["newest_frame"])
            if self.track is not None:
                frame = max(0, self.recording.offered_frames - self.frames_since_gap) if buffered else 0
                self.recording.add_match(frame, self.track)

    def stop_recording(self, expected_id=None):
        with self.lock:
            recorder = self.recording
            if recorder is None:
                return 0
            if expected_id is not None and expected_id != self.recording_id:
                raise RuntimeError("The active recording belongs to a different save request")
            side = self.recording_side
            generation = self.recording_generation
            boundary_frame = None
            boundary_capture_generation = None
            if side == "A":
                bounds = self.turntable_buffer.bounds()
                if bounds["generation"] == generation:
                    boundary_frame = bounds["newest_frame"]
                    boundary_capture_generation = f"{self.boot_generation}:{generation}"
            recorder.closing.set()
        try:
            frames = recorder.stop()
            if side == "A" and frames and boundary_frame is not None:
                with self.lock:
                    self.last_side_a_end_frame = boundary_frame
                    self.last_side_a_generation = generation
                    self.last_side_a_capture_generation = boundary_capture_generation
            return frames
        finally:
            with self.lock:
                if self.recording is recorder and recorder.finished.is_set():
                    self.recording = None
                    self.recording_id = None
                    self.recording_side = None
                    self.recording_generation = None
                    self.recording_overlap_uncertain = False
                    self.recording_started_at = None
                    self.recording_prefix_frames = 0
                    self.recording_limit_frames = 0
                    self.recording_start_frame = None

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
                self.last_signal_at = self.last_audio
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
            if self.last_signal_at is not None:
                if self.last_audio - self.last_signal_at >= 600:
                    self.turntable_buffer.clear()
                    self.last_signal_at = None
                else:
                    self.turntable_buffer.append(block)
            if self.recording is not None:
                self.recording.offer(block)
            if self.live_view:
                try:
                    self._visual_blocks.put_nowait(block)
                except Full:
                    try:
                        self._visual_blocks.get_nowait()
                        self.visual_dropped_frames += 1
                    except Empty:
                        pass
                    try:
                        self._visual_blocks.put_nowait(block)
                    except Full:
                        self.visual_dropped_frames += 1

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
            elif recording.closing.is_set():
                state = "finalizing"
            else:
                state = "recording"
            now = self.clock()
            signal = self.signal and self.last_audio is not None and now - self.last_audio < 1
            input_age = None if self.last_audio is None else max(0, now - self.last_audio)
            input_state = ("no_input" if input_age is None else "stale" if input_age >= 1
                           else "signal" if self.signal else "silence")
            bounds = self.turntable_buffer.bounds()
            side_a_end_available = (self.last_side_a_end_frame is not None
                                    and self.last_side_a_generation == bounds["generation"]
                                    and self.last_side_a_end_frame >= bounds["oldest_frame"])
            projected_late_uncertain = (self.recording_overlap_uncertain if self.recording is not None
                                        else not side_a_end_available)
            track = self.track if self.track_seen is not None and now - self.track_seen < 210 else None
            label = ""
            if track and all(isinstance(track.get(k), str) for k in ("artist", "album")):
                label = re.sub(r"[^A-Za-z0-9 ._-]+", "", f'{track["artist"]} - {track["album"]}').strip(" .")
            suggestion = f"{label[:100]} - Side A.wav" if label else None
            if suggestion is not None and (self.library / suggestion).exists():
                suggestion = f"{label[:85]} - {datetime.now():%Y%m%d-%H%M%S}.wav"
            return {"suggested_filename": suggestion,
                    "capture_generation": f"{self.boot_generation}:{self.turntable_buffer.generation}",
                    "boot_generation": self.boot_generation,
                    "recording_id": self.recording_id,
                    "recording_side": self.recording_side,
                    "recording_frames": 0 if recording is None else recording.offered_frames,
                    "recording_prefix_frames": self.recording_prefix_frames,
                    "recording_elapsed_seconds": (None if self.recording_started_at is None
                                                   else max(0, now - self.recording_started_at)),
                    "recording_limit_frames": self.recording_limit_frames,
                    "buffered_oldest_frame": bounds["oldest_frame"],
                    "buffered_newest_frame": bounds["newest_frame"],
                    "side_a_end_frame": self.last_side_a_end_frame,
                    "side_a_end_capture_generation": self.last_side_a_capture_generation,
                    "recording_start_frame": self.recording_start_frame,
                    "late_save_uncertain": projected_late_uncertain,
                    "buffered_seconds": (self.turntable_buffer.size / (RATE * FRAME_BYTES)
                        if self.last_signal_at is not None and now - self.last_signal_at < 600 else 0),
                    "buffer_limit_seconds": 600,
                    "identify": self.identify, "scrobble": self.scrobble,
                    "live_view": self.live_view,
                    "visual_sequence": self.visual_sequence,
                    "visual_dropped_frames": self.visual_dropped_frames,
                    "visual_age_seconds": (None if self.visual_frame_time is None else
                                            max(0, now - self.visual_frame_time)),
                    "visual_samples": self.visual_samples if self.live_view else None,
                    "signal": signal, "recording": state, "track": track,
                    "input_state": input_state,
                    "input_age_seconds": None if input_age is None else round(input_age, 2),
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
