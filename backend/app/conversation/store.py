"""SQLite-backed local conversation sessions, isolated from task state."""
from __future__ import annotations

import os
import sqlite3
from contextlib import AbstractContextManager, contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator
from uuid import UUID, uuid4


class ConversationStoreError(RuntimeError):
    """Safe storage-layer failure without request content or filesystem details."""


def default_database_path() -> Path:
    configured = os.getenv("UNBOUND_CONVERSATION_DB")
    if configured:
        return Path(configured).expanduser()
    if os.name == "nt":
        base = Path(os.getenv("LOCALAPPDATA", Path.home() / "AppData" / "Local"))
    else:
        base = Path(os.getenv("XDG_DATA_HOME", Path.home() / ".local" / "share"))
    return base / "UNBOUND_AI" / "conversations.sqlite3"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


class SQLiteConversationStore:
    """Small repository abstraction that can be replaced independently of tasks."""

    def __init__(self, database_path: str | Path | None = None) -> None:
        self.database_path = Path(database_path) if database_path is not None else default_database_path()

    def transaction(self) -> AbstractContextManager[sqlite3.Connection]:
        """Open a transaction on the shared local database for sibling repositories."""
        return self._connection()

    @contextmanager
    def _connection(self) -> Iterator[sqlite3.Connection]:
        connection: sqlite3.Connection | None = None
        try:
            self.database_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            connection = sqlite3.connect(self.database_path, timeout=10, isolation_level="IMMEDIATE")
            if os.name != "nt":
                os.chmod(self.database_path, 0o600)
            connection.row_factory = sqlite3.Row
            connection.execute("PRAGMA foreign_keys = ON")
            connection.execute("PRAGMA busy_timeout = 10000")
            connection.execute("PRAGMA secure_delete = ON")
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS conversations (
                    session_id TEXT PRIMARY KEY,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS conversation_messages (
                    message_id TEXT PRIMARY KEY,
                    session_id TEXT NOT NULL REFERENCES conversations(session_id) ON DELETE CASCADE,
                    role TEXT NOT NULL CHECK (role IN ('user', 'assistant')),
                    content TEXT NOT NULL CHECK (length(content) > 0),
                    created_at TEXT NOT NULL,
                    sequence INTEGER NOT NULL CHECK (sequence > 0),
                    request_id TEXT,
                    UNIQUE (session_id, sequence),
                    UNIQUE (session_id, request_id, role)
                );
                CREATE INDEX IF NOT EXISTS idx_conversation_messages_order
                    ON conversation_messages(session_id, sequence);
                CREATE TABLE IF NOT EXISTS conversation_tasks (
                    session_id TEXT NOT NULL REFERENCES conversations(session_id) ON DELETE CASCADE,
                    task_id TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    PRIMARY KEY (session_id, task_id)
                );
                CREATE INDEX IF NOT EXISTS idx_conversation_tasks_order
                    ON conversation_tasks(session_id, created_at, task_id);
                """
            )
            yield connection
            connection.commit()
        except (sqlite3.Error, OSError) as exc:
            if connection is not None:
                connection.rollback()
            raise ConversationStoreError("Conversation storage operation failed") from exc
        finally:
            if connection is not None:
                connection.close()

    @staticmethod
    def _message(row: sqlite3.Row) -> dict[str, object]:
        return {
            "message_id": row["message_id"],
            "session_id": row["session_id"],
            "role": row["role"],
            "content": row["content"],
            "created_at": row["created_at"],
            "sequence": row["sequence"],
        }

    @staticmethod
    def _session(row: sqlite3.Row, message_count: int = 0) -> dict[str, object]:
        return {
            "session_id": row["session_id"],
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
            "message_count": message_count,
        }

    def create_session(self) -> dict[str, object]:
        session_id, timestamp = str(uuid4()), _now()
        with self._connection() as db:
            db.execute(
                "INSERT INTO conversations(session_id, created_at, updated_at) VALUES (?, ?, ?)",
                (session_id, timestamp, timestamp),
            )
            row = db.execute("SELECT * FROM conversations WHERE session_id = ?", (session_id,)).fetchone()
            return self._session(row)

    def get_session(self, session_id: UUID | str) -> dict[str, object] | None:
        with self._connection() as db:
            row = db.execute("SELECT * FROM conversations WHERE session_id = ?", (str(session_id),)).fetchone()
            if row is None:
                return None
            count = db.execute(
                "SELECT COUNT(*) FROM conversation_messages WHERE session_id = ?", (str(session_id),)
            ).fetchone()[0]
            return self._session(row, count)

    def list_sessions(self) -> list[dict[str, object]]:
        with self._connection() as db:
            rows = db.execute(
                """SELECT c.*, COUNT(m.message_id) AS message_count
                   FROM conversations c LEFT JOIN conversation_messages m USING(session_id)
                   GROUP BY c.session_id ORDER BY c.updated_at DESC, c.session_id"""
            ).fetchall()
            return [self._session(row, row["message_count"]) for row in rows]

    def get_messages(self, session_id: UUID | str, limit: int | None = None) -> list[dict[str, object]]:
        with self._connection() as db:
            if limit is None:
                rows = db.execute(
                    "SELECT * FROM conversation_messages WHERE session_id = ? ORDER BY sequence",
                    (str(session_id),),
                ).fetchall()
            else:
                rows = db.execute(
                    """SELECT * FROM (
                         SELECT * FROM conversation_messages WHERE session_id = ?
                         ORDER BY sequence DESC LIMIT ?
                       ) ORDER BY sequence""",
                    (str(session_id), limit),
                ).fetchall()
            return [self._message(row) for row in rows]

    def find_exchange(self, session_id: UUID | str, request_id: UUID | str) -> list[dict[str, object]] | None:
        with self._connection() as db:
            rows = db.execute(
                """SELECT * FROM conversation_messages
                   WHERE session_id = ? AND request_id = ? ORDER BY sequence""",
                (str(session_id), str(request_id)),
            ).fetchall()
            if not rows:
                return None
            return [self._message(row) for row in rows]

    def append_exchange(
        self,
        session_id: UUID | str,
        request_id: UUID | str,
        user_content: str,
        assistant_content: str,
        task_id: UUID | str | None = None,
    ) -> list[dict[str, object]]:
        sid, rid = str(session_id), str(request_id)
        with self._connection() as db:
            # Lock before checking idempotency and allocating sequence numbers.
            db.execute("BEGIN IMMEDIATE")
            session = db.execute("SELECT 1 FROM conversations WHERE session_id = ?", (sid,)).fetchone()
            if session is None:
                raise ConversationStoreError("Conversation session does not exist")
            existing = db.execute(
                "SELECT * FROM conversation_messages WHERE session_id = ? AND request_id = ? ORDER BY sequence",
                (sid, rid),
            ).fetchall()
            if existing:
                existing_user = next((row["content"] for row in existing if row["role"] == "user"), None)
                if task_id is not None and existing_user == user_content:
                    db.execute(
                        "INSERT OR IGNORE INTO conversation_tasks(session_id, task_id, created_at) VALUES (?, ?, ?)",
                        (sid, str(task_id), _now()),
                    )
                return [self._message(row) for row in existing]
            sequence = db.execute(
                "SELECT COALESCE(MAX(sequence), 0) FROM conversation_messages WHERE session_id = ?", (sid,)
            ).fetchone()[0]
            timestamp = _now()
            for role, content in (("user", user_content), ("assistant", assistant_content)):
                sequence += 1
                db.execute(
                    """INSERT INTO conversation_messages
                       (message_id, session_id, role, content, created_at, sequence, request_id)
                       VALUES (?, ?, ?, ?, ?, ?, ?)""",
                    (str(uuid4()), sid, role, content, timestamp, sequence, rid),
                )
            db.execute("UPDATE conversations SET updated_at = ? WHERE session_id = ?", (timestamp, sid))
            if task_id is not None:
                db.execute(
                    "INSERT OR IGNORE INTO conversation_tasks(session_id, task_id, created_at) VALUES (?, ?, ?)",
                    (sid, str(task_id), timestamp),
                )
            rows = db.execute(
                "SELECT * FROM conversation_messages WHERE session_id = ? AND request_id = ? ORDER BY sequence",
                (sid, rid),
            ).fetchall()
            return [self._message(row) for row in rows]

    def delete_session(self, session_id: UUID | str) -> bool:
        with self._connection() as db:
            cursor = db.execute("DELETE FROM conversations WHERE session_id = ?", (str(session_id),))
            return cursor.rowcount > 0

    def link_task(self, session_id: UUID | str, task_id: UUID | str) -> None:
        """Persist a conversation-to-task reference; TaskManager owns task data."""
        with self._connection() as db:
            db.execute(
                "INSERT OR IGNORE INTO conversation_tasks(session_id, task_id, created_at) VALUES (?, ?, ?)",
                (str(session_id), str(task_id), _now()),
            )

    def get_task_ids(self, session_id: UUID | str) -> list[str]:
        with self._connection() as db:
            rows = db.execute(
                "SELECT task_id FROM conversation_tasks WHERE session_id = ? ORDER BY created_at, task_id",
                (str(session_id),),
            ).fetchall()
            return [str(row["task_id"]) for row in rows]
