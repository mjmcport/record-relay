from pathlib import Path
from shutil import which
import subprocess
from tempfile import TemporaryDirectory
import unittest
import wave

from export_track import export_track


@unittest.skipUnless(which("ffmpeg"), "requires FFmpeg")
class ExportTests(unittest.TestCase):
    def test_export_preserves_exact_samples_and_does_not_replace_output(self):
        with TemporaryDirectory() as folder:
            source = Path(folder) / "side.wav"
            output = Path(folder) / "track.flac"
            frames = bytes(range(256)) * 10
            with wave.open(str(source), "wb") as writer:
                writer.setparams((2, 2, 48000, 0, "NONE", "not compressed"))
                writer.writeframes(frames)
            original = source.read_bytes()
            export_track(source, output, 63, 399, {"title": "Test"})
            decoded = subprocess.check_output(
                ["ffmpeg", "-v", "error", "-i", str(output), "-f", "s16le", "-"])
            self.assertEqual(decoded, frames[63 * 4 : 399 * 4])
            first = output.read_bytes()
            with self.assertRaises(FileExistsError):
                export_track(source, output, 0, 1, {})
            self.assertEqual(output.read_bytes(), first)
            self.assertEqual(source.read_bytes(), original)
