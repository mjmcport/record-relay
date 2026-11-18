import io
import json
from datetime import date
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from metadata import (RecognitionEvidence, TypedIdentifier, safe_filename,
                      shazam_evidence, suggested_album_name)
from release_catalog import ReleaseCatalog, RevisionConflict, ServiceBusy


RELEASE_ID = "00000000-0000-0000-0000-000000000001"
RECORDING_ID = "00000000-0000-0000-0000-000000000002"


class MetadataTests(unittest.TestCase):
    def test_shazam_observation_retains_only_typed_explicit_fields(self):
        response = {"matches": [{"id": "m"}], "track": {
            "key": "123456", "subtitle": " Artist ", "title": "Song", "isrc": "USRC17607839",
            "images": {"coverart": "https://example.org/art"}, "sections": [{"type": "SONG",
              "metadata": [{"title": "Album", "text": "Record"}]}]}}
        evidence = shazam_evidence(response, observed_at="2026-01-01T00:00:00Z", frame=42)
        self.assertIsInstance(evidence, RecognitionEvidence)
        self.assertEqual(evidence.album, "Record")
        self.assertEqual({i.namespace for i in evidence.identifiers}, {"shazam_track", "isrc"})
        self.assertEqual(evidence.frame, 42)
        self.assertIsNone(shazam_evidence({"matches": [], "track": {}}))
        with self.assertRaises(ValueError):
            TypedIdentifier("shazam_track", "not a fingerprint id?" )

    def test_name_helpers_keep_unicode_and_escape_path_parts(self):
        label = suggested_album_name("Björk / Debut", date(2026, 11, 18), "B")
        self.assertEqual(label, "Björk / Debut - 2026-11-18 - Side B")
        self.assertEqual(safe_filename(label), "Björk - Debut - 2026-11-18 - Side B.wav")
        self.assertEqual(safe_filename("CON"), "_CON.wav")
        self.assertIn("Date unconfirmed", suggested_album_name(None, None, "A"))
        with self.assertRaises(ValueError):
            suggested_album_name("Record", None, "C")


class ReleaseCatalogTests(unittest.TestCase):
    def setUp(self):
        self.temp = TemporaryDirectory()
        self.db = Path(self.temp.name) / "catalog.sqlite"
        self.calls = []

    def tearDown(self):
        self.temp.cleanup()

    def transport(self, request, timeout):
        self.calls.append((request, timeout))
        if "/release/?" in request.full_url:
            body = {"releases": [{"id": RELEASE_ID, "title": "Album", "artist-credit-phrase": "Artist",
                                  "date": "1980", "country": "GB", "media": [{"format": "Vinyl", "track-count": 2}],
                                  "label-info": [{"label": {"name": "Label"}, "catalog-number": "ABC 1"}]}]}
        else:
            body = {"id": RELEASE_ID, "title": "Album", "artist-credit-phrase": "Artist", "date": "1980",
                    "country": "GB", "label-info": [{"label": {"name": "Label"}, "catalog-number": "ABC 1"}],
                    "media": [{"position": 1, "format": "Vinyl", "tracks": [
                        {"id": RECORDING_ID, "position": 1, "number": "A1", "title": "Song",
                         "recording": {"id": RECORDING_ID, "title": "Song", "artist-credit": [{"name": "Singer"}]}}]}]}
        return io.BytesIO(json.dumps(body).encode())

    def test_explicit_search_candidates_and_query_are_bounded(self):
        catalog = ReleaseCatalog(self.db, transport=self.transport, clock=lambda _: None)
        candidates = catalog.search(album="Album", artist="Artist", barcode="123", limit=99)
        self.assertEqual(candidates[0]["release_mbid"], RELEASE_ID)
        self.assertEqual(candidates[0]["formats"], ["Vinyl"])
        self.assertIn("limit=25", self.calls[0][0].full_url)
        self.assertIn("user-agent", [name.lower() for name, _ in self.calls[0][0].header_items()])
        catalog.search(album="Album", artist="Artist", barcode="123", limit=99)
        self.assertEqual(len(self.calls), 1)
        with self.assertRaises(ValueError):
            catalog.search()

    def test_release_snapshot_pin_cas_and_offline_reads(self):
        online = ReleaseCatalog(self.db, transport=self.transport, clock=lambda _: None)
        result = online.pin("project-1", RELEASE_ID, expected_revision=0)
        self.assertEqual(result["revision"], 1)
        track = result["release"]["media"][0]["tracks"][0]
        self.assertEqual(track["number"], "A1")
        offline = ReleaseCatalog(self.db, online=False)
        self.assertEqual(offline.fetch_release(RELEASE_ID), result["release"])
        self.assertEqual(offline.get_pin("project-1")["release_mbid"], RELEASE_ID)
        with self.assertRaises(RevisionConflict) as error:
            online.clear_pin("project-1", expected_revision=0)
        self.assertEqual(error.exception.current_revision, 1)
        self.assertEqual(offline.clear_pin("project-1", expected_revision=1)["revision"], 2)

    def test_no_network_offline_and_service_busy_errors_are_explicit(self):
        offline = ReleaseCatalog(self.db, online=False)
        with self.assertRaisesRegex(ConnectionError, "Offline"):
            offline.search(album="Not cached")

        def busy(request, timeout):
            from urllib.error import HTTPError
            raise HTTPError(request.full_url, 503, "busy", {"Retry-After": "9"}, None)
        catalog = ReleaseCatalog(self.db, transport=busy, clock=lambda _: None)
        with self.assertRaises(ServiceBusy) as error:
            catalog.search(album="Album")
        self.assertEqual(error.exception.retry_after, 9)


if __name__ == "__main__":
    unittest.main()
