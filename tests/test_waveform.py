import io
import struct
import unittest
import wave

from waveform import waveform_peaks


class WaveformTests(unittest.TestCase):
    def test_each_frame_belongs_to_one_column_and_channels_stay_separate(self):
        recording = io.BytesIO()
        with wave.open(recording, "wb") as writer:
            writer.setparams((2, 2, 48000, 0, "NONE", "not compressed"))
            writer.writeframes(struct.pack("<10h", -32768, 12, 3, -4, 5, 6, 7, -80, 9, 10))
        recording.seek(0)
        with wave.open(recording, "rb") as reader:
            self.assertEqual(waveform_peaks(reader, 2), {
                "sample_rate": 48000, "frames": 5, "peaks": [[32768, 12], [9, 80]],
            })
