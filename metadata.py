"""Small, typed metadata helpers shared by recognition and release lookup.

This module makes no network requests. Provider identifiers remain namespaced;
in particular, a Shazam key is not an acoustic fingerprint.
"""
from dataclasses import dataclass, field
from datetime import datetime, timezone
import re


ID_PATTERNS = {
    "shazam_track": re.compile(r"^[A-Za-z0-9_-]{1,128}$"),
    "apple_track": re.compile(r"^[0-9]{1,18}$"),
    "isrc": re.compile(r"^[A-Z]{2}[A-Z0-9]{3}[0-9]{7}$", re.I),
    "musicbrainz_recording": re.compile(r"^[0-9a-f-]{36}$", re.I),
    "musicbrainz_release": re.compile(r"^[0-9a-f-]{36}$", re.I),
    "musicbrainz_release_track": re.compile(r"^[0-9a-f-]{36}$", re.I),
    "acoustid_result": re.compile(r"^[A-Za-z0-9_-]{1,128}$"),
}


def _clean(value, limit=500):
    return value.strip()[:limit] if isinstance(value, str) and value.strip() else None


@dataclass(frozen=True)
class TypedIdentifier:
    namespace: str
    value: str

    def __post_init__(self):
        pattern = ID_PATTERNS.get(self.namespace)
        if pattern is None or not pattern.fullmatch(str(self.value)):
            raise ValueError("Invalid typed metadata identifier")


@dataclass(frozen=True)
class RecognitionEvidence:
    artist: str
    title: str
    album: str | None = None
    identifiers: tuple[TypedIdentifier, ...] = ()
    source: str = "shazam"
    observed_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    frame: int | None = None
    expires_at: str | None = None
    status: str = "candidate"
    artwork_url: str | None = None


def shazam_evidence(response, *, observed_at=None, frame=None, expires_at=None):
    """Extract only explicitly supplied, bounded Shazam fields."""
    from providers import shazam_metadata

    basic = shazam_metadata(response)
    if not basic:
        return None
    track = response["track"]
    identifiers = []
    raw_key = track.get("key")
    if raw_key is not None:
        try:
            identifiers.append(TypedIdentifier("shazam_track", str(raw_key)))
        except ValueError:
            pass
    isrc = _clean(track.get("isrc"), 16)
    if isrc:
        try:
            identifiers.append(TypedIdentifier("isrc", isrc))
        except ValueError:
            pass
    apple = track.get("apple_music")
    if isinstance(apple, dict) and apple.get("id") is not None:
        try:
            identifiers.append(TypedIdentifier("apple_track", str(apple["id"])))
        except ValueError:
            pass
    art = track.get("images")
    art_url = _clean(art.get("coverart"), 2048) if isinstance(art, dict) else None
    return RecognitionEvidence(
        basic["artist"], basic["title"], basic.get("album"), tuple(identifiers),
        observed_at=observed_at or datetime.now(timezone.utc).isoformat(),
        frame=frame if type(frame) is int and frame >= 0 else None,
        expires_at=expires_at, artwork_url=art_url,
    )


def suggested_album_name(album, recorded_date, side):
    """Build the human-facing label. Date can be None when appliance time is untrusted."""
    if side not in ("A", "B"):
        raise ValueError("side must be A or B")
    date_part = recorded_date.isoformat() if recorded_date else "Date unconfirmed"
    label = _clean(album) or "Unknown album"
    return f"{label} - {date_part} - Side {side}"


def safe_filename(label, *, suffix=".wav", max_length=180):
    """Portable Unicode display-name projection for download names."""
    if not isinstance(label, str) or not isinstance(suffix, str) or not re.fullmatch(r"\.[A-Za-z0-9]{1,8}", suffix):
        raise ValueError("invalid filename")
    value = label.strip().replace("/", "-").replace("\\", "-")
    value = "".join(ch for ch in value if ch.isprintable() and ch not in '<>:"|?*')
    value = re.sub(r"\s+", " ", value).strip(" .")
    value = value[:max(1, max_length - len(suffix))].rstrip(" .") or "recording"
    if value.upper().split(".")[0] in {"CON", "PRN", "AUX", "NUL", *(f"COM{i}" for i in range(1, 10)), *(f"LPT{i}" for i in range(1, 10))}:
        value = "_" + value
    return value + suffix
