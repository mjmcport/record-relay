import io
import struct
import unittest
import wave

from relay import find_transitions


class TransitionTests(unittest.TestCase):
    def detect(self, blocks):
        recording = io.BytesIO()
        with wave.open(recording, "wb") as writer:
            writer.setparams((2, 2, 48000, 0, "NONE", "not compressed"))
            for amplitude, tenths in blocks:
                writer.writeframes(struct.pack("<hh", amplitude, -amplitude) * (4800 * tenths))
        recording.seek(0)
        with wave.open(recording, "rb") as reader:
            return list(find_transitions(reader))

    def test_lead_in_and_track_gap(self):
        self.assertEqual(self.detect([(0, 5), (4000, 20), (0, 9), (4000, 8)]), [0.5, 3.4])

    def test_short_pause_not_a_split_and_silence_no_track(self):
        self.assertEqual(self.detect([(4000, 2), (0, 2), (4000, 2)]), [0])
        self.assertEqual(self.detect([(0, 20)]), [])
