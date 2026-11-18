"""Persistent, conservative scrobble submission receipts."""
import sqlite3
from contextlib import closing
from pathlib import Path
import json
import time


class ReceiptStore:
    def __init__(self, path: Path):
        self.path = path
        with closing(self.connect()) as connection, connection:
            connection.execute("""
                CREATE TABLE IF NOT EXISTS receipt (
                    service TEXT NOT NULL,
                    track TEXT NOT NULL,
                    started_at INTEGER NOT NULL,
                    state TEXT NOT NULL CHECK (state IN ('pending', 'accepted')),
                    PRIMARY KEY (service, track, started_at)
                )
            """)
            connection.execute("""
                CREATE TABLE IF NOT EXISTS submission_receipt (
                    service TEXT NOT NULL, account_hash TEXT NOT NULL,
                    track TEXT NOT NULL, started_at INTEGER NOT NULL,
                    state TEXT NOT NULL CHECK (state IN ('pending', 'accepted')),
                    PRIMARY KEY (service, account_hash, track, started_at)
                )
            """)
            connection.execute("INSERT OR IGNORE INTO submission_receipt "
                               "SELECT service,'',track,started_at,state FROM receipt")
            queue_exists = connection.execute("SELECT 1 FROM sqlite_master WHERE type='table' "
                                              "AND name='listen_queue'").fetchone()
            if queue_exists:
                primary_key = tuple(row['name'] for row in connection.execute(
                    "PRAGMA table_info(listen_queue)") if row['pk'])
                if primary_key != ('play_id', 'account_hash'):
                    connection.execute("ALTER TABLE listen_queue RENAME TO listen_queue_legacy_migration")
            connection.execute("""
                CREATE TABLE IF NOT EXISTS listen_queue (
                    play_id TEXT NOT NULL, account_hash TEXT NOT NULL,
                    track TEXT NOT NULL, artist TEXT NOT NULL, title TEXT NOT NULL,
                    album TEXT, listened_at INTEGER NOT NULL, eligible_at INTEGER NOT NULL,
                    expires_at INTEGER NOT NULL,
                    state TEXT NOT NULL CHECK (state IN ('queued','pending','accepted','expired')),
                    updated_at INTEGER NOT NULL,
                    PRIMARY KEY (play_id, account_hash)
                )
            """)
            legacy_queue = connection.execute("SELECT 1 FROM sqlite_master WHERE type='table' "
                                              "AND name='listen_queue_legacy_migration'").fetchone()
            if legacy_queue:
                connection.execute("INSERT OR IGNORE INTO listen_queue "
                                   "SELECT play_id,account_hash,track,artist,title,album,listened_at,eligible_at,"
                                   "expires_at,state,updated_at FROM listen_queue_legacy_migration")
                connection.execute("DROP TABLE listen_queue_legacy_migration")
            connection.execute("CREATE INDEX IF NOT EXISTS listen_queue_account_state_idx "
                               "ON listen_queue(account_hash,state,eligible_at)")

    def connect(self):
        connection = sqlite3.connect(self.path, timeout=10)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA busy_timeout=10000")
        return connection

    def reserve(self, service, track, started_at, *, account_hash=""):
        """Return True once. Keep uncertain submissions pending across restarts."""
        with closing(self.connect()) as connection, connection:
            if account_hash and (
                    connection.execute("SELECT 1 FROM receipt WHERE service=? AND track=? AND started_at=?",
                                       (service, track, started_at)).fetchone()
                    or connection.execute("SELECT 1 FROM submission_receipt WHERE service=? AND account_hash='' "
                                          "AND track=? AND started_at=?", (service, track, started_at)).fetchone()):
                return False
            result = connection.execute("INSERT INTO submission_receipt VALUES (?,?,?,?,'pending') "
                                        "ON CONFLICT (service,account_hash,track,started_at) DO NOTHING",
                                        (service, account_hash, track, started_at))
            return result.rowcount == 1

    def mark_accepted(self, service, track, started_at, *, account_hash=""):
        with closing(self.connect()) as connection, connection:
            result = connection.execute("UPDATE submission_receipt SET state='accepted' "
                                        "WHERE service=? AND account_hash=? AND track=? AND started_at=? "
                                        "AND state='pending'", (service, account_hash, track, started_at))
            connection.execute("UPDATE receipt SET state='accepted' WHERE service=? AND track=? "
                               "AND started_at=? AND state='pending'", (service, track, started_at))
            return result.rowcount == 1

    def queue_listen(self, *, play_id, account_hash, track, artist, title, album,
                     listened_at, eligible_at, expires_at, max_items=100):
        """Persist an opted-in, definitely-not-yet-attempted listen.

        Payload text is the recognized play identity, not audio or a credential.
        Queue rows are never replayed after moving to ``pending``.
        """
        if not all(isinstance(v, str) and v and len(v) <= 500 for v in
                   (play_id, account_hash, track, artist, title)):
            raise ValueError("Invalid offline listen identity")
        if album is not None and (not isinstance(album, str) or len(album) > 500):
            raise ValueError("Invalid offline listen album")
        now = int(time.time())
        if (type(listened_at) is not int or type(eligible_at) is not int
                or type(expires_at) is not int or expires_at <= eligible_at
                or expires_at - eligible_at > 7 * 86400):
            raise ValueError("Offline listen expiry must be within seven days")
        with closing(self.connect()) as db:
            db.execute("BEGIN IMMEDIATE")
            db.execute("UPDATE listen_queue SET state='expired',updated_at=? "
                       "WHERE state='queued' AND expires_at<=?", (now, now))
            existing = db.execute("SELECT state FROM listen_queue WHERE play_id=? AND account_hash=?",
                                  (play_id, account_hash)).fetchone()
            if existing:
                db.commit()
                return existing[0] == "queued"
            count = db.execute("SELECT count(*) FROM listen_queue WHERE account_hash=? "
                               "AND state IN ('queued','pending')", (account_hash,)).fetchone()[0]
            if count >= max_items:
                db.rollback()
                raise ValueError("Offline ListenBrainz queue is full; remove queued items first")
            old_receipt = db.execute("SELECT state FROM receipt WHERE service='listenbrainz' "
                                     "AND track=? AND started_at=?", (track, listened_at)).fetchone()
            scoped_receipt = db.execute("SELECT state FROM submission_receipt WHERE service='listenbrainz' "
                                        "AND account_hash IN (?, '') AND track=? AND started_at=? LIMIT 1",
                                        (account_hash, track, listened_at)).fetchone()
            if old_receipt or scoped_receipt:
                db.rollback()
                return False
            db.execute("INSERT INTO listen_queue VALUES(?,?,?,?,?,?,?,?,?,'queued',?)",
                       (play_id, account_hash, track, artist, title, album, listened_at,
                        eligible_at, expires_at, now))
            db.commit()
        return True

    def queued_listens(self, account_hash, *, limit=25):
        if not isinstance(account_hash, str) or not account_hash:
            return []
        with closing(self.connect()) as db, db:
            db.execute("UPDATE listen_queue SET state='expired',updated_at=? "
                       "WHERE account_hash=? AND state='queued' AND expires_at<=?",
                       (int(time.time()), account_hash, int(time.time())))
            rows = db.execute("SELECT play_id,track,artist,title,album,listened_at,eligible_at,expires_at,state "
                              "FROM listen_queue WHERE account_hash=? AND state IN ('queued','pending') "
                              "ORDER BY eligible_at LIMIT ?", (account_hash, limit)).fetchall()
        return [dict(row) for row in rows]

    def claim_queued_listen(self, play_id, account_hash):
        """Atomically move a known-unsent row to held/pending before network I/O."""
        with closing(self.connect()) as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT * FROM listen_queue WHERE play_id=? AND account_hash=?",
                             (play_id, account_hash)).fetchone()
            if row is None or row["state"] != "queued" or row["expires_at"] <= int(time.time()):
                db.rollback()
                return None
            old = db.execute("SELECT state FROM submission_receipt WHERE service='listenbrainz' "
                             "AND account_hash IN (?, '') AND track=? AND started_at=? LIMIT 1",
                             (account_hash, row["track"], row["listened_at"])).fetchone()
            legacy = db.execute("SELECT state FROM receipt WHERE service='listenbrainz' AND track=? AND started_at=?",
                                (row["track"], row["listened_at"])).fetchone()
            if old or legacy:
                db.execute("UPDATE listen_queue SET state='pending',updated_at=? WHERE play_id=? AND account_hash=?",
                           (int(time.time()), play_id, account_hash))
                db.commit()
                return None
            try:
                db.execute("INSERT INTO submission_receipt VALUES ('listenbrainz',?,?,?,'pending')",
                           (account_hash, row["track"], row["listened_at"]))
            except sqlite3.IntegrityError:
                db.rollback()
                return None
            db.execute("UPDATE listen_queue SET state='pending',updated_at=? "
                       "WHERE play_id=? AND account_hash=? AND state='queued'",
                       (int(time.time()), play_id, account_hash))
            db.commit()
            return dict(row)

    def update_queued_metadata(self, play_id, account_hash, *, track, artist, title, album):
        if (not all(isinstance(value, str) and value and len(value) <= 500
                    for value in (track, artist, title))
                or (album is not None and (not isinstance(album, str) or len(album) > 500))):
            return False
        with closing(self.connect()) as db, db:
            result = db.execute("UPDATE listen_queue SET track=?,artist=?,title=?,album=?,updated_at=? "
                                "WHERE play_id=? AND account_hash=? AND state='queued'",
                                (track, artist, title, album, int(time.time()), play_id, account_hash))
            return result.rowcount == 1

    def accept_queued_listen(self, play_id, account_hash):
        with closing(self.connect()) as db, db:
            item = db.execute("SELECT track,listened_at FROM listen_queue WHERE play_id=? AND account_hash=?",
                              (play_id, account_hash)).fetchone()
            if item:
                db.execute("UPDATE listen_queue SET state='accepted',updated_at=? "
                           "WHERE play_id=? AND account_hash=? AND state='pending'",
                           (int(time.time()), play_id, account_hash))
                db.execute("UPDATE submission_receipt SET state='accepted' "
                           "WHERE service='listenbrainz' AND account_hash=? AND track=? AND started_at=? "
                           "AND state='pending'", (account_hash, item['track'], item['listened_at']))

    def delete_queued_listen(self, play_id, account_hash):
        """Delete only definitely-unsent queue work; retain ambiguous send evidence."""
        with closing(self.connect()) as db, db:
            result = db.execute("DELETE FROM listen_queue WHERE play_id=? AND account_hash=? "
                                "AND state='queued'",
                                (play_id, account_hash))
            return result.rowcount == 1
