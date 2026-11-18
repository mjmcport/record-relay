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
