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
