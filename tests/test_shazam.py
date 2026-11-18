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

    def test_explicit_apple_track_id_rejects_ambiguous_links(self):
        from providers import apple_track_id

        def response(*urls):
            return {"hub": {"options": [{"actions": [{"uri": url} for url in urls]}]}}

        self.assertEqual(apple_track_id(response("https://music.apple.com/us/song/example/1234")), 1234)
        self.assertEqual(apple_track_id(response("https://music.apple.com/us/album/example/444?i=1234")), 1234)
        self.assertIsNone(apple_track_id(response("https://music.apple.com/us/album/example/444")))
        self.assertIsNone(apple_track_id(response("https://example.org/us/song/example/1234")))
        self.assertIsNone(apple_track_id(response("https://music.apple.com/us/song/example/1234",
                                                   "https://itunes.apple.com/us/song/example/1235")))

    def test_catalog_only_returns_duration_for_exact_song_and_version_name(self):
        from providers import catalog_duration
        import json

        item = {"trackId": 42, "kind": "song", "trackName": "Song",
                "artistName": "Artist", "trackTimeMillis": 90000}

        def transport(request, timeout):
            self.assertEqual(request.full_url, "https://itunes.apple.com/lookup?id=42")
            self.assertEqual(timeout, 3)
            return io.BytesIO(json.dumps({"results": [item]}).encode())

        self.assertEqual(catalog_duration(42, "Song", "Artist", transport), 90)
        self.assertIsNone(catalog_duration(42, "Song (Live)", "Artist", transport))
        item["trackId"] = 43
        self.assertIsNone(catalog_duration(42, "Song", "Artist", transport))
