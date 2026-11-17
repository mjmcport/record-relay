"""Persistent, conservative scrobble submission receipts."""
import sqlite3
from contextlib import closing
from pathlib import Path


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

    def connect(self):
        return sqlite3.connect(self.path)

    def reserve(self, service, track, started_at):
        """Return True once. Keep uncertain submissions pending across restarts."""
        with closing(self.connect()) as connection, connection:
            result = connection.execute("""
                INSERT INTO receipt VALUES (?, ?, ?, 'pending')
                ON CONFLICT (service, track, started_at) DO NOTHING
            """, (service, track, started_at))
            return result.rowcount == 1

    def mark_accepted(self, service, track, started_at):
        with closing(self.connect()) as connection, connection:
            result = connection.execute("""
                UPDATE receipt SET state = 'accepted'
                WHERE service = ? AND track = ? AND started_at = ?
                  AND state = 'pending'
            """, (service, track, started_at))
            return result.rowcount == 1
