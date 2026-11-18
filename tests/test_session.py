import io
from tempfile import TemporaryDirectory
from threading import Event, Thread
from pathlib import Path
import unittest
import wave

from session import Session


class SessionTests(unittest.TestCase):
    def test_controls_and_recording_during_same_playback_stream(self):
        with TemporaryDirectory() as folder:
            session = Session(folder)
            output = io.BytesIO()
            blocks = [b"abcd" * 4800] * 120

            class Input:
                def read(self, _size):
                    index = len(blocks)
                    if index == 118:
                        session.start_recording("side.wav")
                    if index == 115:
                        self.recorded = session.stop_recording()
                    return blocks.pop() if blocks else b""

            source = Input()
            audio = b"".join(blocks)
            session.run(source, output)
            self.assertEqual(output.getvalue(), audio)
            self.assertEqual(source.recorded, 14400)
            with wave.open(str(Path(folder) / "side.wav"), "rb") as recorded:
                self.assertEqual(recorded.readframes(14400), audio[:14400 * 4])
            self.assertFalse(session.lookup_if_due(self.fail))
            session.set_identify(True)

            def recognize(sample):
                with wave.open(io.BytesIO(sample), "rb") as wav:
                    self.assertEqual(wav.getnframes(), 576000)
                return {"title": "Synthetic song"}

            self.assertTrue(session.lookup_if_due(recognize))
            self.assertEqual(session.status()["track"], {"title": "Synthetic song"})
            self.assertFalse(session.lookup_if_due(self.fail))

    def test_disabling_identify_discards_an_in_flight_result(self):
        with TemporaryDirectory() as folder:
            session = Session(folder)
            session.run(io.BytesIO(b"abcd" * 576000), io.BytesIO())
            started = Event()
            finish = Event()

            def recognize(_sample):
                started.set()
                finish.wait(3)
                return {"title": "Should be discarded"}

            session.set_identify(True)
            worker = Thread(target=session.lookup_if_due, args=(recognize,))
            worker.start()
            try:
                self.assertTrue(started.wait(2))
                session.set_identify(False)
            finally:
                finish.set()
                worker.join(3)
            self.assertIsNone(session.status()["track"])

    def test_track_boundary_excludes_previous_audio_and_miss_retries(self):
        with TemporaryDirectory() as folder:
            now = [0]
            session = Session(folder, clock=lambda: now[0])
            first = b"abcd"
            second = b"efgh"
            quiet = bytes(4)

            def audio(frame, seconds):
                session.run(io.BytesIO(frame * round(48000 * seconds)), io.BytesIO())

            session.set_identify(True)
            audio(first, 12)
            self.assertTrue(session.lookup_if_due(lambda _: {"title": "First"}))
            now[0] = 59
            audio(quiet, .2)
            audio(first, 1)
            self.assertEqual(session.status()["track"], {"title": "First"})

            now[0] = 65
            audio(quiet, 1)
            self.assertIsNone(session.status()["track"])
            audio(second, 8)
            self.assertFalse(session.lookup_if_due(self.fail))
            audio(second, 2)

            def check_new_audio(sample):
                with wave.open(io.BytesIO(sample), "rb") as wav:
                    self.assertGreaterEqual(wav.getnframes(), 480000)
                    self.assertEqual(wav.readframes(wav.getnframes()), second * wav.getnframes())
                return None

            self.assertFalse(session.lookup_if_due(check_new_audio))
            self.assertIsNone(session.status()["track"])
            now[0] = 184
            audio(second, 12)
            self.assertFalse(session.lookup_if_due(self.fail))
            now[0] = 185
            audio(second, .1)
            self.assertTrue(session.lookup_if_due(lambda _: {"title": "Second"}))
            self.assertEqual(session.status()["track"], {"title": "Second"})

    def test_periodic_lookup_refreshes_gapless_track(self):
        with TemporaryDirectory() as folder:
            now = [0]
            session = Session(folder, clock=lambda: now[0])
            session.set_identify(True)
            session.run(io.BytesIO(b"abcd" * 576000), io.BytesIO())
            self.assertTrue(session.lookup_if_due(lambda _: {"title": "First"}))
            now[0] = 179
            session.run(io.BytesIO(b"efgh" * 576000), io.BytesIO())
            self.assertFalse(session.lookup_if_due(self.fail))
            now[0] = 180
            session.run(io.BytesIO(b"efgh" * 4800), io.BytesIO())
            self.assertTrue(session.lookup_if_due(lambda _: {"title": "Second"}))
            self.assertEqual(session.status()["track"], {"title": "Second"})

    def test_local_listening_is_explicit_expires_and_resets_on_opt_out(self):
        with TemporaryDirectory() as folder:
            now = [50]
            session = Session(folder, clock=lambda: now[0])
            with self.assertRaises(ValueError):
                session.set_local_listening(True)
            session.set_identify(True)
            session.set_scrobble(True)
            self.assertFalse(session.status()["local_listening"])
            session.set_local_listening(True)
            self.assertTrue(session.status()["local_listening"])
            now[0] = 1850
            self.assertFalse(session.status()["local_listening"])
            session.set_local_listening(True)
            session.set_identify(False)
            self.assertFalse(session.status()["local_listening"])
            self.assertFalse(session.status()["scrobble"])
