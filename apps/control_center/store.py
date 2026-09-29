"""Private transactional command queue; independent of existing paper ledgers."""
from __future__ import annotations
from contextlib import contextmanager
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import sqlite3

ACTIONS = frozenset({"pause", "resume", "flatten"})

def utc(): return datetime.now(timezone.utc).isoformat()

class Store:
    def __init__(self, path: Path):
        self.path = path
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        with self.db() as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS setting (name TEXT PRIMARY KEY, value TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS command (id TEXT PRIMARY KEY, action TEXT NOT NULL,
                  created TEXT NOT NULL, status TEXT NOT NULL, detail TEXT);
                CREATE TABLE IF NOT EXISTS event (id INTEGER PRIMARY KEY, at TEXT NOT NULL,
                  kind TEXT NOT NULL, payload TEXT NOT NULL);
            """)
            db.execute("INSERT OR IGNORE INTO setting VALUES ('paused','true')")
        path.chmod(0o600)

    @contextmanager
    def db(self):
        db = sqlite3.connect(self.path, timeout=5)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA journal_mode=DELETE")
        db.execute("PRAGMA synchronous=FULL")
        try:
            with db: yield db
        finally: db.close()

    def get(self, name, default=None):
        with self.db() as db:
            row = db.execute("SELECT value FROM setting WHERE name=?", (name,)).fetchone()
            return json.loads(row[0]) if row else default

    def put(self, name, value):
        with self.db() as db:
            db.execute("INSERT INTO setting VALUES (?,?) ON CONFLICT(name) DO UPDATE SET value=excluded.value",
                       (name, json.dumps(value, allow_nan=False)))

    def enqueue(self, identifier, action):
        if action not in ACTIONS:
            raise ValueError("unknown action")
        with self.db() as db:
            db.execute("BEGIN IMMEDIATE")
            old = db.execute("SELECT * FROM command WHERE id=?", (identifier,)).fetchone()
            if old:
                if old["action"] != action: raise ValueError("idempotency conflict")
                return dict(old)
            db.execute("INSERT INTO command VALUES (?,?,?,'queued',NULL)", (identifier, action, utc()))
            return dict(db.execute("SELECT * FROM command WHERE id=?", (identifier,)).fetchone())

    def pending(self):
        with self.db() as db:
            return [dict(x) for x in db.execute("SELECT * FROM command WHERE status='queued' ORDER BY created,id")]

    def finish(self, identifier, status, detail):
        with self.db() as db:
            db.execute("UPDATE command SET status=?,detail=? WHERE id=?", (status, detail, identifier))

    def history(self):
        with self.db() as db:
            return [dict(x) for x in db.execute("SELECT * FROM command ORDER BY created DESC LIMIT 20")]

    def event(self, kind, payload):
        with self.db() as db:
            db.execute("INSERT INTO event(at,kind,payload) VALUES (?,?,?)",
                       (utc(), kind, json.dumps(payload, allow_nan=False)))
            db.execute("DELETE FROM event WHERE id NOT IN (SELECT id FROM event ORDER BY id DESC LIMIT 2000)")
