"""Opt-in network adapters. Importing this module never makes a request."""
import asyncio
import io
import json
import ssl
from functools import lru_cache
from urllib.parse import parse_qs, urlencode, urlsplit
from urllib.request import HTTPRedirectHandler, HTTPSHandler, Request, build_opener, urlopen
from uuid import UUID, uuid4
import wave


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


authenticated_open = build_opener(NoRedirect()).open


def tls_transport(authority, certificate, key):
    """Trust only the appliance CA and present the configured client certificate."""
    context = ssl.create_default_context(cafile=str(authority))
    context.minimum_version = ssl.TLSVersion.TLSv1_2
    context.load_cert_chain(certificate, key)
    return build_opener(NoRedirect(), HTTPSHandler(context=context)).open



def request_json(request, transport):
    with transport(request, timeout=10) as response:
        return json.loads(response.read(256_000))


def validate_sample(sample):
    if len(sample) > 12 * 48000 * 4 + 4096:
        raise ValueError("Recognition sample is too large")
    with wave.open(io.BytesIO(sample), "rb") as reader:
        if (reader.getnchannels(), reader.getsampwidth(), reader.getframerate()) != (2, 2, 48000):
            raise ValueError("Expected 48 kHz, 16-bit stereo")
        if not 48000 * 10 <= reader.getnframes() <= 48000 * 12:
            raise ValueError("Use a sample between 10 and 12 seconds")


def shazam_metadata(response):
    """Extract metadata for a genuine recognition match."""
    if not isinstance(response, dict):
        raise ValueError("Unexpected Shazam response")
    track = response.get("track")
    if not response.get("matches") or not isinstance(track, dict):
        return None
    artist, title = track.get("subtitle"), track.get("title")
    if not all(isinstance(value, str) and value.strip() for value in (artist, title)):
        return None
    result = {"artist": artist.strip(), "title": title.strip()}
    for section in track.get("sections") or []:
        if not isinstance(section, dict) or section.get("type") != "SONG":
            continue
        for item in section.get("metadata") or []:
            if isinstance(item, dict) and item.get("title") == "Album":
                album = item.get("text")
                if isinstance(album, str) and album.strip():
                    result["album"] = album.strip()
                    return result
    return result


def apple_track_id(track):
    """Accept only explicit Apple track links attached to the recognized track."""
    hub = track.get("hub")
    options = hub.get("options", []) if isinstance(hub, dict) else []
    ids = set()
    for option in options if isinstance(options, list) else ():
        if not isinstance(option, dict):
            continue
        for action in option.get("actions") or ():
            if not isinstance(action, dict) or not isinstance(action.get("uri"), str):
                continue
            try:
                url = urlsplit(action["uri"])
            except ValueError:
                continue
            if url.scheme not in ("https", "itms", "itmss") or url.hostname not in (
                    "music.apple.com", "itunes.apple.com"):
                continue
            candidates = parse_qs(url.query).get("i", [])
            if not candidates and "/song/" in url.path:
                candidates = [url.path.rstrip("/").rsplit("/", 1)[-1]]
            for value in candidates:
                if value.isascii() and value.isdecimal() and 1 <= len(value) <= 18:
                    ids.add(int(value))
    return next(iter(ids)) if len(ids) == 1 else None


def catalog_duration(track_id, title, artist, transport=authenticated_open):
    """Lookup one exact Apple ID. Require matching song, title and artist."""
    if type(track_id) is not int or track_id <= 0:
        return None
    url = "https://itunes.apple.com/lookup?" + urlencode({"id": track_id})
    with transport(Request(url), timeout=3) as response:
        data = json.loads(response.read(64_000))
    matches = [item for item in data.get("results", [])
               if isinstance(item, dict) and item.get("trackId") == track_id and item.get("kind") == "song"]
    if len(matches) != 1:
        return None
    item = matches[0]
    for key, expected in (("trackName", title), ("artistName", artist)):
        value = item.get(key)
        if not isinstance(value, str) or value.strip().casefold() != expected.strip().casefold():
            return None
    milliseconds = item.get("trackTimeMillis")
    return milliseconds / 1000 if type(milliseconds) is int and 0 < milliseconds <= 3_600_000 else None


def shazam_recognizer(*, use_catalog=False):
    """Construct the optional ShazamIO provider without opening a connection."""
    from shazamio import Shazam

    cached_duration = lru_cache(maxsize=128)(catalog_duration)

    async def lookup(sample):
        async with Shazam() as client:
            return await client.recognize(sample)

    def recognize(sample):
        validate_sample(sample)
        response = asyncio.run(asyncio.wait_for(lookup(sample), timeout=10))
        match = shazam_metadata(response)
        if match and use_catalog and (track_id := apple_track_id(response["track"])) is not None:
            try:
                duration = cached_duration(track_id, match["title"], match["artist"])
            except (OSError, ValueError, TypeError, AttributeError):
                duration = None
            if duration is not None:
                match["duration_seconds"] = duration
        return match

    return recognize


def recognize_audd(sample, token, *, enabled=False, transport=authenticated_open):
    """Send at most 12 seconds of native PCM to AudD when explicitly enabled."""
    if not enabled:
        return None
    if not token or not token.isalnum():
        raise ValueError("An AudD token is required")
    validate_sample(sample)
    boundary = uuid4().hex
    body = (
        f'--{boundary}\r\nContent-Disposition: form-data; name="api_token"\r\n\r\n{token}\r\n'
        f'--{boundary}\r\nContent-Disposition: form-data; name="file"; filename="sample.wav"\r\n'
        'Content-Type: audio/wav\r\n\r\n'
    ).encode() + sample + f"\r\n--{boundary}--\r\n".encode()
    response = request_json(Request("https://api.audd.io/", body, {
        "Content-Type": f"multipart/form-data; boundary={boundary}",
    }), transport)
    if response.get("status") != "success":
        raise RuntimeError("AudD did not confirm the recognition request")
    return response.get("result")


def send_listen(play, *, artist, title, album=None, token, enabled=False, transport=authenticated_open):
    """Submit only through the caller's eligibility and receipt workflow."""
    if not play.eligible(enabled=enabled):
        return False
    if not token or any(char.isspace() for char in token):
        raise ValueError("A ListenBrainz token is required")
    if not artist.strip() or not title.strip():
        raise ValueError("A track title and artist are required")
    metadata = {"artist_name": artist, "track_name": title}
    if album:
        metadata["release_name"] = album
    payload = {"listen_type": "single", "payload": [
        {"listened_at": play.started_at, "track_metadata": metadata}
    ]}
    request = Request("https://api.listenbrainz.org/1/submit-listens", json.dumps(payload).encode(),
                      {"Content-Type": "application/json", "Authorization": f"Token {token}"})
    return request_json(request, transport).get("status") == "ok"


def cover_candidates(release_id, *, enabled=False, transport=urlopen):
    """Return front-cover candidates for an operator-selected MusicBrainz release."""
    if not enabled:
        return []
    url = f"https://coverartarchive.org/release/{UUID(release_id)}"
    response = request_json(Request(url, headers={"User-Agent": "RecordRelay/0.1"}), transport)
    return [image["image"] for image in response.get("images", [])
            if image.get("front") and image.get("approved")
            and isinstance(image.get("image"), str)]
