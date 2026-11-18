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

SCHEMA_VERSION = 1


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


def validate_wav(path: Path, expected_frames: int | None = None) -> dict:
    """Read WAV metadata and ensure its data chunk contains complete frames."""
    try:
        with wave.open(str(path), "rb") as wav:
            channels, width, rate, frames = (wav.getnchannels(), wav.getsampwidth(),
                                               wav.getframerate(), wav.getnframes())
            if channels < 1 or width < 1 or rate < 1:
                raise InvalidMaster("WAV has invalid audio parameters")
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
        self.path = Path(path)
        self.library = Path(library).resolve()
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.library.mkdir(parents=True, exist_ok=True, mode=0o700)
        os.chmod(self.path.parent, 0o700)
        os.chmod(self.library, 0o700)
        self._migrate()

    def connect(self):
        db = sqlite3.connect(self.path, timeout=10, isolation_level=None)
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
            if row and row[0] < SCHEMA_VERSION:
                raise CatalogError(f"No migration path from catalog schema {row[0]}")
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
                    result TEXT, error TEXT, created_at TEXT NOT NULL, updated_at TEXT NOT NULL
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
                CREATE INDEX IF NOT EXISTS takes_project_idx ON takes(project_id, created_at);
                CREATE UNIQUE INDEX IF NOT EXISTS one_active_take_per_project_side
                    ON takes(project_id, side) WHERE state IN ('preparing','recording','finalizing');
                CREATE INDEX IF NOT EXISTS observations_project_idx ON observations(project_id, observed_at);
            """)
            if not row:
                db.execute("INSERT INTO schema_info(version) VALUES (?)", (SCHEMA_VERSION,))
        try:
            os.chmod(self.path, 0o600)
        except FileNotFoundError:
            pass

    @staticmethod
    def _now():
        from datetime import datetime, timezone
        return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")

    def _relative(self, locator: str) -> Path:
        candidate = Path(locator)
        if candidate.is_absolute() or any(part in ("", ".", "..") for part in candidate.parts):
            raise CatalogError("Locator must be a normalized relative path")
        target = self.library.joinpath(candidate)
        if target.resolve().parent != target.parent.resolve():
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
        digest = sha256_file(stage)
        with self.transaction() as db:
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
            os.fsync(dir_fd)
        finally:
            os.close(dir_fd)
        receipt = {"take_id": intent["take_id"], "project_id": intent["project_id"],
                   "side": intent["side"], "locator": intent["intended_locator"],
                   "sha256": digest, **info}
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
        return receipt

    def reconcile(self) -> list[dict]:
        """Resolve crash gaps by stable intent identity; never claim partial WAVs saved."""
        with self.connect() as db:
            rows = db.execute("SELECT * FROM intents WHERE state NOT IN ('committed','failed','quarantined','interrupted') ORDER BY created_at").fetchall()
        outcomes = []
        for row in rows:
            intent = dict(row)
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
