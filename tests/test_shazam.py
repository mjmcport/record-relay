import io
import sys
from types import SimpleNamespace
import unittest
from unittest.mock import patch
import wave

from providers import shazam_metadata, shazam_recognizer


class ShazamTests(unittest.TestCase):
    def test_bounded_sample_and_normalized_match(self):
        calls = []

        class Client:
            async def __aenter__(self):
                return self

            async def __aexit__(self, *_):
                pass

            async def recognize(self, sample):
                calls.append(sample)
                return {
                    "matches": [{"id": "match"}],
                    "track": {"subtitle": " Artist ", "title": " Song ",
                              "sections": [{"type": "SONG", "metadata": [
                                  {"title": "Album", "text": " Record "}
                              ]}]},
                }

        with patch.dict(sys.modules, {"shazamio": SimpleNamespace(Shazam=Client)}):
            recognize = shazam_recognizer()
        self.assertEqual(calls, [])
        buffer = io.BytesIO()
        with wave.open(buffer, "wb") as wav:
            wav.setparams((2, 2, 48000, 0, "NONE", "not compressed"))
            wav.writeframes(bytes(48000 * 4 * 10))
        sample = buffer.getvalue()
        self.assertEqual(recognize(sample), {"artist": "Artist", "title": "Song", "album": "Record"})
        self.assertEqual(calls, [sample])
        with self.assertRaises(ValueError):
            recognize(sample + bytes(48000 * 4 * 3))
        self.assertEqual(calls, [sample])

    def test_missing_match_or_track_metadata_is_ignored(self):
        self.assertIsNone(shazam_metadata({"track": {"title": "Song"}}))
        self.assertIsNone(shazam_metadata({"matches": [1], "track": {"title": "Song"}}))
