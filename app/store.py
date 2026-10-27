"""Original-message storage and transactional SQLite retrieval indexes."""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import threading
import uuid
from datetime import datetime, timezone

from .retriever import HybridRetriever, character_terms, explicit_clues, lexical_terms


class RequestConflictError(Exception):
    """An idempotency key was reused with a different payload."""


class SQLiteMemoryStore:
    def __init__(self, db_path: str, **retrieval_options) -> None:
        self._lock = threading.RLock()
        if db_path != ":memory:":
            parent = os.path.dirname(db_path)
            if parent:
                os.makedirs(parent, exist_ok=True)
        self._conn = sqlite3.connect(db_path, check_same_thread=False, timeout=30)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA foreign_keys = ON")
        self._initialize()
        self._retriever = HybridRetriever(self._conn, **retrieval_options)

    def _initialize(self) -> None:
        # Migrate before creating indexes referring to new columns. Original
        # IDs, raw content and request hashes survive upgrades unchanged.
        with self._conn:
            self._conn.execute("""CREATE TABLE IF NOT EXISTS request_ledger (
                request_id TEXT PRIMARY KEY, payload_hash TEXT NOT NULL,
                created_at TEXT NOT NULL)""")
            self._conn.execute("""CREATE TABLE IF NOT EXISTS memories (
                id TEXT PRIMARY KEY, user_id TEXT NOT NULL, session_id TEXT NOT NULL,
                role TEXT NOT NULL, content TEXT NOT NULL, created_at TEXT NOT NULL,
                original_timestamp INTEGER, message_index INTEGER NOT NULL DEFAULT 0)""")
            columns = {row["name"] for row in self._conn.execute("PRAGMA table_info(memories)")}
            if "original_timestamp" not in columns:
                self._conn.execute("ALTER TABLE memories ADD COLUMN original_timestamp INTEGER")
            if "message_index" not in columns:
                self._conn.execute("ALTER TABLE memories ADD COLUMN message_index INTEGER NOT NULL DEFAULT 0")
                # Old records have no source sequence; rowid preserves their
                # insertion order, without pretending it is an event timestamp.
                counters: dict[tuple[str, str], int] = {}
                for row in self._conn.execute(
                    "SELECT id, user_id, session_id FROM memories ORDER BY rowid"
                ).fetchall():
                    key = (row["user_id"], row["session_id"])
                    position = counters.get(key, 0)
                    self._conn.execute("UPDATE memories SET message_index=? WHERE id=?", (position, row["id"]))
                    counters[key] = position + 1
            self._conn.execute("CREATE INDEX IF NOT EXISTS idx_memories_user ON memories(user_id)")
            self._conn.execute("CREATE INDEX IF NOT EXISTS idx_memory_session ON memories(user_id, session_id, message_index)")
            self._conn.execute("CREATE INDEX IF NOT EXISTS idx_memory_time ON memories(user_id, original_timestamp)")
            self._conn.execute("CREATE VIRTUAL TABLE IF NOT EXISTS memory_lexical USING fts5(terms)")
            self._conn.execute("CREATE VIRTUAL TABLE IF NOT EXISTS memory_character USING fts5(terms)")
            self._conn.execute("""CREATE TABLE IF NOT EXISTS memory_clues (
                memory_id TEXT NOT NULL REFERENCES memories(id) ON DELETE CASCADE,
                clue TEXT NOT NULL, PRIMARY KEY(memory_id, clue))""")
            self._conn.execute("CREATE INDEX IF NOT EXISTS idx_memory_clue ON memory_clues(clue)")
            self._conn.execute("""CREATE TRIGGER IF NOT EXISTS delete_memory_indexes
                AFTER DELETE ON memories BEGIN
                  DELETE FROM memory_lexical WHERE rowid=old.rowid;
                  DELETE FROM memory_character WHERE rowid=old.rowid;
                  DELETE FROM memory_clues WHERE memory_id=old.id;
                END""")
            self._conn.execute("CREATE TABLE IF NOT EXISTS index_metadata (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
            version = self._conn.execute("SELECT value FROM index_metadata WHERE key='version'").fetchone()
            if version is None or version[0] != "1":
                self._conn.execute("DELETE FROM memory_lexical")
                self._conn.execute("DELETE FROM memory_character")
                self._conn.execute("DELETE FROM memory_clues")
                for row in self._conn.execute("SELECT rowid AS record_rowid, id, content FROM memories").fetchall():
                    self._index(row["record_rowid"], row["id"], row["content"])
                self._conn.execute("INSERT OR REPLACE INTO index_metadata VALUES ('version','1')")

    def _index(self, rowid: int, memory_id: str, content: str) -> None:
        self._conn.execute("INSERT INTO memory_lexical(rowid, terms) VALUES (?,?)", (rowid, " ".join(lexical_terms(content))))
        self._conn.execute("INSERT INTO memory_character(rowid, terms) VALUES (?,?)", (rowid, " ".join(character_terms(content))))
        self._conn.executemany("INSERT INTO memory_clues VALUES (?,?)", [(memory_id, clue) for clue in explicit_clues(content)])

    @staticmethod
    def _hash(payload: dict) -> str:
        canonical = json.dumps(payload, sort_keys=True, ensure_ascii=False)
        return hashlib.sha256(canonical.encode("utf-8")).hexdigest()

    def add(self, request_id: str, user_id: str, session_id: str, messages: list[dict]) -> str:
        payload_hash = self._hash({"user_id": user_id, "session_id": session_id, "messages": messages})
        with self._lock:
            # BEGIN IMMEDIATE serializes writers across processes as well as
            # threads; retries cannot pass the ledger check concurrently.
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                existing = self._conn.execute("SELECT payload_hash FROM request_ledger WHERE request_id=?", (request_id,)).fetchone()
                if existing:
                    if existing[0] != payload_hash:
                        raise RequestConflictError(request_id)
                    self._conn.rollback()
                    return "duplicate"
                now = datetime.now(timezone.utc).isoformat()
                self._conn.execute("INSERT INTO request_ledger VALUES (?,?,?)", (request_id, payload_hash, now))
                position = self._conn.execute("SELECT COALESCE(MAX(message_index),-1)+1 FROM memories WHERE user_id=? AND session_id=?", (user_id, session_id)).fetchone()[0]
                for offset, message in enumerate(messages):
                    memory_id = str(uuid.uuid4())
                    cursor = self._conn.execute("""INSERT INTO memories
                        (id,user_id,session_id,role,content,created_at,original_timestamp,message_index)
                        VALUES (?,?,?,?,?,?,?,?)""", (memory_id, user_id, session_id, message["role"], message["content"], now, message.get("timestamp"), position + offset))
                    self._index(cursor.lastrowid, memory_id, message["content"])
                self._conn.commit()
                return "new"
            except BaseException:
                self._conn.rollback()
                raise

    def search(self, user_id: str, query: str, top_k: int) -> list[dict]:
        with self._lock:
            # A single snapshot covers candidates and neighbor lookups.
            self._conn.execute("BEGIN")
            try:
                return self._retriever.search(user_id, query, top_k)
            finally:
                self._conn.rollback()

    def delete_memory(self, user_id: str, memory_id: str) -> bool:
        """Internal lifecycle operation; source deletion also removes indexes."""
        with self._lock, self._conn:
            return self._conn.execute("DELETE FROM memories WHERE user_id=? AND id=?", (user_id, memory_id)).rowcount > 0

    def stats(self) -> dict:
        with self._lock:
            row = self._conn.execute("SELECT COUNT(*),COUNT(DISTINCT user_id) FROM memories").fetchone()
            return {"memories": row[0], "users": row[1]}

    def close(self) -> None:
        with self._lock:
            self._conn.close()
