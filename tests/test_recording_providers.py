import io
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
import wave

from listening import ListeningInstance
from providers import cover_candidates, recognize_audd, send_listen
from recording import record_side


class PilotTests(unittest.TestCase):
    def test_recording_is_opt_in_and_stops_at_frame_limit(self):
        with TemporaryDirectory() as folder:
            target = Path(folder) / "side.wav"
            audio = b"abcd" * 48001
            source = io.BytesIO(audio)
            with self.assertRaises(ValueError):
                record_side(source, target, 1)
            self.assertEqual(source.tell(), 0)
            self.assertEqual(record_side(source, target, 1, enabled=True), 48000)
            with wave.open(str(target), "rb") as recording:
                self.assertEqual(recording.readframes(48001), audio[:-4])

    def test_provider_calls_are_opt_in_and_shapes_match(self):
        requests = []
        def mock(request, timeout):
            requests.append(request)
            return io.BytesIO(json.dumps({
                "status": "ok" if "listenbrainz" in request.full_url else "success",
                "result": {"title": "Test song"},
                "images": [{"front": True, "approved": True, "image": "https://example.org/front"}],
            }).encode())

        play = ListeningInstance("test", 123, 120, seconds=60)
        self.assertFalse(send_listen(play, artist="Test", title="Tune", token="mock", transport=mock))
        self.assertIsNone(recognize_audd(b"", "mock", transport=mock))
        self.assertEqual(cover_candidates("invalid", transport=mock), [])
        self.assertFalse(requests)

        recording = io.BytesIO()
        with wave.open(recording, "wb") as writer:
            writer.setparams((2, 2, 48000, 0, "NONE", "not compressed"))
            writer.writeframes(b"abcd" * 480000)
        result = recognize_audd(recording.getvalue(), "mock", enabled=True, transport=mock)
        self.assertEqual(result["title"], "Test song")
        self.assertIn(b'filename="sample.wav"', requests[-1].data)
        self.assertTrue(send_listen(play, artist="Test", title="Tune", token="mock",
                                    enabled=True, transport=mock))
        self.assertEqual(json.loads(requests[-1].data)["payload"][0]["listened_at"], 123)
        self.assertEqual(requests[-1].get_header("Authorization"), "Token mock")
        self.assertEqual(cover_candidates("00000000-0000-0000-0000-000000000001",
                                          enabled=True, transport=mock),
                         ["https://example.org/front"])
