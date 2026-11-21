import json
import os
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone

SCHEMA = """
CREATE TABLE IF NOT EXISTS messages (
  id          INTEGER PRIMARY KEY AUTOINCREMENT,
  session_id  TEXT NOT NULL,
  role        TEXT NOT NULL,
  content     TEXT NOT NULL,
  metadata    TEXT,
  created_at  TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_messages_session ON messages(session_id, id);
CREATE TABLE IF NOT EXISTS sessions (
  session_id    TEXT PRIMARY KEY,
  system_prompt TEXT
);
"""


class Database:
    def __init__(self, path: str) -> None:
        self.path = path
        directory = os.path.dirname(path)
        if directory:
            os.makedirs(directory, exist_ok=True)
        with self._conn() as c:
            c.execute("PRAGMA journal_mode=WAL")
            c.executescript(SCHEMA)

    @contextmanager
    def _conn(self):
        conn = sqlite3.connect(self.path, timeout=10)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA synchronous=NORMAL")
        try:
            yield conn
            conn.commit()
        finally:
            conn.close()

    def ping(self) -> bool:
        with self._conn() as c:
            c.execute("SELECT 1")
        return True

    def session_exists(self, session_id: str) -> bool:
        with self._conn() as c:
            row = c.execute("SELECT 1 FROM messages WHERE session_id = ? LIMIT 1", (session_id,)).fetchone()
        return row is not None

    def get_system_prompt(self, session_id: str) -> str | None:
        with self._conn() as c:
            row = c.execute("SELECT system_prompt FROM sessions WHERE session_id = ?", (session_id,)).fetchone()
        return row["system_prompt"] if row else None

    def load_context(self, session_id: str) -> tuple[str | None, list[dict]]:
        """System prompt + history in ONE connection/thread hop (was two)."""
        with self._conn() as c:
            sp = c.execute("SELECT system_prompt FROM sessions WHERE session_id = ?", (session_id,)).fetchone()
            rows = c.execute(
                "SELECT role, content, metadata FROM messages WHERE session_id = ? ORDER BY id", (session_id,)
            ).fetchall()
        return (sp["system_prompt"] if sp else None), [self._row(r) for r in rows]

    @staticmethod
    def _row(r) -> dict:
        item = {"role": r["role"], "content": r["content"]}
        if r["metadata"]:
            item["metadata"] = json.loads(r["metadata"])
        return item

    def history(self, session_id: str) -> list[dict]:
        with self._conn() as c:
            rows = c.execute(
                "SELECT role, content, metadata FROM messages WHERE session_id = ? ORDER BY id", (session_id,)
            ).fetchall()
        return [self._row(r) for r in rows]

    def add_exchange(self, session_id: str, system_prompt: str | None, user_msg: str, reply: str, metadata: dict) -> None:
        """Persist user + assistant messages atomically (only called on success)."""
        now = lambda: datetime.now(timezone.utc).isoformat()
        with self._conn() as c:
            if system_prompt is not None:
                c.execute(
                    "INSERT INTO sessions(session_id, system_prompt) VALUES(?, ?) "
                    "ON CONFLICT(session_id) DO UPDATE SET system_prompt = excluded.system_prompt",
                    (session_id, system_prompt),
                )
            c.execute(
                "INSERT INTO messages(session_id, role, content, metadata, created_at) VALUES(?, 'user', ?, NULL, ?)",
                (session_id, user_msg, now()),
            )
            c.execute(
                "INSERT INTO messages(session_id, role, content, metadata, created_at) VALUES(?, 'assistant', ?, ?, ?)",
                (session_id, reply, json.dumps(metadata), now()),
            )
