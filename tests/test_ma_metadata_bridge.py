from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace as State
import unittest

from integrations.ma_metadata_bridge import MetadataBridge
from integrations.project_metadata import project


class MetadataBridgeTests(unittest.IsolatedAsyncioTestCase):
    async def test_updates_only_exact_live_session_and_clears_when_opted_out(self):
        with TemporaryDirectory() as root:
            snapshot = Path(root) / "track.json"
            updates = []
            live = State(source_id="turntable", provider_instance_id="sendspin",
                         playback_session_id="first")
            players = State(get_audio_source_session=lambda player: live if player == "sink" else None,
                            update_source_metadata=lambda *args: updates.append(args))
            provider = State(mass=State(players=players), instance_id="sendspin",
                             _source_name=lambda _: "Turntable")
            bridge = MetadataBridge(provider, "sink", "turntable", snapshot, State)
            project(snapshot, {"identify": True, "signal": True,
                               "track": {"title": "Song", "artist": "Artist",
                                         "album": "Album", "private": "not exported"}})
            self.assertNotIn("private", snapshot.read_text())
            await bridge.tick()
            await bridge.tick()
            self.assertEqual(len(updates), 1)
            self.assertEqual(updates[0][:3], ("sink", "turntable", "sendspin"))
            self.assertEqual(updates[0][3].album, "Album")
            live.source_id = "radio"
            project(snapshot, {})
            await bridge.tick()
            self.assertEqual(len(updates), 1)
            live.source_id = "turntable"
            await bridge.tick()
            self.assertEqual(len(updates), 1)
            project(snapshot, {"identify": True, "signal": True,
                               "track": {"title": "Next", "artist": "Artist"}})
            await bridge.tick()
            project(snapshot, {"identify": False})
            await bridge.tick()
            self.assertEqual([event[3].title for event in updates], ["Song", "Next", "Turntable"])
