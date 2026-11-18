"""Explicit MusicBrainz search and locally pinned release snapshots.

The caller supplies one persistent SQLite file shared by panel processes.
Requests are bounded, cached, and serialized through a database rate gate.
"""
from datetime import datetime, timezone
import base64
import json
import os
import re
import sqlite3
import time
from contextlib import contextmanager
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import HTTPRedirectHandler, Request, build_opener, HTTPSHandler, urlopen
from urllib.parse import urlsplit, urlunsplit
from uuid import UUID

from providers import NoRedirect

API = "https://musicbrainz.org/ws/2"
RECORDING_API = API + "/recording"
USER_AGENT = "RecordRelay/1.0 (https://github.com/mjmcport/record-relay)"
CAA_HOSTS = {"coverartarchive.org", "archive.org", "s3.us.archive.org"}


class _CAAHTTPSRedirect(HTTPRedirectHandler):
    """Follow only the Cover Art Archive and its documented archive.org image hosts."""
    def redirect_request(self, request, fp, code, msg, headers, newurl):
        old, new = urlsplit(request.full_url), urlsplit(newurl)
        if (old.scheme != "https" or old.hostname not in CAA_HOSTS or old.port not in (None, 443)
                or new.hostname not in CAA_HOSTS or new.port not in (None, 443) or new.username or new.password
                or new.query or new.fragment):
            return None
        target = urlunsplit(("https", new.hostname, new.path, "", ""))
        return Request(target, headers={"User-Agent": USER_AGENT, "Accept": "image/jpeg, application/json"})


def _cover_transport(request, timeout=8):
    parsed = urlsplit(request.full_url)
    if (parsed.scheme != "https" or parsed.hostname != "coverartarchive.org"
            or parsed.username or parsed.password or parsed.query or parsed.fragment):
        raise ValueError("Cover art request must use the Cover Art Archive")
    opener = build_opener(_CAAHTTPSRedirect(), HTTPSHandler())
    return opener.open(request, timeout=timeout)


def _connect(path):
    path = os.fspath(path)
    if os.path.islink(path) or os.path.islink(path + "-wal") or os.path.islink(path + "-shm"):
        raise ValueError("Metadata database and SQLite sidecars may not be symbolic links")
    db = sqlite3.connect(path, timeout=5)
    db.execute("PRAGMA busy_timeout=5000")
    db.execute("""CREATE TABLE IF NOT EXISTS metadata_cache
        (cache_key TEXT PRIMARY KEY, body TEXT NOT NULL, saved REAL NOT NULL)""")
    db.execute("""CREATE TABLE IF NOT EXISTS metadata_clock
        (name TEXT PRIMARY KEY, last_request REAL NOT NULL)""")
    db.execute("""CREATE TABLE IF NOT EXISTS release_pins
        (project_id TEXT PRIMARY KEY, revision INTEGER NOT NULL, release_mbid TEXT,
         snapshot TEXT, updated TEXT NOT NULL)""")
    db.execute("""CREATE TABLE IF NOT EXISTS project_covers
        (project_id TEXT PRIMARY KEY, release_mbid TEXT NOT NULL, image_id TEXT NOT NULL)""")
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


def _phrase(value):
    escaped = re.sub(r'([+\-!(){}\[\]^"~*?:\\/])', r'\\\1', value)
    return f'"{escaped}"'


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
    def __init__(self, database, *, transport=urlopen, cover_transport=None, clock=time.sleep, online=True):
        self.database = str(database)
        self.transport = transport
        self.cover_transport = cover_transport or _cover_transport
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

    def _get_caa(self, key, url):
        if not self.online:
            with _db(self.database) as db:
                row = db.execute("SELECT body FROM metadata_cache WHERE cache_key=?", (key,)).fetchone()
            if not row:
                raise ConnectionError("Offline")
            return json.loads(row[0])
        return _cached_get(self.database, key, url, self.cover_transport, self.clock)

    def search(self, *, album=None, artist=None, catalog_number=None, barcode=None,
               recording_isrc=None, title=None, limit=10):
        """Search only on explicit call; return candidates, never select the first."""
        if not album and not catalog_number and not barcode and (title or recording_isrc):
            return self.search_recording_releases(title=title, artist=artist,
                                                  recording_isrc=recording_isrc, limit=limit)
        terms = []
        for field, value in (("release", album), ("artist", artist),
                             ("catno", catalog_number), ("barcode", barcode)):
            if value and isinstance(value, str) and value.strip():
                terms.append(f'{field}:{_phrase(value.strip()[:120])}')
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
        candidates = (self._candidate(row) for row in data.get("releases", [])[:limit]
                      if isinstance(row, dict) and row.get("id"))
        return [candidate for candidate in candidates if candidate is not None]

    def search_recording_releases(self, *, title=None, artist=None, recording_isrc=None, limit=10):
        """Find possible editions through recordings linked to an identified title/artist."""
        title = _text(title, 120); artist = _text(artist, 120)
        if recording_isrc:
            if not isinstance(recording_isrc, str) or not re.fullmatch(r"[A-Z]{2}[A-Z0-9]{3}[0-9]{7}", recording_isrc.upper()):
                raise ValueError("ISRC is invalid")
            query = f'isrc:"{recording_isrc.upper()}"'
        elif title and artist:
            query = f'recording:{_phrase(title)} AND artist:{_phrase(artist)}'
        else:
            raise ValueError("A recognized title and artist are required")
        key = "recordings:" + query.casefold()
        url = RECORDING_API + "?" + urlencode({"query": query, "fmt": "json", "limit": "3"})
        try:
            data = self._get(key, url)
            recordings = data.get("recordings", [])
            matched = []
            for recording in recordings[:3] if isinstance(recordings, list) else []:
                if not isinstance(recording, dict):
                    continue
                try:
                    recording_id = str(UUID(recording.get("id", "")))
                except (ValueError, TypeError, AttributeError):
                    continue
                releases = recording.get("releases")
                if not isinstance(releases, list) or not releases:
                    detail_key = "recording-releases:" + recording_id
                    detail_url = f"{RECORDING_API}/{recording_id}?" + urlencode({"inc": "releases", "fmt": "json"})
                    releases = self._get(detail_key, detail_url).get("releases", [])
                artists = recording.get("artist-credit", [])
                credited = " ".join(_text(item.get("name") or item.get("artist", {}).get("name"), 100) or ""
                                     for item in artists if isinstance(item, dict)) if isinstance(artists, list) else ""
                match = {"title": _text(recording.get("title"), 120) or title or "Recognized recording",
                         "artist": _text(recording.get("artist-credit-phrase"), 120) or credited,
                         "score": recording.get("score") if type(recording.get("score")) is int else None}
                for release in releases[:25] if isinstance(releases, list) else []:
                    if isinstance(release, dict) and release.get("id"):
                        matched.append((release, match))
        except (OSError, URLError, TimeoutError, ConnectionError) as exc:
            raise ConnectionError("Offline") from exc
        candidates = {}
        for release, match in matched:
            candidate = self._candidate(release)
            if candidate is None:
                continue
            item = candidates.setdefault(candidate["release_mbid"], candidate)
            if "matched_tracks" not in item:
                item["matched_tracks"] = []
            if match not in item["matched_tracks"] and len(item["matched_tracks"]) < 3:
                item["matched_tracks"].append(match)
        return list(candidates.values())[:max(1, min(int(limit), 10))]

    @staticmethod
    def _candidate(row):
        try:
            mbid = str(UUID(row.get("id", "")))
        except (ValueError, TypeError, AttributeError):
            return None
        labels = row.get("label-info", [])
        if not isinstance(labels, list):
            labels = []
        credit = row.get("artist-credit")
        artist = _text(row.get("artist-credit-phrase"))
        if not artist and isinstance(credit, list):
            artist = " ".join(_text(item.get("name") or item.get("artist", {}).get("name")) or ""
                              for item in credit if isinstance(item, dict)).strip() or None
        media = row.get("media", []) if isinstance(row.get("media", []), list) else []
        track_counts = [m["track-count"] for m in media if isinstance(m, dict)
                        and type(m.get("track-count")) is int and m["track-count"] >= 0]
        return {"release_mbid": mbid, "title": _text(row.get("title")),
                "artist": artist, "date": _text(row.get("date"), 20),
                "country": _text(row.get("country"), 10),
                "formats": list(dict.fromkeys(_text(m.get("format"), 100) for m in media
                                               if isinstance(m, dict) and _text(m.get("format")))),
                "labels": [{"name": _text(x.get("label", {}).get("name")),
                            "catalog_number": _text(x.get("catalog-number"), 100)}
                           for x in labels[:10] if isinstance(x, dict) and isinstance(x.get("label"), dict)],
                "track_count": sum(track_counts) if track_counts else None}

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

    def cover_art_candidates(self, release_mbid):
        """Return approved front-cover thumbnail choices, never choosing one for the user."""
        release_mbid = str(UUID(release_mbid))
        key = "covers:" + release_mbid
        url = f"https://coverartarchive.org/release/{release_mbid}"
        try:
            data = self._get_caa(key, url)
        except (OSError, URLError, TimeoutError, ConnectionError) as exc:
            raise ConnectionError("Cover art is unavailable") from exc
        images = data.get("images", [])
        result = []
        for image in images[:30] if isinstance(images, list) else []:
            if not isinstance(image, dict) or image.get("front") is not True or image.get("approved") is not True:
                continue
            image_id = image.get("id")
            if not isinstance(image_id, (int, str)) or not str(image_id).isdecimal() or len(str(image_id)) > 20:
                continue
            result.append({"image_id": str(image_id),
                           "comment": _text(image.get("comment"), 160),
                           "thumbnail_url": f"/api/releases/artwork?release_mbid={release_mbid}&image_id={image_id}"})
            if len(result) == 4:
                break
        return result

    def cover_thumbnail(self, release_mbid, image_id):
        """Fetch and cache a bounded 250px JPEG through the allowlisted HTTPS proxy."""
        release_mbid = str(UUID(release_mbid))
        if not isinstance(image_id, (str, int)) or not str(image_id).isdecimal() or len(str(image_id)) > 20:
            raise ValueError("Invalid cover image ID")
        image_id = str(image_id)
        key = f"cover-image:{release_mbid}:{image_id}"
        with _db(self.database) as db:
            row = db.execute("SELECT body FROM metadata_cache WHERE cache_key=?", (key,)).fetchone()
        if row:
            cached = json.loads(row[0])
            return base64.b64decode(cached["jpeg"], validate=True)
        if image_id not in {item["image_id"] for item in self.cover_art_candidates(release_mbid)}:
            raise ValueError("Choose an approved front cover for this release")
        url = f"https://coverartarchive.org/release/{release_mbid}/{image_id}-250.jpg"
        request = Request(url, headers={"User-Agent": USER_AGENT, "Accept": "image/jpeg"})
        try:
            with self.cover_transport(request, timeout=8) as response:
                raw = response.read(500_001)
                final = urlsplit(getattr(response, "url", url))
                headers = getattr(response, "headers", {})
                content_type = headers.get("Content-Type") if hasattr(headers, "get") else None
        except (OSError, URLError, TimeoutError) as exc:
            raise ConnectionError("Cover art is unavailable") from exc
        if (final.scheme != "https" or final.hostname not in CAA_HOSTS
                or len(raw) > 500_000 or not raw.startswith(b"\xff\xd8\xff")
                or not isinstance(content_type, str)
                or content_type.split(";", 1)[0].strip().lower() != "image/jpeg"):
            raise ValueError("Cover art response is not a valid bounded JPEG")
        body = json.dumps({"jpeg": base64.b64encode(raw).decode("ascii")})
        with _db(self.database) as db:
            db.execute("INSERT OR REPLACE INTO metadata_cache VALUES(?,?,?)", (key, body, time.time()))
        return raw

    def pin(self, project_id, release_mbid, *, expected_revision, cover_image_id=None):
        """Fetch and materialize before CAS pin; a later online result cannot replace it."""
        snapshot = self.fetch_release(release_mbid)
        if cover_image_id is not None:
            cover_image_id = str(cover_image_id)
            if cover_image_id not in {item["image_id"] for item in self.cover_art_candidates(release_mbid)}:
                raise ValueError("Choose an approved front cover for this release")
        with _db(self.database) as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT revision FROM release_pins WHERE project_id=?", (project_id,)).fetchone()
            revision = row[0] if row else 0
            if revision != expected_revision:
                raise RevisionConflict(revision)
            db.execute("INSERT INTO release_pins VALUES(?,?,?,?,?) ON CONFLICT(project_id) DO UPDATE SET revision=excluded.revision, release_mbid=excluded.release_mbid, snapshot=excluded.snapshot, updated=excluded.updated",
                       (project_id, revision + 1, snapshot["release_mbid"],
                        json.dumps(snapshot, ensure_ascii=False), datetime.now(timezone.utc).isoformat()))
            if cover_image_id is None:
                db.execute("DELETE FROM project_covers WHERE project_id=?", (project_id,))
            else:
                db.execute("INSERT OR REPLACE INTO project_covers VALUES(?,?,?)",
                           (project_id, snapshot["release_mbid"], cover_image_id))
        return {"revision": revision + 1, "release": snapshot}

    def get_pin(self, project_id):
        with _db(self.database) as db:
            row = db.execute("SELECT revision,release_mbid,snapshot FROM release_pins WHERE project_id=?", (project_id,)).fetchone()
            cover = db.execute("SELECT release_mbid,image_id FROM project_covers WHERE project_id=?", (project_id,)).fetchone()
        if not row:
            return None
        value = {"revision": row[0], "release_mbid": row[1], "release": json.loads(row[2])}
        if cover and cover[0] == row[1]:
            value["cover"] = {"image_id": cover[1], "thumbnail_url":
                               f"/api/releases/artwork?release_mbid={cover[0]}&image_id={cover[1]}"}
        else:
            value["cover"] = None
        return value

    def clear_pin(self, project_id, *, expected_revision):
        with _db(self.database) as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT revision FROM release_pins WHERE project_id=?", (project_id,)).fetchone()
            revision = row[0] if row else 0
            if revision != expected_revision:
                raise RevisionConflict(revision)
            db.execute("INSERT INTO release_pins VALUES(?,?,NULL,NULL,?) ON CONFLICT(project_id) DO UPDATE SET revision=excluded.revision,release_mbid=NULL,snapshot=NULL,updated=excluded.updated",
                       (project_id, revision + 1, datetime.now(timezone.utc).isoformat()))
            db.execute("DELETE FROM project_covers WHERE project_id=?", (project_id,))
        return {"revision": revision + 1, "release_mbid": None, "release": None}


class RevisionConflict(RuntimeError):
    def __init__(self, current_revision):
        super().__init__("Release pin changed; reload before applying")
        self.current_revision = current_revision
