from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from offload import digest, offload


class OffloadTests(unittest.TestCase):
    def test_verified_copy_preserves_source_and_existing_destination(self):
        with TemporaryDirectory() as folder:
            root = Path(folder)
            source = root / "side.flac"
            source.write_bytes(b"test payload")
            destination = root / "track.flac"
            self.assertEqual(offload(source, destination), digest(source))
            self.assertEqual(destination.read_bytes(), source.read_bytes())
            with self.assertRaises(FileExistsError):
                offload(source, destination)
            self.assertEqual(sorted(root.iterdir()), [source, destination])

    def test_mismatch_leaves_no_file_or_scratch(self):
        with TemporaryDirectory() as folder:
            source = Path(folder) / "side.flac"
            source.write_bytes(b"test payload")
            destination = Path(folder) / "track.flac"
            with patch("offload.digest", side_effect=["source", "corrupt"]):
                with self.assertRaises(OSError):
                    offload(source, destination)
            self.assertEqual(list(Path(folder).iterdir()), [source])
