"""Durable local request queue. A pending request is NOT an Apple Music Up Next item."""
import sqlite3
from datetime import datetime, timezone, timedelta
from pathlib import Path
from threading import RLock
from .models import Request, Track


def utcnow():
    return datetime.now(timezone.utc)


class QueueError(ValueError):
    pass


class QueueStore:
    def __init__(self, path: Path | str):
        self.path = str(path)
        self.lock = RLock()
        self.conn = sqlite3.connect(self.path, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        with self.lock:
            self.conn.execute('PRAGMA journal_mode=WAL')
            self.conn.execute('''CREATE TABLE IF NOT EXISTS song_requests (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                title TEXT NOT NULL, artist TEXT NOT NULL, album TEXT NOT NULL,
                duration_ms INTEGER NOT NULL, track_id TEXT NOT NULL, url TEXT NOT NULL,
                viewer_id TEXT NOT NULL, viewer_name TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'pending', created_at TEXT NOT NULL
            )''')
            self.conn.commit()

    def pending(self) -> list[Request]:
        with self.lock:
            rows = self.conn.execute("SELECT * FROM song_requests WHERE status='pending' ORDER BY id ASC").fetchall()
            return [Request(**dict(row)) for row in rows]

    def queued(self) -> list[Request]:
        with self.lock:
            rows = self.conn.execute("SELECT * FROM song_requests WHERE status='queued' ORDER BY id ASC").fetchall()
            return [Request(**dict(row)) for row in rows]

    def active(self) -> list[Request]:
        with self.lock:
            rows = self.conn.execute("SELECT * FROM song_requests WHERE status IN ('pending','queued') ORDER BY id ASC").fetchall()
            return [Request(**dict(row)) for row in rows]

    def mark_queued(self, request_id: int) -> bool:
        with self.lock:
            cur = self.conn.execute("UPDATE song_requests SET status='queued' WHERE id=? AND status='pending'", (request_id,))
            self.conn.commit()
            return cur.rowcount > 0

    def add(self, track: Track, viewer_id: str, viewer_name: str,
            max_queue=30, max_per_viewer=2, cooldown_seconds=90) -> tuple[int, int]:
        if not track.track_id or not track.title or not track.artist:
            raise QueueError('Song metadata is incomplete. Please try an Apple Music song link.')
        with self.lock:
            pending = self.active()
            if len(pending) >= max_queue:
                raise QueueError('The request queue is full right now.')
            if any(p.track_id == track.track_id for p in pending):
                raise QueueError('That song is already in the request queue.')
            if sum(p.viewer_id == viewer_id for p in pending) >= max_per_viewer:
                raise QueueError(f'You already have {max_per_viewer} pending requests.')
            if cooldown_seconds:
                last = self.conn.execute(
                    'SELECT created_at FROM song_requests WHERE viewer_id=? ORDER BY id DESC LIMIT 1',
                    (viewer_id,)
                ).fetchone()
                if last:
                    elapsed = (utcnow() - datetime.fromisoformat(last['created_at'])).total_seconds()
                    if elapsed < cooldown_seconds:
                        raise QueueError(f'Please wait {int(cooldown_seconds - elapsed) + 1}s before requesting again.')
            cursor = self.conn.execute('''INSERT INTO song_requests
                (title,artist,album,duration_ms,track_id,url,viewer_id,viewer_name,status,created_at)
                VALUES (?,?,?,?,?,?,?,?,?,?)''',
                (track.title,track.artist,track.album,track.duration_ms,track.track_id,track.url,
                 viewer_id,viewer_name,'pending',utcnow().isoformat()))
            self.conn.commit()
            return int(cursor.lastrowid), len(pending) + 1

    def change(self, request_id: int, status: str) -> bool:
        if status not in ('played', 'removed'):
            raise ValueError('invalid request status')
        with self.lock:
            cursor = self.conn.execute("UPDATE song_requests SET status=? WHERE id=? AND status IN ('pending','queued')",
                                       (status, request_id))
            self.conn.commit()
            return cursor.rowcount > 0

    def remove_own(self, viewer_id: str) -> bool:
        with self.lock:
            row = self.conn.execute("SELECT id FROM song_requests WHERE status='pending' AND viewer_id=? ORDER BY id DESC LIMIT 1",
                                    (viewer_id,)).fetchone()
            return self.change(row['id'], 'removed') if row else False

    def close(self):
        with self.lock:
            self.conn.close()
