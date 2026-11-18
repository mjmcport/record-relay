"""Explicit MusicBrainz search and locally pinned release snapshots.

The caller supplies one persistent SQLite file shared by panel processes.
Requests are bounded, cached, and serialized through a database rate gate.
"""
from datetime import datetime, timezone
import json
import os
import sqlite3
import time
from contextlib import contextmanager
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen
from uuid import UUID

from providers import NoRedirect

API = "https://musicbrainz.org/ws/2"
USER_AGENT = "RecordRelay/1.0 (https://github.com/mjmcport/record-relay)"


def _connect(path):
    db = sqlite3.connect(path, timeout=5)
    db.execute("PRAGMA busy_timeout=5000")
    db.execute("""CREATE TABLE IF NOT EXISTS metadata_cache
        (cache_key TEXT PRIMARY KEY, body TEXT NOT NULL, saved REAL NOT NULL)""")
    db.execute("""CREATE TABLE IF NOT EXISTS metadata_clock
        (name TEXT PRIMARY KEY, last_request REAL NOT NULL)""")
    db.execute("""CREATE TABLE IF NOT EXISTS release_pins
        (project_id TEXT PRIMARY KEY, revision INTEGER NOT NULL, release_mbid TEXT,
         snapshot TEXT, updated TEXT NOT NULL)""")
    try:
        os.chmod(path, 0o600)
    except OSError:
        db.close()
        raise
    return db


@contextmanager
def _db(path):
    db = _connect(path)
    try:
        with db:
            yield db
    finally:
        db.close()


def _request_json(url, transport):
    req = Request(url, headers={"User-Agent": USER_AGENT, "Accept": "application/json"})
    with transport(req, timeout=8) as response:
        raw = response.read(1_000_001)
    if len(raw) > 1_000_000:
        raise ValueError("MusicBrainz response too large")
    data = json.loads(raw)
    if not isinstance(data, dict):
        raise ValueError("Unexpected MusicBrainz response")
    return data


def _cached_get(path, key, url, transport, clock, *, now=None):
    now = time.time() if now is None else now
    with _db(path) as db:
        db.execute("BEGIN IMMEDIATE")
        cached = db.execute("SELECT body FROM metadata_cache WHERE cache_key=?", (key,)).fetchone()
        if cached:
            return json.loads(cached[0])
        row = db.execute("SELECT last_request FROM metadata_clock WHERE name='musicbrainz'").fetchone()
        previous = row[0] if row else 0
        wait = max(0, 1.0 - (now - previous))
        if wait:
            clock(wait)
        stamp = max(time.time(), previous + 1.0)
        db.execute("INSERT INTO metadata_clock VALUES('musicbrainz', ?) ON CONFLICT(name) DO UPDATE SET last_request=excluded.last_request", (stamp,))
        db.commit()
    try:
        data = _request_json(url, transport)
    except HTTPError as exc:
        if exc.code in (429, 503):
            delay = exc.headers.get("Retry-After", "")
            try:
                penalty = int(delay) if delay.isdecimal() else 30
            except ValueError:
                penalty = 30
            with _db(path) as db:
                db.execute("UPDATE metadata_clock SET last_request=? WHERE name='musicbrainz'",
                           (time.time() + min(penalty, 3600),))
            raise ServiceBusy(delay if delay.isdecimal() else None) from exc
        raise
    with _db(path) as db:
        db.execute("INSERT OR REPLACE INTO metadata_cache VALUES(?,?,?)", (key, json.dumps(data, ensure_ascii=False), time.time()))
    return data


class ServiceBusy(RuntimeError):
    def __init__(self, retry_after=None):
        super().__init__("Service busy — retry later")
        self.retry_after = int(retry_after) if retry_after else None


def _text(value, limit=300):
    return value.strip()[:limit] if isinstance(value, str) and value.strip() else None


def parse_release(raw):
    """Return a bounded edition snapshot with typed MusicBrainz IDs."""
    if not isinstance(raw, dict):
        raise ValueError("Invalid MusicBrainz release")
    try:
        mbid = str(UUID(raw.get("id", "")))
    except (ValueError, TypeError, AttributeError):
        raise ValueError("Invalid MusicBrainz release ID")
    media = []
    for medium in raw.get("media", [])[:20] if isinstance(raw.get("media", []), list) else []:
        if not isinstance(medium, dict):
            continue
        tracks = []
        for track in medium.get("tracks", [])[:200] if isinstance(medium.get("tracks", []), list) else []:
            if not isinstance(track, dict):
                continue
            recording = track.get("recording") if isinstance(track.get("recording"), dict) else {}
            tracks.append({
                "release_track_mbid": track.get("id"),
                "position": track.get("position"), "number": _text(track.get("number"), 20),
                "title": _text(track.get("title")),
                "recording_mbid": recording.get("id"),
                "recording_title": _text(recording.get("title")),
                "artists": [_text(a.get("name")) for a in recording.get("artist-credit", [])[:8]
                            if isinstance(a, dict) and isinstance(a.get("name"), str)],
            })
        media.append({"position": medium.get("position"), "format": _text(medium.get("format"), 100), "tracks": tracks})
    label_info = raw.get("label-info") if isinstance(raw.get("label-info"), list) else []
    return {
        "release_mbid": mbid, "title": _text(raw.get("title")),
        "artist": _text(raw.get("artist-credit-phrase")),
        "date": _text(raw.get("date"), 20), "country": _text(raw.get("country"), 10),
        "status": _text(raw.get("status"), 30),
        "labels": [{"name": _text(i.get("label", {}).get("name")),
                    "catalog_number": _text(i.get("catalog-number"), 100)}
                   for i in label_info[:20] if isinstance(i, dict) and isinstance(i.get("label"), dict)],
        "media": media,
        "source": "musicbrainz", "fetched_at": datetime.now(timezone.utc).isoformat(),
    }


class ReleaseCatalog:
    def __init__(self, database, *, transport=urlopen, clock=time.sleep, online=True):
        self.database = str(database)
        self.transport = transport
        self.clock = clock
        self.online = bool(online)
        with _db(self.database):
            pass

    def _get(self, key, url):
        if not self.online:
            with _db(self.database) as db:
                row = db.execute("SELECT body FROM metadata_cache WHERE cache_key=?", (key,)).fetchone()
            if not row:
                raise ConnectionError("Offline")
            return json.loads(row[0])
        return _cached_get(self.database, key, url, self.transport, self.clock)

    def search(self, *, album=None, artist=None, catalog_number=None, barcode=None,
               recording_isrc=None, title=None, limit=10):
        """Search only on explicit call; return candidates, never select the first."""
        terms = []
        for field, value in (("release", album), ("artist", artist),
                             ("catno", catalog_number), ("barcode", barcode),
                             ("recording", title), ("recording", title if recording_isrc else None),
                             ("isrc", recording_isrc)):
            if value and isinstance(value, str) and value.strip():
                terms.append(f'{field}:"{value.strip()[:120]}"')
        if not terms:
            raise ValueError("Provide album, artist, catalog number, barcode, title or ISRC")
        limit = max(1, min(int(limit), 25))
        query = " AND ".join(terms)
        key = "search:" + query.casefold() + f":{limit}"
        url = API + "/release/?" + urlencode({"query": query, "fmt": "json", "limit": limit})
        try:
            data = self._get(key, url)
        except (OSError, URLError, TimeoutError, ConnectionError) as exc:
            raise ConnectionError("Offline") from exc
        return [self._candidate(row) for row in data.get("releases", [])[:limit]
                if isinstance(row, dict) and row.get("id")]

    @staticmethod
    def _candidate(row):
        try:
            mbid = str(UUID(row.get("id", "")))
        except (ValueError, TypeError, AttributeError):
            return None
        labels = row.get("label-info", [])
        return {"release_mbid": mbid, "title": _text(row.get("title")),
                "artist": _text(row.get("artist-credit-phrase")), "date": _text(row.get("date"), 20),
                "country": _text(row.get("country"), 10),
                "formats": list(dict.fromkeys(_text(m.get("format"), 100) for m in row.get("media", [])
                                               if isinstance(m, dict) and _text(m.get("format")))),
                "labels": [{"name": _text(x.get("label", {}).get("name")),
                            "catalog_number": _text(x.get("catalog-number"), 100)}
                           for x in labels[:10] if isinstance(x, dict) and isinstance(x.get("label"), dict)],
                "track_count": sum(m.get("track-count", 0) for m in row.get("media", [])
                                   if isinstance(m, dict) and type(m.get("track-count")) is int)}

    def fetch_release(self, release_mbid):
        release_mbid = str(UUID(release_mbid))
        key = "release:" + release_mbid
        snapshot_key = "snapshot:" + release_mbid
        with _db(self.database) as db:
            saved = db.execute("SELECT body FROM metadata_cache WHERE cache_key=?", (snapshot_key,)).fetchone()
        if saved:
            return json.loads(saved[0])
        url = f"{API}/release/{release_mbid}?" + urlencode({"fmt": "json", "inc": "artists+labels+recordings"})
        try:
            snapshot = parse_release(self._get(key, url))
            with _db(self.database) as db:
                db.execute("INSERT OR REPLACE INTO metadata_cache VALUES(?,?,?)",
                           (snapshot_key, json.dumps(snapshot, ensure_ascii=False), time.time()))
            return snapshot
        except (OSError, URLError, TimeoutError, ConnectionError) as exc:
            raise ConnectionError("Offline") from exc

    def pin(self, project_id, release_mbid, *, expected_revision):
        """Fetch and materialize before CAS pin; a later online result cannot replace it."""
        snapshot = self.fetch_release(release_mbid)
        with _db(self.database) as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT revision FROM release_pins WHERE project_id=?", (project_id,)).fetchone()
            revision = row[0] if row else 0
            if revision != expected_revision:
                raise RevisionConflict(revision)
            db.execute("INSERT INTO release_pins VALUES(?,?,?,?,?) ON CONFLICT(project_id) DO UPDATE SET revision=excluded.revision, release_mbid=excluded.release_mbid, snapshot=excluded.snapshot, updated=excluded.updated",
                       (project_id, revision + 1, snapshot["release_mbid"],
                        json.dumps(snapshot, ensure_ascii=False), datetime.now(timezone.utc).isoformat()))
        return {"revision": revision + 1, "release": snapshot}

    def get_pin(self, project_id):
        with _db(self.database) as db:
            row = db.execute("SELECT revision,release_mbid,snapshot FROM release_pins WHERE project_id=?", (project_id,)).fetchone()
        return None if not row else {"revision": row[0], "release_mbid": row[1], "release": json.loads(row[2])}

    def clear_pin(self, project_id, *, expected_revision):
        with _db(self.database) as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT revision FROM release_pins WHERE project_id=?", (project_id,)).fetchone()
            revision = row[0] if row else 0
            if revision != expected_revision:
                raise RevisionConflict(revision)
            db.execute("INSERT INTO release_pins VALUES(?,?,NULL,NULL,?) ON CONFLICT(project_id) DO UPDATE SET revision=excluded.revision,release_mbid=NULL,snapshot=NULL,updated=excluded.updated",
                       (project_id, revision + 1, datetime.now(timezone.utc).isoformat()))
        return {"revision": revision + 1, "release_mbid": None, "release": None}


class RevisionConflict(RuntimeError):
    def __init__(self, current_revision):
        super().__init__("Release pin changed; reload before applying")
        self.current_revision = current_revision
