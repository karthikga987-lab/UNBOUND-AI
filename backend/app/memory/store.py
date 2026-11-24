"""SQLite repository for approved memories and reviewable candidates."""
from __future__ import annotations

import hashlib
import re
import sqlite3
import unicodedata
from datetime import datetime, timezone
from typing import Any
from uuid import UUID, uuid4

from backend.app.conversation.store import ConversationStoreError, SQLiteConversationStore


class MemoryStoreError(RuntimeError):
    pass


class MemoryNotFoundError(MemoryStoreError):
    pass


class MemoryConflictError(MemoryStoreError):
    pass


class MemorySourceError(MemoryStoreError):
    pass


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _fingerprint(content: str) -> str:
    normalized = unicodedata.normalize("NFKC", content).casefold()
    normalized = " ".join(normalized.split())
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()


class SQLiteMemoryStore:
    """Memory data shares the configured SQLite file but uses separate tables."""

    def __init__(self, database: SQLiteConversationStore) -> None:
        self.database = database

    @staticmethod
    def _ensure_schema(db: sqlite3.Connection) -> None:
        db.executescript(
            """
            CREATE TABLE IF NOT EXISTS personal_memories (
                memory_id TEXT PRIMARY KEY,
                fingerprint TEXT NOT NULL UNIQUE,
                content TEXT NOT NULL CHECK(length(content) BETWEEN 1 AND 1200),
                category TEXT NOT NULL CHECK(category IN
                    ('fact','preference','project','recurring_context','instruction','other')),
                status TEXT NOT NULL CHECK(status IN ('pending_approval','approved')),
                origin TEXT NOT NULL CHECK(origin IN ('user_provided','model_extracted','user_edited')),
                source_session_id TEXT REFERENCES conversations(session_id) ON DELETE SET NULL,
                source_message_id TEXT REFERENCES conversation_messages(message_id) ON DELETE SET NULL,
                user_confirmed INTEGER NOT NULL CHECK(user_confirmed IN (0,1)),
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                approved_at TEXT
            );
            CREATE INDEX IF NOT EXISTS idx_personal_memories_status_updated
                ON personal_memories(status, updated_at DESC);
            CREATE TABLE IF NOT EXISTS memory_suppressions (
                fingerprint TEXT PRIMARY KEY,
                suppressed_at TEXT NOT NULL,
                source_message_id TEXT
            );
            CREATE TABLE IF NOT EXISTS memory_settings (
                setting_key TEXT PRIMARY KEY CHECK(setting_key = 'retrieval_enabled'),
                setting_value INTEGER NOT NULL CHECK(setting_value IN (0,1)),
                updated_at TEXT NOT NULL
            );
            """
        )
        suppression_columns = {
            row["name"] for row in db.execute("PRAGMA table_info(memory_suppressions)").fetchall()
        }
        if "source_message_id" not in suppression_columns:
            db.execute("ALTER TABLE memory_suppressions ADD COLUMN source_message_id TEXT")
        db.execute(
            "CREATE INDEX IF NOT EXISTS idx_memory_suppressions_source ON memory_suppressions(source_message_id)"
        )

    @staticmethod
    def _record(db: sqlite3.Connection, row: sqlite3.Row) -> dict[str, Any]:
        source = None
        if row["source_message_id"]:
            source = db.execute(
                "SELECT content FROM conversation_messages WHERE message_id = ?",
                (row["source_message_id"],),
            ).fetchone()
        excerpt = source["content"][:500] if source else None
        return {
            "memory_id": row["memory_id"],
            "content": row["content"],
            "category": row["category"],
            "status": row["status"],
            "origin": row["origin"],
            "source_session_id": row["source_session_id"],
            "source_message_id": row["source_message_id"],
            "source_excerpt": excerpt,
            "user_confirmed": bool(row["user_confirmed"]),
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
            "approved_at": row["approved_at"],
        }

    @staticmethod
    def _select(db: sqlite3.Connection, memory_id: UUID | str) -> sqlite3.Row | None:
        return db.execute("SELECT * FROM personal_memories WHERE memory_id = ?", (str(memory_id),)).fetchone()

    def create_candidate(
        self,
        *,
        content: str,
        category: str,
        origin: str,
        source_session_id: UUID | str | None = None,
        source_message_id: UUID | str | None = None,
        explicitly_requested: bool = False,
    ) -> dict[str, Any]:
        fingerprint = _fingerprint(content)
        now = _now()
        try:
            with self.database.transaction() as db:
                self._ensure_schema(db)
                db.execute("BEGIN IMMEDIATE")
                if source_session_id is not None:
                    exists = db.execute("SELECT 1 FROM conversations WHERE session_id = ?", (str(source_session_id),)).fetchone()
                    if not exists:
                        raise MemorySourceError("Source conversation not found")
                if source_message_id is not None:
                    source = db.execute(
                        "SELECT session_id, role FROM conversation_messages WHERE message_id = ?",
                        (str(source_message_id),),
                    ).fetchone()
                    if source is None or source["session_id"] != str(source_session_id) or source["role"] != "user":
                        raise MemorySourceError("Source must be a user message from the selected conversation")

                if source_message_id is not None and not explicitly_requested:
                    suppressed = db.execute(
                        "SELECT 1 FROM memory_suppressions WHERE fingerprint = ? OR source_message_id = ?",
                        (fingerprint, str(source_message_id)),
                    ).fetchone()
                else:
                    suppressed = db.execute(
                        "SELECT 1 FROM memory_suppressions WHERE fingerprint = ?", (fingerprint,)
                    ).fetchone()
                if suppressed and not explicitly_requested:
                    raise MemoryConflictError("This memory was previously rejected or deleted")
                if suppressed:
                    # A direct explicit request can recreate the exact item,
                    # but retain source-message suppression so old history does
                    # not generate paraphrased candidates later.
                    db.execute(
                        "DELETE FROM memory_suppressions WHERE fingerprint = ? AND source_message_id IS NULL",
                        (fingerprint,),
                    )

                existing = db.execute(
                    "SELECT * FROM personal_memories WHERE fingerprint = ?", (fingerprint,)
                ).fetchone()
                if existing:
                    if explicitly_requested and existing["status"] == "pending_approval":
                        db.execute(
                            """UPDATE personal_memories SET status='approved', user_confirmed=1,
                               approved_at=?, updated_at=? WHERE memory_id=?""",
                            (now, now, existing["memory_id"]),
                        )
                        existing = self._select(db, existing["memory_id"])
                    return self._record(db, existing)

                memory_id = str(uuid4())
                status = "approved" if explicitly_requested else "pending_approval"
                db.execute(
                    """INSERT INTO personal_memories
                       (memory_id, fingerprint, content, category, status, origin,
                        source_session_id, source_message_id, user_confirmed,
                        created_at, updated_at, approved_at)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (
                        memory_id, fingerprint, content, category, status, origin,
                        str(source_session_id) if source_session_id else None,
                        str(source_message_id) if source_message_id else None,
                        int(explicitly_requested), now, now, now if explicitly_requested else None,
                    ),
                )
                return self._record(db, self._select(db, memory_id))
        except (sqlite3.Error, OSError, ConversationStoreError) as exc:
            raise MemoryStoreError("Personal memory storage operation failed") from exc

    def get_memory(self, memory_id: UUID | str) -> dict[str, Any]:
        try:
            with self.database.transaction() as db:
                self._ensure_schema(db)
                row = self._select(db, memory_id)
                if row is None:
                    raise MemoryNotFoundError("Memory not found")
                return self._record(db, row)
        except (sqlite3.Error, OSError, ConversationStoreError) as exc:
            raise MemoryStoreError("Personal memory storage operation failed") from exc

    def list_memories(self, status: str | None = None) -> list[dict[str, Any]]:
        try:
            with self.database.transaction() as db:
                self._ensure_schema(db)
                if status:
                    rows = db.execute(
                        "SELECT * FROM personal_memories WHERE status = ? ORDER BY updated_at DESC, memory_id",
                        (status,),
                    ).fetchall()
                else:
                    rows = db.execute(
                        "SELECT * FROM personal_memories ORDER BY updated_at DESC, memory_id"
                    ).fetchall()
                return [self._record(db, row) for row in rows]
        except (sqlite3.Error, OSError, ConversationStoreError) as exc:
            raise MemoryStoreError("Personal memory storage operation failed") from exc

    def approve(self, memory_id: UUID | str) -> dict[str, Any]:
        now = _now()
        try:
            with self.database.transaction() as db:
                self._ensure_schema(db)
                db.execute("BEGIN IMMEDIATE")
                row = self._select(db, memory_id)
                if row is None:
                    raise MemoryNotFoundError("Memory not found")
                if row["status"] != "pending_approval":
                    raise MemoryConflictError("Memory is not awaiting approval")
                db.execute(
                    "UPDATE personal_memories SET status='approved', user_confirmed=1, approved_at=?, updated_at=? WHERE memory_id=?",
                    (now, now, str(memory_id)),
                )
                return self._record(db, self._select(db, memory_id))
        except (sqlite3.Error, OSError, ConversationStoreError) as exc:
            raise MemoryStoreError("Personal memory storage operation failed") from exc

    def reject(self, memory_id: UUID | str) -> str:
        now = _now()
        try:
            with self.database.transaction() as db:
                self._ensure_schema(db)
                db.execute("BEGIN IMMEDIATE")
                row = self._select(db, memory_id)
                if row is None:
                    raise MemoryNotFoundError("Memory not found")
                if row["status"] != "pending_approval":
                    raise MemoryConflictError("Only a pending candidate can be rejected")
                db.execute(
                    "INSERT OR REPLACE INTO memory_suppressions(fingerprint, suppressed_at, source_message_id) VALUES (?, ?, ?)",
                    (row["fingerprint"], now, row["source_message_id"]),
                )
                db.execute("DELETE FROM personal_memories WHERE memory_id = ?", (str(memory_id),))
                return now
        except (sqlite3.Error, OSError, ConversationStoreError) as exc:
            raise MemoryStoreError("Personal memory storage operation failed") from exc

    def update_memory(
        self, memory_id: UUID | str, *, content: str | None = None, category: str | None = None
    ) -> dict[str, Any]:
        now = _now()
        try:
            with self.database.transaction() as db:
                self._ensure_schema(db)
                db.execute("BEGIN IMMEDIATE")
                row = self._select(db, memory_id)
                if row is None:
                    raise MemoryNotFoundError("Memory not found")
                if row["status"] != "approved" or not row["user_confirmed"]:
                    raise MemoryConflictError("Only an approved memory can be edited")
                new_content = content if content is not None else row["content"]
                new_category = category if category is not None else row["category"]
                new_fingerprint = _fingerprint(new_content)
                duplicate = db.execute(
                    "SELECT memory_id FROM personal_memories WHERE fingerprint=? AND memory_id<>?",
                    (new_fingerprint, str(memory_id)),
                ).fetchone()
                if duplicate:
                    raise MemoryConflictError("An identical memory already exists")
                if new_fingerprint != row["fingerprint"]:
                    db.execute(
                        "INSERT OR REPLACE INTO memory_suppressions(fingerprint, suppressed_at, source_message_id) VALUES (?, ?, ?)",
                        (row["fingerprint"], now, row["source_message_id"]),
                    )
                    db.execute("DELETE FROM memory_suppressions WHERE fingerprint = ?", (new_fingerprint,))
                source_session_id = row["source_session_id"]
                source_message_id = row["source_message_id"]
                if content is not None and content != row["content"]:
                    # The original transcript excerpt no longer supports the
                    # corrected text; do not present stale provenance as proof.
                    source_session_id = None
                    source_message_id = None
                db.execute(
                    """UPDATE personal_memories SET fingerprint=?, content=?, category=?, origin='user_edited',
                       source_session_id=?, source_message_id=?, updated_at=? WHERE memory_id=?""",
                    (new_fingerprint, new_content, new_category, source_session_id, source_message_id, now, str(memory_id)),
                )
                return self._record(db, self._select(db, memory_id))
        except (sqlite3.Error, OSError, ConversationStoreError) as exc:
            raise MemoryStoreError("Personal memory storage operation failed") from exc

    def delete_memory(self, memory_id: UUID | str) -> None:
        now = _now()
        try:
            with self.database.transaction() as db:
                self._ensure_schema(db)
                db.execute("BEGIN IMMEDIATE")
                row = self._select(db, memory_id)
                if row is None:
                    raise MemoryNotFoundError("Memory not found")
                db.execute(
                    "INSERT OR REPLACE INTO memory_suppressions(fingerprint, suppressed_at, source_message_id) VALUES (?, ?, ?)",
                    (row["fingerprint"], now, row["source_message_id"]),
                )
                db.execute("DELETE FROM personal_memories WHERE memory_id = ?", (str(memory_id),))
        except (sqlite3.Error, OSError, ConversationStoreError) as exc:
            raise MemoryStoreError("Personal memory storage operation failed") from exc

    def retrieval_enabled(self) -> bool:
        try:
            with self.database.transaction() as db:
                self._ensure_schema(db)
                row = db.execute("SELECT setting_value FROM memory_settings WHERE setting_key='retrieval_enabled'").fetchone()
                return True if row is None else bool(row["setting_value"])
        except (sqlite3.Error, OSError, ConversationStoreError) as exc:
            raise MemoryStoreError("Personal memory storage operation failed") from exc

    def set_retrieval_enabled(self, enabled: bool) -> None:
        try:
            with self.database.transaction() as db:
                self._ensure_schema(db)
                db.execute("BEGIN IMMEDIATE")
                db.execute(
                    """INSERT INTO memory_settings(setting_key, setting_value, updated_at)
                       VALUES ('retrieval_enabled', ?, ?)
                       ON CONFLICT(setting_key) DO UPDATE SET setting_value=excluded.setting_value, updated_at=excluded.updated_at""",
                    (int(enabled), _now()),
                )
        except (sqlite3.Error, OSError, ConversationStoreError) as exc:
            raise MemoryStoreError("Personal memory storage operation failed") from exc

    @staticmethod
    def _tokens(text: str) -> set[str]:
        ignored = {"about", "after", "again", "also", "and", "are", "can", "could", "for", "from", "have", "into", "just", "like", "more", "most", "please", "that", "the", "their", "there", "this", "those", "what", "when", "where", "which", "with", "would", "your", "you", "remember", "tell", "me"}
        return {token for token in re.findall(r"\w+", text.casefold()) if len(token) > 2 and token not in ignored}

    def retrieve_relevant(self, query: str, *, limit: int = 5) -> list[dict[str, Any]]:
        query_tokens = self._tokens(query)
        if not query_tokens:
            return []
        try:
            with self.database.transaction() as db:
                self._ensure_schema(db)
                db.execute("BEGIN")
                setting = db.execute(
                    "SELECT setting_value FROM memory_settings WHERE setting_key='retrieval_enabled'"
                ).fetchone()
                if setting is not None and not bool(setting["setting_value"]):
                    return []
                rows = db.execute(
                    """SELECT * FROM personal_memories
                       WHERE status='approved' AND user_confirmed=1
                       ORDER BY updated_at DESC LIMIT 500"""
                ).fetchall()
                ranked = []
                for row in rows:
                    overlap = query_tokens.intersection(self._tokens(row["content"]))
                    if overlap:
                        ranked.append((len(overlap), row["updated_at"], row))
                ranked.sort(key=lambda item: (item[0], item[1]), reverse=True)
                return [
                    {
                        "memory_id": row["memory_id"],
                        "content": row["content"],
                        "category": row["category"],
                        "source_session_id": row["source_session_id"],
                        "source_message_id": row["source_message_id"],
                    }
                    for _, _, row in ranked[:limit]
                ]
        except (sqlite3.Error, OSError, ConversationStoreError) as exc:
            raise MemoryStoreError("Personal memory storage operation failed") from exc
