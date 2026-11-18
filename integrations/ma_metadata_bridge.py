"""Optional adapter for the Music Assistant 2.10.12 Sendspin source provider."""
import asyncio
import json
import os
from pathlib import Path
from time import time


def read_track(path, now):
    """Read a short-lived snapshot from an atomic, locally projected file."""
    with path.open("rb") as source:
        payload = source.read(4097)
    if len(payload) > 4096:
        raise ValueError("Oversized metadata file")
    record = json.loads(payload)
    timestamp = record["updated_at"]
    if type(timestamp) not in (float, int) or not -5 <= now - timestamp <= 15:
        raise ValueError("Stale metadata file")
    track = record["track"]
    if track is None:
        return None
    fields = tuple(track.get(key) for key in ("title", "artist", "album"))
    if not all(value is None or isinstance(value, str) and len(value) <= 300 for value in fields):
        raise ValueError("Invalid track fields")
    if not fields[0] or not fields[1]:
        raise ValueError("Missing track identity")
    return fields


class MetadataBridge:
    def __init__(self, provider, player_id, source_id, snapshot, stream_metadata):
        self.provider = provider
        self.player_id = player_id
        self.source_id = source_id
        self.snapshot = Path(snapshot)
        self.stream_metadata = stream_metadata
        self.last = None

    async def tick(self):
        players = self.provider.mass.players
        session = players.get_audio_source_session(self.player_id)
        if (session is None or session.source_id != self.source_id
                or session.provider_instance_id != self.provider.instance_id):
            self.last = None
            return
        key = session.playback_session_id
        try:
            track = await asyncio.to_thread(read_track, self.snapshot, time())
        except (OSError, ValueError, KeyError, AttributeError, TypeError):
            track = None
        if self.last == (key, track) or (track is None and
                                        (self.last is None or self.last[0] != key)):
            return
        current = players.get_audio_source_session(self.player_id)
        if current is not session or current.playback_session_id != key:
            return
        title, artist, album = track or (self.provider._source_name(self.source_id), None, None)
        players.update_source_metadata(
            self.player_id, self.source_id, self.provider.instance_id,
            self.stream_metadata(title=title, artist=artist, album=album),
        )
        self.last = key, track

    async def run(self):
        while True:
            await self.tick()
            await asyncio.sleep(2)


def start(provider, stream_metadata):
    values = tuple(os.environ.get(key) for key in (
        "RECORD_RELAY_PLAYER_ID", "RECORD_RELAY_SOURCE_ID", "RECORD_RELAY_METADATA_FILE"
    ))
    if not all(values):
        return None
    return asyncio.create_task(MetadataBridge(provider, *values, stream_metadata).run())
