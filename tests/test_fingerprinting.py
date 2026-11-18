import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
import wave
from urllib.parse import parse_qs, urlsplit

from fingerprinting import _segment_wav, identify_saved_segment, lookup_acoustid


class FingerprintingTests(unittest.TestCase):
    def test_fpcalc_reads_only_reviewed_segment_and_returns_allowlisted_matches(self):
        with TemporaryDirectory() as folder:
            path = Path(folder) / "master.wav"
            with wave.open(str(path), "wb") as writer:
                writer.setparams((2, 2, 48000, 0, "NONE", "not compressed"))
                writer.writeframes(b"\x11\x00\x11\x00" * (12 * 48000))
            calls = []

            def runner(command, **kwargs):
                calls.append((command, kwargs))
                with wave.open(__import__("io").BytesIO(kwargs["input"]), "rb") as clip:
                    self.assertEqual(clip.getnframes(), 12 * 48000)
                class Result:
                    stdout = json.dumps({"fingerprint": "A" * 100}).encode()
                return Result()

            def transport(request, timeout):
                query = parse_qs(request.data.decode())
                self.assertEqual(set(query), {"client", "duration", "fingerprint", "meta", "format"})
                self.assertEqual(query["meta"], ["recordings+recordingids"])
                self.assertEqual(query["duration"], ["12"])
                self.assertEqual(urlsplit(request.full_url).netloc, "api.acoustid.org")
                self.assertNotIn("audio", query)
                class Response:
                    def __enter__(self): return self
                    def __exit__(self, *args): return False
                    def read(self, limit):
                        return json.dumps({"status": "ok", "results": [{
                            "id": "12345678-1234-1234-1234-123456789012", "score": 0.92,
                            "recordings": [{"id": "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa",
                                            "title": "Fixture song", "artists": [{"name": "Fixture artist"}],
                                            "private": "discard"}],
                            "private": "discard"}]}).encode()
                return Response()

            result = identify_saved_segment(path, 0, 12 * 48000, "abcdefgh12345678",
                                           runner=runner, transport=transport)
            self.assertEqual(calls[0][0][-1], "-")
            self.assertEqual(result["matches"][0]["recordings"], [{
                "recording_mbid": "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa",
                "title": "Fixture song", "artist": "Fixture artist"}])
            self.assertNotIn("private", json.dumps(result))

    def test_short_or_invalid_segment_is_rejected_before_fingerprint_tool(self):
        with TemporaryDirectory() as folder:
            path = Path(folder) / "master.wav"
            with wave.open(str(path), "wb") as writer:
                writer.setparams((2, 2, 48000, 0, "NONE", "not compressed"))
                writer.writeframes(b"\x00" * (20 * 48000 * 4))
            with self.assertRaisesRegex(ValueError, "at least ten seconds"):
                identify_saved_segment(path, 0, 9 * 48000, "abcdefgh12345678",
                                       runner=lambda *a, **k: self.fail("must not run"), transport=lambda *a, **k: None)

    def test_long_segment_samples_beginning_middle_or_end_but_sends_full_duration(self):
        with TemporaryDirectory() as folder:
            path = Path(folder) / "long-side.wav"
            rate = 48_000
            seconds = 130
            with wave.open(str(path), "wb") as writer:
                writer.setparams((2, 2, rate, 0, "NONE", "not compressed"))
                # Stamp each second with its second index so the excerpt offset is observable.
                for second in range(seconds):
                    writer.writeframes((second.to_bytes(2, "little", signed=True) * 2) * rate)

            expected = {"beginning": (0, 0), "middle": (5, 5), "end": (10, 10)}
            for position, (offset, first_sample) in expected.items():
                audio, full_duration, sample_start, sample_duration = _segment_wav(
                    path, 0, seconds * rate, sample_position=position)
                self.assertEqual((full_duration, sample_start, sample_duration), (130, offset, 120))
                with wave.open(__import__("io").BytesIO(audio), "rb") as sample:
                    first_frame = sample.readframes(1)
                self.assertEqual(int.from_bytes(first_frame[:2], "little", signed=True), first_sample)

            with self.assertRaisesRegex(ValueError, "beginning, middle or end"):
                _segment_wav(path, 0, seconds * rate, sample_position="random")


if __name__ == "__main__":
    unittest.main()
