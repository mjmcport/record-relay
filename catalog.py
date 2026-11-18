"""Private SQLite catalog and crash-recoverable immutable master publication.

This module deliberately has no dependency on the capture loop or panel server.
Callers prepare an intent before starting a writer, then publish a closed WAV
and commit its receipt. Paths are stored relative to the configured library.
"""
from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import stat
import uuid
import wave
from contextlib import contextmanager
from pathlib import Path

SCHEMA_VERSION = 3


class _ClosingConnection(sqlite3.Connection):
    """Close SQLite handles used with ``with`` after its transaction ends."""

    def __exit__(self, exc_type, exc, traceback):
        try:
            return super().__exit__(exc_type, exc, traceback)
        finally:
            self.close()


class CatalogError(Exception):
    """Base class for catalog errors."""


class Conflict(CatalogError):
    """An optimistic concurrency or immutable-name conflict."""


class InvalidMaster(CatalogError):
    """A candidate master is not a complete, valid WAV."""


def _json(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def sha256_file(path: Path, chunk_size: int = 1024 * 1024) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while True:
            chunk = stream.read(chunk_size)
            if not chunk:
                break
            digest.update(chunk)
    return digest.hexdigest()


def _validated_match_sidecar(path: Path, frames: int) -> bytes | None:
    if path.is_symlink():
        raise InvalidMaster("Recognition sidecar may not be a symbolic link")
    if not path.exists():
        return None
    if not path.is_file() or path.stat().st_size > 512_000:
        raise InvalidMaster("Recognition sidecar is not a bounded regular file")
    try:
        raw = path.read_bytes()
        cues = json.loads(raw)
    except (OSError, ValueError) as exc:
        raise InvalidMaster("Recognition sidecar is invalid") from exc
    if not isinstance(cues, list) or len(cues) > 512:
        raise InvalidMaster("Recognition sidecar has an invalid cue list")
    for cue in cues:
        if not isinstance(cue, dict):
            raise InvalidMaster("Recognition sidecar contains an invalid cue")
        tags = cue.get("tags")
        if (type(cue.get("frame")) is not int or not 0 <= cue["frame"] < frames
                or not isinstance(tags, dict) or not tags
                or any(key not in {"title", "artist", "album"} or not isinstance(value, str)
                       or len(value) > 300 or "\0" in value for key, value in tags.items())):
            raise InvalidMaster("Recognition sidecar contains an invalid cue")
    return raw


def validate_wav(path: Path, expected_frames: int | None = None) -> dict:
    """Read WAV metadata and ensure its data chunk contains complete frames."""
    try:
        with wave.open(str(path), "rb") as wav:
            channels, width, rate, frames = (wav.getnchannels(), wav.getsampwidth(),
                                               wav.getframerate(), wav.getnframes())
            if channels < 1 or width < 1 or rate < 1:
                raise InvalidMaster("WAV has invalid audio parameters")
            if frames < 1:
                raise InvalidMaster("WAV contains no audio frames")
            if expected_frames is not None and frames != expected_frames:
                raise InvalidMaster("WAV frame count does not match the closed writer")
            # Reading all frames catches a truncated data chunk before publication.
            remaining = frames
            while remaining:
                data = wav.readframes(min(remaining, 65536))
                if not data:
                    raise InvalidMaster("WAV data is truncated")
                frame_bytes = channels * width
                got = len(data) // frame_bytes
                if len(data) % frame_bytes or got == 0:
                    raise InvalidMaster("WAV ends in a partial audio frame")
                remaining -= got
        return {"channels": channels, "sample_width": width, "sample_rate": rate,
                "frames": frames, "size": path.stat().st_size}
    except (wave.Error, EOFError, OSError) as exc:
        raise InvalidMaster(f"Unable to validate WAV: {exc}") from exc


class Catalog:
    """Transactional catalog, independent of listens.sqlite."""

    def __init__(self, path: str | Path, library: str | Path):
        requested_path = Path(path).expanduser()
        if requested_path.is_symlink():
            raise CatalogError("Catalog database may not be a symbolic link")
        self.path = requested_path.resolve(strict=False)
        self.library = Path(library).resolve()
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.library.mkdir(parents=True, exist_ok=True, mode=0o700)
        parent_info = self.path.parent.stat()
        if parent_info.st_uid != os.getuid() or stat.S_IMODE(parent_info.st_mode) & 0o077:
            raise CatalogError("Catalog state directory must be owned by this user and mode 0700")
        library_info = self.library.stat()
        if library_info.st_uid != os.getuid():
            raise CatalogError("Audio library must be owned by this user")
        if stat.S_IMODE(library_info.st_mode) & 0o077:
            os.chmod(self.library, 0o700)
        self._migrate()

    def connect(self):
        if self.path.is_symlink() or any(Path(str(self.path) + suffix).is_symlink()
                                          for suffix in ("-wal", "-shm")):
            raise CatalogError("Catalog database and SQLite sidecars may not be symbolic links")
        db = sqlite3.connect(self.path, timeout=10, isolation_level=None,
                             factory=_ClosingConnection)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA foreign_keys=ON")
        db.execute("PRAGMA busy_timeout=10000")
        db.execute("PRAGMA journal_mode=WAL")
        db.execute("PRAGMA synchronous=FULL")
        return db

    @contextmanager
    def transaction(self):
        db = self.connect()
        try:
            db.execute("BEGIN IMMEDIATE")
            yield db
            db.commit()
        except Exception:
            db.rollback()
            raise
        finally:
            db.close()

    def _migrate(self):
        with self.connect() as db:
            db.execute("CREATE TABLE IF NOT EXISTS schema_info (version INTEGER NOT NULL)")
            row = db.execute("SELECT version FROM schema_info LIMIT 1").fetchone()
            if row and row[0] > SCHEMA_VERSION:
                raise CatalogError(f"Catalog schema {row[0]} is newer than supported {SCHEMA_VERSION}")
            db.executescript("""
                CREATE TABLE IF NOT EXISTS projects (
                    id TEXT PRIMARY KEY, label TEXT NOT NULL DEFAULT '', revision INTEGER NOT NULL DEFAULT 1,
                    state TEXT NOT NULL DEFAULT 'open', metadata TEXT NOT NULL DEFAULT '{}', created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS takes (
                    id TEXT PRIMARY KEY, project_id TEXT NOT NULL REFERENCES projects(id), side TEXT,
                    state TEXT NOT NULL, revision INTEGER NOT NULL DEFAULT 1, staging_id TEXT UNIQUE,
                    locator TEXT UNIQUE, master_sha256 TEXT, frames INTEGER, sample_rate INTEGER,
                    channels INTEGER, sample_width INTEGER, created_at TEXT NOT NULL, published_at TEXT
                );
                CREATE TABLE IF NOT EXISTS intents (
                    request_id TEXT PRIMARY KEY, request_digest TEXT NOT NULL, project_id TEXT NOT NULL REFERENCES projects(id),
                    take_id TEXT NOT NULL UNIQUE REFERENCES takes(id), capture_generation TEXT NOT NULL,
                    boot_generation TEXT NOT NULL, side TEXT NOT NULL, staging_id TEXT NOT NULL UNIQUE,
                    staging_locator TEXT NOT NULL UNIQUE, intended_locator TEXT NOT NULL UNIQUE,
                    expected_revision INTEGER NOT NULL, state TEXT NOT NULL, expected_frames INTEGER,
                    result TEXT, error TEXT, created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
                    side_a_end_frame INTEGER CHECK(side_a_end_frame IS NULL OR side_a_end_frame >= 0)
                );
                CREATE TABLE IF NOT EXISTS segments (
                    id TEXT PRIMARY KEY, take_id TEXT NOT NULL REFERENCES takes(id), start_frame INTEGER NOT NULL,
                    end_frame INTEGER NOT NULL, revision INTEGER NOT NULL DEFAULT 1, metadata TEXT NOT NULL DEFAULT '{}'
                );
                CREATE TABLE IF NOT EXISTS observations (
                    id TEXT PRIMARY KEY, project_id TEXT NOT NULL REFERENCES projects(id), take_id TEXT,
                    kind TEXT NOT NULL, observed_at TEXT NOT NULL, provenance TEXT NOT NULL, payload TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS artifacts (
                    id TEXT PRIMARY KEY, project_id TEXT NOT NULL REFERENCES projects(id), kind TEXT NOT NULL,
                    digest TEXT NOT NULL, locator TEXT NOT NULL, size INTEGER NOT NULL, created_at TEXT NOT NULL,
                    UNIQUE(kind, digest)
                );
                CREATE TABLE IF NOT EXISTS drafts (
                    id TEXT PRIMARY KEY, take_id TEXT NOT NULL REFERENCES takes(id), master_sha256 TEXT NOT NULL,
                    source_locator TEXT NOT NULL, payload TEXT NOT NULL, imported_at TEXT NOT NULL,
                    UNIQUE(take_id, master_sha256)
                );
                CREATE TABLE IF NOT EXISTS commands (
                    request_id TEXT PRIMARY KEY, request_digest TEXT NOT NULL, state TEXT NOT NULL,
                    result TEXT, error TEXT, created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS side_boundaries (
                    project_id TEXT NOT NULL REFERENCES projects(id),
                    capture_generation TEXT NOT NULL,
                    end_frame INTEGER NOT NULL CHECK(end_frame >= 0),
                    take_id TEXT NOT NULL REFERENCES takes(id),
                    recorded_at TEXT NOT NULL,
                    PRIMARY KEY(project_id, capture_generation)
                );
                CREATE INDEX IF NOT EXISTS takes_project_idx ON takes(project_id, created_at);
                CREATE UNIQUE INDEX IF NOT EXISTS one_active_take_per_project_side
                    ON takes(project_id, side) WHERE state IN ('preparing','recording','finalizing');
                CREATE INDEX IF NOT EXISTS observations_project_idx ON observations(project_id, observed_at);
            """)
            intent_columns = {column[1] for column in db.execute("PRAGMA table_info(intents)")}
            if "side_a_end_frame" not in intent_columns:
                db.execute("ALTER TABLE intents ADD COLUMN side_a_end_frame INTEGER "
                           "CHECK(side_a_end_frame IS NULL OR side_a_end_frame >= 0)")
            if not row:
                db.execute("INSERT INTO schema_info(version) VALUES (?)", (SCHEMA_VERSION,))
            elif row[0] < SCHEMA_VERSION:
                db.execute("UPDATE schema_info SET version=?", (SCHEMA_VERSION,))
        try:
            os.chmod(self.path, 0o600)
        except FileNotFoundError:
            pass

    @staticmethod
    def _now():
        from datetime import datetime, timezone
        return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")

    def _relative(self, locator: str) -> Path:
        if (not isinstance(locator, str) or not locator or "\\" in locator or "\0" in locator
                or any(part in ("", ".", "..") for part in locator.split("/"))):
            raise CatalogError("Locator must be a normalized relative path")
        candidate = Path(locator)
        if candidate.is_absolute() or candidate.as_posix() != locator:
            raise CatalogError("Locator must be a normalized relative path")
        target = self.library.joinpath(candidate)
        resolved_parent = target.parent.resolve()
        try:
            resolved_parent.relative_to(self.library)
        except ValueError as exc:
            raise CatalogError("Locator must stay inside the library") from exc
        cursor = self.library
        for part in candidate.parts[:-1]:
            cursor = cursor / part
            if cursor.is_symlink():
                raise CatalogError("Locator may not traverse symlinks")
        if target.is_symlink():
            raise CatalogError("Locator may not traverse symlinks")
        return target

    def create_project(self, label: str = "", *, project_id: str | None = None, metadata=None) -> dict:
        project_id = project_id or str(uuid.uuid4())
        now = self._now()
        with self.transaction() as db:
            db.execute("INSERT INTO projects(id,label,metadata,created_at) VALUES(?,?,?,?)",
                       (project_id, label, _json(metadata or {}), now))
        return self.get_project(project_id)

    def get_project(self, project_id: str) -> dict | None:
        with self.connect() as db:
            row = db.execute("SELECT * FROM projects WHERE id=?", (project_id,)).fetchone()
        if not row:
            return None
        result = dict(row)
        result["metadata"] = json.loads(result["metadata"])
        return result

    def get_or_create_project(self, project_id: str, label: str = "") -> dict:
        existing = self.get_project(project_id)
        if existing:
            return existing
        try:
            return self.create_project(label, project_id=project_id)
        except sqlite3.IntegrityError:
            return self.get_project(project_id)

    def projects(self, *, limit=50):
        if not 1 <= limit <= 200:
            raise ValueError("limit must be between 1 and 200")
        with self.connect() as db:
            rows = db.execute("SELECT * FROM projects ORDER BY created_at DESC LIMIT ?", (limit,)).fetchall()
        result = []
        for row in rows:
            item = dict(row)
            item["metadata"] = json.loads(item["metadata"])
            result.append(item)
        return result

    def edit_project(self, project_id: str, expected_revision: int, *, label: str | None = None,
                     metadata: dict | None = None) -> dict:
        with self.transaction() as db:
            row = db.execute("SELECT * FROM projects WHERE id=?", (project_id,)).fetchone()
            if not row:
                raise KeyError(project_id)
            if row["revision"] != expected_revision:
                raise Conflict(f"Project revision is {row['revision']}")
            db.execute("UPDATE projects SET label=?, metadata=?, revision=revision+1 WHERE id=? AND revision=?",
                       (row["label"] if label is None else label,
                        row["metadata"] if metadata is None else _json(metadata), project_id, expected_revision))
        return self.get_project(project_id)

    def begin_take(self, *, request_id: str, project_id: str, side: str, capture_generation: str,
                   boot_generation: str, staging_locator: str, intended_locator: str,
                   expected_revision: int = 1) -> dict:
        if side not in {"A", "B"}:
            raise ValueError("side must be A or B")
        staging = self._relative(staging_locator)
        target = self._relative(intended_locator)
        if staging == target or staging.parent != target.parent:
            raise ValueError("staging and published master must be distinct files in the same directory")
        now, take_id, staging_id = self._now(), str(uuid.uuid4()), str(uuid.uuid4())
        body = {"action": "begin_take", "project_id": project_id, "side": side,
                "capture_generation": capture_generation, "boot_generation": boot_generation,
                "staging_locator": staging_locator, "intended_locator": intended_locator,
                "expected_revision": expected_revision}
        digest = hashlib.sha256(_json(body).encode()).hexdigest()
        with self.transaction() as db:
            old = db.execute("SELECT * FROM intents WHERE request_id=?", (request_id,)).fetchone()
            if old:
                if old["request_digest"] != digest:
                    raise Conflict("request_id was already used for a different command")
                return dict(old)
            project = db.execute("SELECT revision FROM projects WHERE id=?", (project_id,)).fetchone()
            if not project:
                raise KeyError(project_id)
            if project["revision"] != expected_revision:
                raise Conflict(f"Project revision is {project['revision']}")
            try:
                db.execute("INSERT INTO takes(id,project_id,side,state,staging_id,created_at) VALUES(?,?,?,'preparing',?,?)",
                           (take_id, project_id, side, staging_id, now))
                db.execute("INSERT INTO intents(request_id,request_digest,project_id,take_id,capture_generation,boot_generation,side,staging_id,staging_locator,intended_locator,expected_revision,state,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,'prepared',?,?)",
                           (request_id, digest, project_id, take_id, capture_generation, boot_generation, side,
                            staging_id, staging_locator, intended_locator, expected_revision, now, now))
            except sqlite3.IntegrityError as exc:
                raise Conflict("An active take or intent already owns this side/locator") from exc
        with self.transaction() as db:
            db.execute("UPDATE intents SET state='writing',updated_at=? WHERE request_id=? AND state='prepared'", (self._now(), request_id))
            db.execute("UPDATE takes SET state='recording' WHERE id=? AND state='preparing'", (take_id,))
        return self.get_intent(request_id)

    def get_intent(self, request_id: str) -> dict | None:
        with self.connect() as db:
            row = db.execute("SELECT * FROM intents WHERE request_id=?", (request_id,)).fetchone()
        return dict(row) if row else None

    def record_side_boundary(self, project_id: str, capture_generation: str,
                             end_frame: int, take_id: str):
        if (not isinstance(capture_generation, str) or not capture_generation
                or type(end_frame) is not int or end_frame < 0):
            raise ValueError("Invalid saved-side boundary")
        now = self._now()
        with self.transaction() as db:
            take = db.execute("SELECT project_id,side,state FROM takes WHERE id=?", (take_id,)).fetchone()
            if not take or take["project_id"] != project_id or take["side"] != "A" or take["state"] != "saved":
                raise Conflict("Only a saved Side A can establish this boundary")
            db.execute("INSERT INTO side_boundaries(project_id,capture_generation,end_frame,take_id,recorded_at) "
                       "VALUES(?,?,?,?,?) ON CONFLICT(project_id,capture_generation) DO UPDATE SET "
                       "end_frame=excluded.end_frame,take_id=excluded.take_id,recorded_at=excluded.recorded_at",
                       (project_id, capture_generation, end_frame, take_id, now))

    def get_side_boundary(self, project_id: str, capture_generation: str | None = None):
        with self.connect() as db:
            if capture_generation is None:
                row = db.execute("SELECT * FROM side_boundaries WHERE project_id=? "
                                 "ORDER BY recorded_at DESC LIMIT 1", (project_id,)).fetchone()
            else:
                row = db.execute("SELECT * FROM side_boundaries WHERE project_id=? AND capture_generation=?",
                                 (project_id, capture_generation)).fetchone()
        return dict(row) if row else None

    def close_take(self, request_id: str, *, expected_frames: int,
                   side_a_end_frame: int | None = None) -> dict:
        """Record the writer's closed frame count before attempting publication."""
        if type(expected_frames) is not int or expected_frames <= 0:
            raise InvalidMaster("A saved side must contain complete audio frames")
        if side_a_end_frame is not None and (type(side_a_end_frame) is not int or side_a_end_frame < 0):
            raise InvalidMaster("Side A boundary must be a non-negative capture frame")
        with self.transaction() as db:
            row = db.execute("SELECT take_id,state,side,side_a_end_frame FROM intents WHERE request_id=?", (request_id,)).fetchone()
            if not row:
                raise KeyError(request_id)
            if row["state"] in ("closed", "verified"):
                current = db.execute("SELECT expected_frames FROM intents WHERE request_id=?", (request_id,)).fetchone()[0]
                if current != expected_frames:
                    raise Conflict("Closed writer frame count changed")
                if side_a_end_frame is not None:
                    if row["side"] != "A":
                        raise InvalidMaster("Only Side A can persist a Side A boundary")
                    if row["side_a_end_frame"] is not None and row["side_a_end_frame"] != side_a_end_frame:
                        raise Conflict("Side A capture boundary changed after close")
                    db.execute("UPDATE intents SET side_a_end_frame=COALESCE(side_a_end_frame,?),updated_at=? WHERE request_id=?",
                               (side_a_end_frame, self._now(), request_id))
                return self.get_intent(request_id)
            if row["state"] != "writing":
                raise Conflict("Recording intent is no longer writable")
            if side_a_end_frame is not None and row["side"] != "A":
                raise InvalidMaster("Only Side A can persist a Side A boundary")
            if row["side_a_end_frame"] is not None and side_a_end_frame is not None and row["side_a_end_frame"] != side_a_end_frame:
                raise Conflict("Side A capture boundary changed after close")
            db.execute("UPDATE intents SET state='closed',expected_frames=?,side_a_end_frame=COALESCE(side_a_end_frame,?),updated_at=? WHERE request_id=?",
                       (expected_frames, side_a_end_frame, self._now(), request_id))
            db.execute("UPDATE takes SET state='finalizing' WHERE id=? AND state='recording'", (row["take_id"],))
        return self.get_intent(request_id)

    def fail_take(self, request_id: str, reason: str):
        """Close a failed writer intent without presenting it as a saved master."""
        reason = str(reason)[:300]
        with self.transaction() as db:
            row = db.execute("SELECT take_id,state FROM intents WHERE request_id=?", (request_id,)).fetchone()
            if not row or row["state"] in ("committed", "failed", "quarantined", "interrupted"):
                return
            db.execute("UPDATE intents SET state='interrupted',error=?,updated_at=? WHERE request_id=?",
                       (reason, self._now(), request_id))
            db.execute("UPDATE takes SET state='interrupted' WHERE id=?", (row["take_id"],))

    def quarantine_take(self, request_id: str, reason: str):
        """Release a corrupt/ambiguous take from the active-side uniqueness guard."""
        intent = self.get_intent(request_id)
        if intent:
            self._mark_quarantined(intent, str(reason))

    def publish_take(self, request_id: str, *, expected_frames: int | None = None) -> dict:
        """Verify closed staging WAV, durably publish without replacement, commit receipt."""
        intent = self.get_intent(request_id)
        if not intent:
            raise KeyError(request_id)
        if intent["state"] == "committed":
            return json.loads(intent["result"])
        stage, final = self._relative(intent["staging_locator"]), self._relative(intent["intended_locator"])
        for candidate in (stage, final):
            if candidate.is_symlink():
                raise InvalidMaster("Staging and publication paths may not be symlinks")
        info = validate_wav(stage, expected_frames)
        with stage.open("rb") as stream:
            os.fsync(stream.fileno())
        stage_matches = stage.with_suffix(".matches.json")
        final_matches = final.with_suffix(".matches.json")
        if final_matches.is_symlink():
            raise Conflict("Published recognition sidecar may not be a symbolic link")
        match_bytes = _validated_match_sidecar(stage_matches, info["frames"])
        if final_matches.exists() and match_bytes is None:
            raise Conflict("A recognition sidecar already occupies the publication locator")
        if match_bytes is not None:
            with stage_matches.open("rb") as stream:
                os.fsync(stream.fileno())
        digest = sha256_file(stage)
        with self.transaction() as db:
            if expected_frames is None:
                expected_frames = intent.get("expected_frames")
            if expected_frames is not None and expected_frames != info["frames"]:
                raise InvalidMaster("WAV frame count does not match the closed writer")
            db.execute("UPDATE intents SET state='verified',expected_frames=?,updated_at=? WHERE request_id=? AND state IN ('writing','closed','verified')",
                       (info["frames"], self._now(), request_id))
        # Hard-link is an atomic, same-filesystem no-replace publication. Keep the
        # staging name until the directory entry and DB receipt are both durable.
        try:
            os.link(stage, final, follow_symlinks=False)
        except FileExistsError as exc:
            if not final.is_file() or sha256_file(final) != digest:
                raise Conflict("Published locator already contains different bytes") from exc
        dir_fd = os.open(final.parent, os.O_RDONLY)
        try:
            if match_bytes is not None:
                try:
                    os.link(stage_matches, final_matches, follow_symlinks=False)
                except FileExistsError as exc:
                    if (not final_matches.is_file() or not os.path.samefile(stage_matches, final_matches)
                            or final_matches.read_bytes() != match_bytes):
                        raise Conflict("Published recognition sidecar contains different bytes") from exc
            os.fsync(dir_fd)
        finally:
            os.close(dir_fd)
        receipt = {"take_id": intent["take_id"], "project_id": intent["project_id"],
                   "side": intent["side"], "locator": intent["intended_locator"],
                   "sha256": digest, **info}
        if intent["side"] == "A" and intent.get("side_a_end_frame") is not None:
            receipt["side_a_end_frame"] = intent["side_a_end_frame"]
            receipt["capture_generation"] = intent["capture_generation"]
        now = self._now()
        with self.transaction() as db:
            current = db.execute("SELECT state FROM intents WHERE request_id=?", (request_id,)).fetchone()
            if not current or current["state"] not in ("verified", "published", "committed"):
                raise Conflict("Recording intent changed during publication")
            db.execute("UPDATE intents SET state='committed',result=?,updated_at=? WHERE request_id=?",
                       (_json(receipt), now, request_id))
            db.execute("UPDATE takes SET state='saved',locator=?,master_sha256=?,frames=?,sample_rate=?,channels=?,sample_width=?,published_at=? WHERE id=?",
                       (intent["intended_locator"], digest, info["frames"], info["sample_rate"],
                        info["channels"], info["sample_width"], now, intent["take_id"]))
            if intent["side"] == "A" and intent.get("side_a_end_frame") is not None:
                db.execute("INSERT INTO side_boundaries(project_id,capture_generation,end_frame,take_id,recorded_at) "
                           "VALUES(?,?,?,?,?) ON CONFLICT(project_id,capture_generation) DO UPDATE SET "
                           "end_frame=excluded.end_frame,take_id=excluded.take_id,recorded_at=excluded.recorded_at",
                           (intent["project_id"], intent["capture_generation"], intent["side_a_end_frame"],
                            intent["take_id"], now))
        # Once the durable catalog receipt exists, remove the redundant staging
        # hard link. A crash before this cleanup leaves a harmless duplicate.
        try:
            if stage.is_file() and final.is_file() and os.path.samefile(stage, final):
                stage.unlink()
                if stage_matches.is_file() and final_matches.is_file() and os.path.samefile(stage_matches, final_matches):
                    stage_matches.unlink()
                dir_fd = os.open(final.parent, os.O_RDONLY)
                try:
                    os.fsync(dir_fd)
                finally:
                    os.close(dir_fd)
        except OSError:
            pass
        return receipt

    def reconcile(self, *, live_request_ids=(), defer_unverified_writing=False) -> list[dict]:
        """Resolve crash gaps by stable intent identity; never claim partial WAVs saved."""
        live_request_ids = set(live_request_ids)
        with self.connect() as db:
            rows = db.execute("SELECT * FROM intents WHERE state NOT IN ('committed','failed','quarantined','interrupted') ORDER BY created_at").fetchall()
        outcomes = []
        for row in rows:
            intent = dict(row)
            if intent["request_id"] in live_request_ids:
                continue
            if defer_unverified_writing and intent["state"] == "writing":
                continue
            stage, final = self._relative(intent["staging_locator"]), self._relative(intent["intended_locator"])
            if stage.is_symlink() or final.is_symlink():
                self._mark_quarantined(intent, "staging_or_publication_path_is_symlink")
                outcomes.append({"request_id": intent["request_id"], "state": "quarantined",
                                 "reason": "staging_or_publication_path_is_symlink"})
                continue
            candidate = final if final.exists() else stage if stage.exists() else None
            if candidate is None:
                self._mark_failed(intent, "missing_source")
                outcomes.append({"request_id": intent["request_id"], "state": "failed", "reason": "missing_source"})
                continue
            try:
                validate_wav(candidate, intent["expected_frames"])
                if candidate == final:
                    # A published file is adoptable only when its bytes correspond
                    # to this take's staging inode (or a prior receipt digest).
                    if stage.exists() and os.stat(stage).st_ino != os.stat(final).st_ino:
                        raise InvalidMaster("published file does not match staging identity")
                    if not stage.exists() and not intent["result"]:
                        raise InvalidMaster("published file has no matching staging identity")
                receipt = self.publish_take(intent["request_id"], expected_frames=intent["expected_frames"])
                outcomes.append({"request_id": intent["request_id"], "state": "committed", "receipt": receipt})
            except (InvalidMaster, Conflict, OSError) as exc:
                self._mark_quarantined(intent, str(exc))
                outcomes.append({"request_id": intent["request_id"], "state": "quarantined", "reason": str(exc)})
        return outcomes

    def _mark_failed(self, intent, error):
        with self.transaction() as db:
            db.execute("UPDATE intents SET state='failed',error=?,updated_at=? WHERE request_id=?", (error, self._now(), intent["request_id"]))
            db.execute("UPDATE takes SET state='failed' WHERE id=?", (intent["take_id"],))

    def _mark_quarantined(self, intent, error):
        with self.transaction() as db:
            db.execute("UPDATE intents SET state='quarantined',error=?,updated_at=? WHERE request_id=?", (error[:300], self._now(), intent["request_id"]))
            db.execute("UPDATE takes SET state='interrupted' WHERE id=?", (intent["take_id"],))

    def history(self, *, limit=50, cursor=None):
        if not 1 <= limit <= 200:
            raise ValueError("limit must be between 1 and 200")
        with self.connect() as db:
            if cursor:
                rows = db.execute("SELECT t.*,p.label FROM takes t JOIN projects p ON p.id=t.project_id WHERE t.created_at < ? ORDER BY t.created_at DESC LIMIT ?", (cursor, limit + 1)).fetchall()
            else:
                rows = db.execute("SELECT t.*,p.label FROM takes t JOIN projects p ON p.id=t.project_id ORDER BY t.created_at DESC LIMIT ?", (limit + 1,)).fetchall()
        items = [dict(row) for row in rows[:limit]]
        return {"schema_version": 1, "items": items,
                "next_cursor": items[-1]["created_at"] if len(rows) > limit and items else None}

    def take_for_locator(self, locator: str):
        self._relative(locator)
        with self.connect() as db:
            row = db.execute("SELECT * FROM takes WHERE locator=?", (locator,)).fetchone()
        return None if row is None else dict(row)

    def save_review_draft(self, take_id: str, payload: dict) -> dict:
        if not isinstance(payload, dict):
            raise ValueError("Review draft must be an object")
        try:
            encoded = _json(payload)
        except (TypeError, ValueError) as exc:
            raise ValueError("Review draft contains unsupported data") from exc
        if len(encoded.encode("utf-8")) > 8_100_000:
            raise ValueError("Review draft exceeds the supported size")
        with self.connect() as db:
            take = db.execute("SELECT * FROM takes WHERE id=? AND state='saved'", (take_id,)).fetchone()
        if not take or not take["locator"] or not take["master_sha256"]:
            raise CatalogError("Review draft needs a saved, verified master")
        source = self._relative(take["locator"])
        if source.is_symlink() or not source.is_file() or sha256_file(source) != take["master_sha256"]:
            raise Conflict("Master identity changed; reload before saving the review")
        draft = dict(payload)
        draft["master_sha256"] = take["master_sha256"]
        encoded = _json(draft)
        now, draft_id = self._now(), str(uuid.uuid4())
        with self.transaction() as db:
            db.execute("INSERT INTO drafts(id,take_id,master_sha256,source_locator,payload,imported_at) VALUES(?,?,?,?,?,?) ON CONFLICT(take_id,master_sha256) DO UPDATE SET payload=excluded.payload,source_locator=excluded.source_locator,imported_at=excluded.imported_at",
                       (draft_id, take_id, take["master_sha256"], take["locator"], encoded, now))
            row = db.execute("SELECT id FROM drafts WHERE take_id=? AND master_sha256=?",
                             (take_id, take["master_sha256"])).fetchone()
        return {"saved": True, "id": row[0], "master_sha256": take["master_sha256"]}

    def load_review_draft(self, take_id: str) -> dict:
        with self.connect() as db:
            row = db.execute("SELECT d.*,t.locator,t.master_sha256 AS current_sha FROM drafts d JOIN takes t ON t.id=d.take_id WHERE d.take_id=? ORDER BY d.imported_at DESC LIMIT 1", (take_id,)).fetchone()
        if not row:
            return {}
        source = self._relative(row["locator"])
        if (row["current_sha"] != row["master_sha256"] or source.is_symlink()
                or not source.is_file() or sha256_file(source) != row["master_sha256"]):
            raise Conflict("Saved review belongs to different master bytes")
        return json.loads(row["payload"])

    def import_legacy_wav(self, source: str | Path, *, project_id: str, side: str | None = None) -> dict:
        """Lazily register an existing WAV; never copy, rename, or modify it."""
        source = Path(source)
        if source.is_symlink() or not source.is_file():
            raise InvalidMaster("Legacy master must be a regular, non-symlink file")
        info = validate_wav(source)
        digest = sha256_file(source)
        try:
            locator = source.resolve().relative_to(self.library).as_posix()
        except ValueError as exc:
            raise CatalogError("Legacy master must be inside the configured library") from exc
        now, take_id = self._now(), str(uuid.uuid4())
        with self.transaction() as db:
            existing = db.execute("SELECT * FROM takes WHERE locator=?", (locator,)).fetchone()
            if existing:
                if existing["master_sha256"] and existing["master_sha256"] != digest:
                    raise Conflict("Legacy locator now refers to different master bytes")
                return dict(existing)
            db.execute("INSERT INTO takes(id,project_id,side,state,locator,master_sha256,frames,sample_rate,channels,sample_width,created_at,published_at) VALUES(?,?,?,'saved',?,?,?,?,?,?,?,?)",
                       (take_id, project_id, side if side in ("A", "B") else None, locator, digest,
                        info["frames"], info["sample_rate"], info["channels"], info["sample_width"], now, now))
        return {"id": take_id, "project_id": project_id, "locator": locator,
                "side": side if side in ("A", "B") else None,
                "master_sha256": digest, **info}

    def import_legacy_draft(self, take_id: str, draft_path: str | Path) -> dict:
        """Preserve/import an old draft only when its master identity is verified."""
        source = Path(draft_path)
        try:
            raw = source.read_bytes()
            if len(raw) > 2_000_000:
                return {"state": "invalid", "reason": "draft_too_large", "source": str(source)}
            payload = json.loads(raw)
            if not isinstance(payload, dict):
                raise ValueError("draft must be an object")
        except (OSError, ValueError, json.JSONDecodeError) as exc:
            return {"state": "invalid", "reason": str(exc)[:200], "source": str(source)}
        with self.connect() as db:
            take = db.execute("SELECT * FROM takes WHERE id=?", (take_id,)).fetchone()
        if not take or not take["master_sha256"]:
            return {"state": "unmatched", "reason": "take_has_no_verified_master", "source": str(source)}
        identity = payload.get("master_sha256") or payload.get("sha256")
        # Legacy drafts keyed only by filename are deliberately not auto-bound.
        if identity != take["master_sha256"]:
            return {"state": "unmatched", "reason": "master_identity_mismatch", "source": str(source)}
        now = self._now()
        with self.transaction() as db:
            row = db.execute("SELECT * FROM drafts WHERE take_id=? AND master_sha256=?", (take_id, identity)).fetchone()
            if row:
                return {"state": "imported", "id": row["id"], "duplicate": True}
            draft_id = str(uuid.uuid4())
            db.execute("INSERT INTO drafts(id,take_id,master_sha256,source_locator,payload,imported_at) VALUES(?,?,?,?,?,?)",
                       (draft_id, take_id, identity, source.name, _json(payload), now))
        return {"state": "imported", "id": draft_id, "duplicate": False}

    def store_artifact(self, project_id: str, kind: str, data: bytes, *, max_bytes=8_000_000) -> dict:
        """Store bounded art/artifacts by digest with exclusive creation."""
        if not isinstance(data, bytes) or len(data) > max_bytes:
            raise ValueError("artifact is not bytes or exceeds size limit")
        digest = hashlib.sha256(data).hexdigest()
        relative = Path("artifacts") / kind / digest
        target = self._relative(relative.as_posix())
        target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        try:
            fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        except FileExistsError:
            if sha256_file(target) != digest:
                raise Conflict("Digest-addressed artifact is corrupt")
        else:
            with os.fdopen(fd, "wb") as stream:
                stream.write(data)
                stream.flush()
                os.fsync(stream.fileno())
            dir_fd = os.open(target.parent, os.O_RDONLY)
            try:
                os.fsync(dir_fd)
            finally:
                os.close(dir_fd)
        now = self._now()
        with self.transaction() as db:
            row = db.execute("SELECT * FROM artifacts WHERE kind=? AND digest=?", (kind, digest)).fetchone()
            if row:
                return dict(row)
            artifact_id = str(uuid.uuid4())
            db.execute("INSERT INTO artifacts(id,project_id,kind,digest,locator,size,created_at) VALUES(?,?,?,?,?,?,?)",
                       (artifact_id, project_id, kind, digest, relative.as_posix(), len(data), now))
        return {"id": artifact_id, "project_id": project_id, "kind": kind,
                "digest": digest, "locator": relative.as_posix(), "size": len(data), "created_at": now}
