"""Read-only playback evidence from an explicitly configured Music Assistant."""
import json
from urllib.parse import urlsplit
from urllib.request import Request

from providers import request_json


def player_is_audible(player, player_id, source_uri):
    """Require one physical player with explicit volume and source evidence."""
    volume = player.get("volume_level")
    return (
        player.get("player_id") == player_id
        and player.get("type") == "player"
        and not player.get("group_childs")
        and player.get("available") is True
        and player.get("playback_state") == "playing"
        and player.get("active_source") == source_uri
        and player.get("volume_muted") is False
        and type(volume) in (int, float)
        and 0 < volume <= 100
    )


class MusicAssistantObserver:
    def __init__(self, base_url, token, player_id, source_uri, *, transport):
        address = urlsplit(base_url)
        if (not address.hostname or address.username or address.password or address.query
                or address.fragment or address.path not in ("", "/")
                or (address.scheme != "https" and not (
                    address.scheme == "http" and address.hostname in ("127.0.0.1", "::1", "localhost")
                ))):
            raise ValueError("Use HTTPS or an HTTP loopback tunnel for Music Assistant")
        if not token or any(char.isspace() for char in token):
            raise ValueError("A Music Assistant access token is required")
        if not player_id or not source_uri:
            raise ValueError("Set the exact player ID and source URI")
        self.url = base_url.rstrip("/") + "/api"
        self.token = token
        self.player_id = player_id
        self.source_uri = source_uri
        self.transport = transport

    def audible(self):
        packet = {"command": "players/all", "args": {}, "message_id": "record-relay-observer"}
        request = Request(self.url, json.dumps(packet).encode(), {
            "Content-Type": "application/json", "Authorization": f"Bearer {self.token}",
        })
        players = request_json(request, self.transport)
        if isinstance(players, dict):
            players = players.get("result")
        if not isinstance(players, list):
            return False
        matches = [item for item in players if isinstance(item, dict)
                   and item.get("player_id") == self.player_id]
        return len(matches) == 1 and player_is_audible(matches[0], self.player_id, self.source_uri)


class ScrobbleMonitor:
    """Consume a current recognition result and confirm playback before delivery."""

    def __init__(self, observer, delivery, *, wall_clock):
        self.observer = observer
        self.delivery = delivery
        self.wall_clock = wall_clock
        self.play = None
        self.track_epoch = None

    def tick(self, status, still_enabled):
        from listening import ListeningInstance

        track = status.get("track")
        if not status.get("scrobble") or not status.get("signal") or not isinstance(track, dict):
            self.delivery.timing.previous = None
            return False
        artist, title = track.get("artist"), track.get("title")
        if not all(isinstance(value, str) and value.strip() for value in (artist, title)):
            self.delivery.timing.previous = None
            return False
        identity = json.dumps([artist.casefold().strip(), title.casefold().strip()])
        epoch = status.get("track_epoch")
        if self.play is None or self.play.track_id != identity or self.track_epoch != epoch:
            self.play = ListeningInstance(identity, int(self.wall_clock()), None)
            self.track_epoch = epoch
        try:
            audible = self.observer.audible()
        except Exception:
            self.delivery.timing.previous = None
            raise
        enabled = still_enabled(track)
        return self.delivery.observe(self.play, audible=audible and enabled, enabled=enabled,
                                     artist=artist, title=title, album=track.get("album"))
