import io
import threading
import unittest
import wave

from capture import FRAME_BYTES, RATE, RecentAudio, relay


class FragmentedInput(io.BytesIO):
    def read(self, size):
        return super().read(min(size, 777))


class ShortWrites(io.BytesIO):
    def write(self, data):
        return super().write(data[:511])


class CaptureTests(unittest.TestCase):
    def test_fragmented_source_and_partial_sink_preserve_every_byte(self):
        audio = bytes(range(256)) * 2048
        sink = ShortWrites()
        recent = RecentAudio(seconds=1)
        self.assertEqual(relay(FragmentedInput(audio), sink, recent), len(audio) // FRAME_BYTES)
        self.assertEqual(sink.getvalue(), audio)
        with wave.open(io.BytesIO(recent.wav_snapshot())) as reader:
            self.assertEqual(reader.getframerate(), RATE)
            self.assertEqual(reader.readframes(RATE), audio[-RATE * FRAME_BYTES:])

    def test_consumer_can_hold_snapshot_without_holding_up_playback(self):
        recent = RecentAudio(seconds=1)
        recent.append(b"a" * FRAME_BYTES)
        done = threading.Event()
        snapshot = recent.snapshot()
        thread = threading.Thread(target=lambda: (recent.append(b"b" * FRAME_BYTES), done.set()))
        thread.start()
        self.assertTrue(done.wait(timeout=2))
        thread.join()
        self.assertEqual(snapshot, b"a" * FRAME_BYTES)

    def test_truncated_frame_is_rejected_without_emitting_it(self):
        sink = io.BytesIO()
        with self.assertRaises(ValueError):
            relay(io.BytesIO(b"12345"), sink, RecentAudio())
        self.assertEqual(sink.getvalue(), b"1234")

    def test_late_side_b_window_uses_absolute_frames_and_rejects_expired_bounds(self):
        recent = RecentAudio(seconds=1)
        # Small synthetic appends keep the same exact frame arithmetic as the
        # production ring without retaining a minute of test samples.
        recent.limit = 8 * FRAME_BYTES
        recent.append(b"A" * (6 * FRAME_BYTES))
        bounds = recent.bounds()
        requested = recent.snapshot_range(2, 6, generation=bounds["generation"])
        recent.append(b"B" * (6 * FRAME_BYTES))
        self.assertEqual(requested, b"A" * (4 * FRAME_BYTES))
        self.assertEqual(recent.bounds(), {
            "generation": 0, "oldest_frame": 4, "newest_frame": 12,
        })
        self.assertEqual(recent.snapshot_range(8, 12), b"B" * (4 * FRAME_BYTES))
        with self.assertRaisesRegex(ValueError, "unavailable"):
            recent.snapshot_range(2, 6)
        recent.clear()
        with self.assertRaisesRegex(ValueError, "generation changed"):
            recent.snapshot_range(8, 12, generation=0)

    def test_late_selection_is_exact_prefix_for_the_live_writer(self):
        from tempfile import TemporaryDirectory
        from pathlib import Path
        import wave
        from fanout import BackgroundRecording

        recent = RecentAudio(seconds=1)
        recent.limit = 10 * FRAME_BYTES
        recent.append(b"A" * (3 * FRAME_BYTES) + b"B" * (3 * FRAME_BYTES))
        selection, prefix = recent.snapshot_selection(3, generation=0)
        self.assertEqual(selection, {"generation": 0, "start_frame": 3, "end_frame": 6})
        with TemporaryDirectory() as scratch:
            destination = Path(scratch) / "side-b.wav"
            recorder = BackgroundRecording(destination, 1, prefix=prefix)
            live = b"C" * (2 * FRAME_BYTES)
            relay(io.BytesIO(live), io.BytesIO(), recent, recorder=recorder)
            self.assertEqual(recorder.stop(), 5)
            with wave.open(str(destination), "rb") as saved:
                self.assertEqual(saved.readframes(5), b"B" * (3 * FRAME_BYTES) + live)

    def test_playback_failure_is_reported_once_while_capture_continues(self):
        class BrokenPlayback:
            def write(self, data):
                raise BrokenPipeError("synthetic bridge loss")

            def flush(self):
                pass

        audio = b"abcd" * 100
        recent = RecentAudio(seconds=1)
        errors = []
        self.assertEqual(relay(io.BytesIO(audio), BrokenPlayback(), recent,
                               on_playback_error=errors.append), 100)
        self.assertEqual(recent.snapshot(), audio)
        self.assertEqual(errors[0].args, ("synthetic bridge loss",))
        self.assertEqual(len(errors), 1)

    def test_input_error_retains_truthful_partial_and_never_publishes_complete_side(self):
        from tempfile import TemporaryDirectory
        from pathlib import Path
        from fanout import BackgroundRecording

        class InterruptedInput:
            def __init__(self):
                self.calls = 0

            def read(self, size):
                self.calls += 1
                if self.calls == 1:
                    return b"abcd" * 17
                raise OSError("synthetic device loss")

        with TemporaryDirectory() as scratch:
            destination = Path(scratch) / "side-a.wav"
            recorder = BackgroundRecording(destination, 10)
            with self.assertRaisesRegex(OSError, "synthetic device loss"):
                relay(InterruptedInput(), io.BytesIO(), RecentAudio(), recorder=recorder)
            with self.assertRaisesRegex(OSError, "recoverable partial"):
                recorder.stop()
            self.assertFalse(destination.exists())
            partial = Path(scratch) / "side-a.partial.wav"
            with wave.open(str(partial), "rb") as saved:
                self.assertEqual(saved.getnframes(), 17)
                self.assertEqual(saved.readframes(17), b"abcd" * 17)
