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

    @unittest.skipUnless(which("ffprobe"), "requires FFprobe")
    def test_embeds_a_reviewed_local_cover(self):
        import base64
        import json

        with TemporaryDirectory() as folder:
            source = Path(folder) / "side.wav"
            output = Path(folder) / "track.flac"
            cover = Path(folder) / "cover.png"
            cover.write_bytes(base64.b64decode(
                "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+jB1sAAAAASUVORK5CYII="
            ))
            with wave.open(str(source), "wb") as writer:
                writer.setparams((2, 2, 48000, 0, "NONE", "not compressed"))
                writer.writeframes(b"abcd" * 480)
            export_track(source, output, 0, 480, {}, cover=cover)
            probe = json.loads(subprocess.check_output(
                ["ffprobe", "-v", "error", "-show_streams", "-of", "json", str(output)]
            ))
            self.assertEqual(probe["streams"][1]["disposition"]["attached_pic"], 1)
