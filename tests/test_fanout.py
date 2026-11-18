import io
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Event
import unittest
from unittest.mock import patch
import wave

from capture import BLOCK_BYTES, RecentAudio, relay
from fanout import BackgroundRecording
from recording import record_side


class FanoutTests(unittest.TestCase):
    def test_recording_gets_exact_playback_samples(self):
        with TemporaryDirectory() as scratch:
            path = Path(scratch) / "side.wav"
            audio = b"abcd" * 8000
            target = io.BytesIO()
            recorder = BackgroundRecording(path, 10)
            relay(io.BytesIO(audio), target, RecentAudio(), recorder=recorder)
            self.assertEqual(recorder.stop(), 8000)
            with wave.open(str(path), "rb") as result:
                self.assertEqual(result.readframes(8000), target.getvalue())

    def test_stalled_recorder_does_not_stall_relay_or_publish_partial_side(self):
        with TemporaryDirectory() as scratch:
            path = Path(scratch) / "side.wav"
            proceed = Event()

            def delayed(*args, **kwargs):
                if not proceed.wait(3):
                    raise TimeoutError("test recorder was never released")
                return record_side(*args, **kwargs)

            with patch("fanout.record_side", side_effect=delayed):
                recorder = BackgroundRecording(path, 10, blocks=1)
                audio = b"abcd" * BLOCK_BYTES
                target = io.BytesIO()
                try:
                    relay(io.BytesIO(audio), target, RecentAudio(), recorder=recorder)
                    self.assertEqual(target.getvalue(), audio)
                    self.assertTrue(recorder.overrun.is_set())
                finally:
                    proceed.set()
                with self.assertRaisesRegex(OSError, "fell behind"):
                    recorder.stop()
            self.assertFalse(path.exists())
