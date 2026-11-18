import asyncio
import tempfile
from pathlib import Path
from types import SimpleNamespace as Value

from music_assistant.controllers.players.audio_sources import AudioSourceMixin
from music_assistant.providers.sendspin_source import provider
from music_assistant.providers.sendspin_source.record_relay_metadata import MetadataBridge
from music_assistant_models.streamdetails import StreamMetadata

class Players(AudioSourceMixin):
    def __init__(self):
        self._source_sessions = {"silent": Value(source_id="synthetic", provider_instance_id="owner",
                                                 playback_session_id="fake", stream_metadata=None)}
        self.updates = []
        import logging
        self.logger = logging.getLogger(__name__)
    def trigger_player_update(self, player_id):
        self.updates.append(player_id)

async def run():
    with tempfile.TemporaryDirectory() as temporary:
        import json
        from time import time
        path = Path(temporary) / "metadata.json"
        path.write_text(json.dumps({"updated_at": time(),
                                    "track": {"artist": "Synthetic", "title": "Local fixture"}}))
        players = Players()
        source = Value(instance_id="owner", mass=Value(players=players),
                       _source_name=lambda _: "Turntable")
        bridge = MetadataBridge(source, "silent", "synthetic", path, StreamMetadata)
        await bridge.tick()
        published = players.get_audio_source_session("silent").stream_metadata
        assert published.title == "Local fixture" and published.artist == "Synthetic"
        assert players.updates == ["silent"]
        path.unlink()
        await bridge.tick()
        assert players.get_audio_source_session("silent").stream_metadata.title == "Turntable"
        assert players.updates == ["silent", "silent"]
        assert hasattr(provider, "start_record_relay_metadata")
        print("Live metadata publishing and expiry fallback passed with the genuine Music Assistant controller.")

asyncio.run(run())
