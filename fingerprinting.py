"""Explicit, post-split AcoustID lookup; never uploads audio or writes fingerprints."""
import json
import re
import subprocess
import wave
from io import BytesIO
from urllib.parse import urlencode
from urllib.request import Request

from providers import authenticated_open

ACOUSTID_LOOKUP = "https://api.acoustid.org/v2/lookup"
MAX_LOOKUP_SECONDS = 120


def _segment_wav(path, start_frame, end_frame, *, sample_position="beginning"):
    with wave.open(str(path), "rb") as source:
        if (source.getnchannels(), source.getsampwidth(), source.getframerate()) != (2, 2, 48000):
            raise ValueError("AcoustID requires a 48 kHz, 16-bit stereo WAV master")
        total = source.getnframes()
        if (type(start_frame) is not int or type(end_frame) is not int
                or start_frame < 0 or end_frame > total or end_frame <= start_frame):
            raise ValueError("Choose a valid reviewed track interval")
        length = end_frame - start_frame
        if length < 10 * source.getframerate():
            raise ValueError("Choose at least ten seconds of reviewed audio")
        frames = min(length, MAX_LOOKUP_SECONDS * source.getframerate())
        if sample_position not in ("beginning", "middle", "end"):
            raise ValueError("Choose the beginning, middle or end of this track")
        offset = {"beginning": 0, "middle": (length - frames) // 2,
                  "end": length - frames}[sample_position]
        source.setpos(start_frame + offset)
        pcm = source.readframes(frames)
        if len(pcm) != frames * 4:
            raise ValueError("The selected audio range is incomplete")
        result = BytesIO()
        with wave.open(result, "wb") as output:
            output.setparams((2, 2, 48000, 0, "NONE", "not compressed"))
            output.writeframes(pcm)
    return (result.getvalue(), length // source.getframerate(),
            offset // source.getframerate(), frames // source.getframerate())


def make_fingerprint(path, start_frame, end_frame, *, sample_position="beginning",
                     fpcalc="fpcalc", runner=subprocess.run):
    audio, duration, sample_start, sample_duration = _segment_wav(
        path, start_frame, end_frame, sample_position=sample_position)
    result = runner([fpcalc, "-json", "-length", str(MAX_LOOKUP_SECONDS), "-"],
                    input=audio, capture_output=True, check=True, timeout=30)
    if len(result.stdout) > 256_000:
        raise ValueError("Fingerprint tool returned too much data")
    try:
        payload = json.loads(result.stdout)
    except (TypeError, ValueError) as exc:
        raise ValueError("Fingerprint tool returned invalid JSON") from exc
    fingerprint = payload.get("fingerprint") if isinstance(payload, dict) else None
    if not isinstance(fingerprint, str) or not 10 <= len(fingerprint) <= 128_000:
        raise ValueError("Fingerprint tool returned no usable result")
    return fingerprint, duration, sample_start, sample_duration


def lookup_acoustid(fingerprint, duration, client_key, *, transport=authenticated_open):
    """Send only a fingerprint, duration, app key, and fixed metadata allowlist."""
    if not isinstance(client_key, str) or not re.fullmatch(r"[A-Za-z0-9_-]{8,128}", client_key):
        raise ValueError("Configure a valid AcoustID application key in a private file")
    if not isinstance(fingerprint, str) or not 10 <= len(fingerprint) <= 128_000:
        raise ValueError("Invalid acoustic fingerprint")
    if type(duration) is not int or not 10 <= duration <= 86_400:
        raise ValueError("Invalid reviewed track duration")
    form = urlencode({"client": client_key, "duration": duration, "fingerprint": fingerprint,
                      "meta": "recordings+recordingids", "format": "json"}).encode()
    request = Request(ACOUSTID_LOOKUP, form, {
        "Content-Type": "application/x-www-form-urlencoded",
        "User-Agent": "RecordRelay/0.2 (https://github.com/mjmcport/record-relay)",
    })
    with transport(request, timeout=10) as response:
        raw = response.read(256_001)
    if len(raw) > 256_000:
        raise ValueError("AcoustID response exceeds the supported size")
    try:
        body = json.loads(raw)
    except (ValueError, TypeError) as exc:
        raise ValueError("AcoustID returned invalid JSON") from exc
    if not isinstance(body, dict) or body.get("status") != "ok":
        raise RuntimeError("AcoustID lookup failed; retry later")
    matches = []
    for item in body.get("results", [])[:10]:
        if not isinstance(item, dict):
            continue
        result_id = item.get("id")
        score = item.get("score")
        if (not isinstance(result_id, str) or not re.fullmatch(r"[0-9a-f-]{36}", result_id, re.I)
                or type(score) not in (int, float) or not 0 <= score <= 1):
            continue
        recordings = []
        for recording in item.get("recordings", [])[:5]:
            if not isinstance(recording, dict):
                continue
            mbid = recording.get("id")
            title = recording.get("title")
            artists = recording.get("artists", [])
            artist = artists[0].get("name") if artists and isinstance(artists[0], dict) else None
            if (isinstance(mbid, str) and re.fullmatch(r"[0-9a-f-]{36}", mbid, re.I)
                    and isinstance(title, str) and len(title) <= 300
                    and (artist is None or isinstance(artist, str) and len(artist) <= 300)):
                recordings.append({"recording_mbid": mbid, "title": title, "artist": artist})
        matches.append({"acoustid_id": result_id, "score": float(score), "recordings": recordings})
    return {"duration_seconds": duration, "matches": matches}


def identify_saved_segment(path, start_frame, end_frame, client_key, *, fpcalc="fpcalc",
                           sample_position="beginning", runner=subprocess.run,
                           transport=authenticated_open):
    fingerprint, duration, sample_start, sample_duration = make_fingerprint(
        path, start_frame, end_frame, sample_position=sample_position,
        fpcalc=fpcalc, runner=runner)
    result = lookup_acoustid(fingerprint, duration, client_key, transport=transport)
    result.update({"sample_position": sample_position, "sample_start_seconds": sample_start,
                   "sample_duration_seconds": sample_duration})
    return result
