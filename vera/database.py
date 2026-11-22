"""VERA SQLite WAL + FTS5 + Vektör Hafıza, Görevler, Onaylar ve Denetim Veritabanı."""

from __future__ import annotations

import json
import math
import sqlite3
import threading
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


class Database:
    """Thread-safe SQLite storage with FTS5 full-text search and cosine vector scoring."""

    def __init__(self, db_path: Path):
        self.db_path = db_path
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._init_schema()

    @contextmanager
    def connect(self) -> Iterator[sqlite3.Connection]:
        with self._lock:
            conn = sqlite3.connect(str(self.db_path), timeout=30.0)
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA journal_mode=WAL;")
            conn.execute("PRAGMA synchronous=NORMAL;")
            conn.execute("PRAGMA foreign_keys=ON;")
            try:
                yield conn
                conn.commit()
            except Exception:
                conn.rollback()
                raise
            finally:
                conn.close()

    def _init_schema(self) -> None:
        with self.connect() as conn:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS messages (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    session_id TEXT NOT NULL,
                    channel TEXT NOT NULL,
                    role TEXT NOT NULL,
                    content TEXT NOT NULL,
                    metadata_json TEXT DEFAULT '{}',
                    created_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_messages_session ON messages(session_id, id DESC);

                CREATE TABLE IF NOT EXISTS memory_entries (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    category TEXT NOT NULL,
                    content TEXT NOT NULL,
                    tags TEXT DEFAULT '',
                    importance INTEGER DEFAULT 3,
                    embedding_json TEXT DEFAULT NULL,
                    consolidated INTEGER DEFAULT 0,
                    created_at TEXT NOT NULL
                );

                CREATE VIRTUAL TABLE IF NOT EXISTS memory_fts USING fts5(
                    content,
                    category,
                    tags,
                    content='memory_entries',
                    content_rowid='id'
                );

                CREATE TRIGGER IF NOT EXISTS memory_entries_ai AFTER INSERT ON memory_entries BEGIN
                    INSERT INTO memory_fts(rowid, content, category, tags)
                    VALUES (new.id, new.content, new.category, new.tags);
                END;

                CREATE TRIGGER IF NOT EXISTS memory_entries_ad AFTER DELETE ON memory_entries BEGIN
                    INSERT INTO memory_fts(memory_fts, rowid, content, category, tags)
                    VALUES('delete', old.id, old.content, old.category, old.tags);
                END;

                CREATE TRIGGER IF NOT EXISTS memory_entries_au AFTER UPDATE ON memory_entries BEGIN
                    INSERT INTO memory_fts(memory_fts, rowid, content, category, tags)
                    VALUES('delete', old.id, old.content, old.category, old.tags);
                    INSERT INTO memory_fts(rowid, content, category, tags)
                    VALUES (new.id, new.content, new.category, new.tags);
                END;

                CREATE TABLE IF NOT EXISTS notes (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    title TEXT NOT NULL,
                    content TEXT NOT NULL,
                    tags TEXT DEFAULT '',
                    updated_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS scheduled_jobs (
                    id TEXT PRIMARY KEY,
                    name TEXT NOT NULL,
                    job_type TEXT NOT NULL,
                    schedule_expr TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    enabled INTEGER DEFAULT 1,
                    last_run_at TEXT DEFAULT NULL,
                    created_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS web_watchers (
                    id TEXT PRIMARY KEY,
                    name TEXT NOT NULL,
                    url TEXT NOT NULL,
                    selector TEXT DEFAULT '',
                    instruction TEXT NOT NULL,
                    interval_minutes INTEGER DEFAULT 60,
                    last_hash TEXT DEFAULT '',
                    last_excerpt TEXT DEFAULT '',
                    last_checked_at TEXT DEFAULT NULL,
                    enabled INTEGER DEFAULT 1,
                    created_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS pending_approvals (
                    id TEXT PRIMARY KEY,
                    session_id TEXT NOT NULL,
                    channel TEXT NOT NULL,
                    tool_name TEXT NOT NULL,
                    args_json TEXT NOT NULL,
                    summary TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'pending',
                    result_text TEXT DEFAULT NULL,
                    created_at TEXT NOT NULL,
                    resolved_at TEXT DEFAULT NULL
                );

                CREATE TABLE IF NOT EXISTS audit_logs (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    event_type TEXT NOT NULL,
                    detail TEXT NOT NULL,
                    tokens_in INTEGER DEFAULT 0,
                    tokens_out INTEGER DEFAULT 0,
                    model TEXT DEFAULT '',
                    created_at TEXT NOT NULL
                );
                """
            )

    # --- Messages ---
    def add_message(
        self,
        session_id: str,
        channel: str,
        role: str,
        content: str,
        metadata: dict[str, Any] | None = None,
    ) -> int:
        with self.connect() as conn:
            cur = conn.execute(
                """
                INSERT INTO messages (session_id, channel, role, content, metadata_json, created_at)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    session_id,
                    channel,
                    role,
                    content,
                    json.dumps(metadata or {}, ensure_ascii=False),
                    utc_now_iso(),
                ),
            )
            return int(cur.lastrowid)

    def get_recent_messages(self, session_id: str, limit: int = 20) -> list[dict[str, Any]]:
        with self.connect() as conn:
            rows = conn.execute(
                """
                SELECT id, session_id, channel, role, content, metadata_json, created_at
                FROM messages
                WHERE session_id = ?
                ORDER BY id DESC
                LIMIT ?
                """,
                (session_id, limit),
            ).fetchall()
        items = [dict(r) for r in reversed(rows)]
        for item in items:
            try:
                item["metadata"] = json.loads(item.pop("metadata_json") or "{}")
            except Exception:
                item["metadata"] = {}
        return items

    def clear_session_messages(self, session_id: str) -> int:
        with self.connect() as conn:
            cur = conn.execute("DELETE FROM messages WHERE session_id = ?", (session_id,))
            return cur.rowcount

    def get_messages_since(self, since_iso: str, limit: int = 120) -> list[dict[str, Any]]:
        with self.connect() as conn:
            rows = conn.execute(
                """
                SELECT id, session_id, channel, role, content, created_at
                FROM messages
                WHERE created_at >= ?
                ORDER BY id ASC
                LIMIT ?
                """,
                (since_iso, limit),
            ).fetchall()
        return [dict(r) for r in rows]

    # --- Episodic & Semantic Memory ---
    def add_memory_entry(
        self,
        category: str,
        content: str,
        tags: str = "",
        importance: int = 3,
        embedding: list[float] | None = None,
    ) -> int:
        emb_json = json.dumps(embedding) if embedding else None
        with self.connect() as conn:
            cur = conn.execute(
                """
                INSERT INTO memory_entries (category, content, tags, importance, embedding_json, created_at)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (category, content, tags, max(1, min(5, importance)), emb_json, utc_now_iso()),
            )
            return int(cur.lastrowid)

    def search_memory(
        self,
        query: str,
        query_embedding: list[float] | None = None,
        limit: int = 8,
    ) -> list[dict[str, Any]]:
        results_by_id: dict[int, dict[str, Any]] = {}

        # 1) SQLite FTS5 keyword recall
        safe_tokens = [
            "".join(ch for ch in tok if ch.isalnum() or ch in "çğıöşüÇĞİÖŞÜ_-")
            for tok in query.split()
        ]
        safe_tokens = [t for t in safe_tokens if len(t) >= 2]
        if safe_tokens:
            fts_query = " OR ".join(f'"{t}"*' for t in safe_tokens[:8])
            with self.connect() as conn:
                try:
                    rows = conn.execute(
                        """
                        SELECT m.id, m.category, m.content, m.tags, m.importance, m.created_at,
                               bm25(memory_fts) AS rank_score
                        FROM memory_fts
                        JOIN memory_entries m ON m.id = memory_fts.rowid
                        WHERE memory_fts MATCH ?
                        ORDER BY rank_score ASC
                        LIMIT ?
                        """,
                        (fts_query, limit * 2),
                    ).fetchall()
                    for r in rows:
                        d = dict(r)
                        # bm25 returns negative or small numbers; normalize into positive relevance
                        d["score"] = 1.0 / (1.0 + abs(float(d.pop("rank_score", 0.0))))
                        results_by_id[d["id"]] = d
                except sqlite3.OperationalError:
                    pass

        # 2) Semantic cosine similarity (when embeddings are available)
        if query_embedding:
            q_norm = math.sqrt(sum(x * x for x in query_embedding)) or 1.0
            with self.connect() as conn:
                rows = conn.execute(
                    """
                    SELECT id, category, content, tags, importance, embedding_json, created_at
                    FROM memory_entries
                    WHERE embedding_json IS NOT NULL
                    ORDER BY id DESC
                    LIMIT 400
                    """
                ).fetchall()
            for r in rows:
                try:
                    vec = json.loads(r["embedding_json"])
                    if len(vec) != len(query_embedding):
                        continue
                    v_norm = math.sqrt(sum(x * x for x in vec)) or 1.0
                    sim = sum(a * b for a, b in zip(query_embedding, vec)) / (q_norm * v_norm)
                except Exception:
                    continue
                if sim < 0.25:
                    continue
                entry_id = int(r["id"])
                if entry_id in results_by_id:
                    results_by_id[entry_id]["score"] += float(sim) * 1.5
                else:
                    results_by_id[entry_id] = {
                        "id": entry_id,
                        "category": r["category"],
                        "content": r["content"],
                        "tags": r["tags"],
                        "importance": r["importance"],
                        "created_at": r["created_at"],
                        "score": float(sim) * 1.5,
                    }

        # 3) Fallback LIKE scan if FTS returned nothing
        if not results_by_id and query.strip():
            like = f"%{query.strip()[:60]}%"
            with self.connect() as conn:
                rows = conn.execute(
                    """
                    SELECT id, category, content, tags, importance, created_at
                    FROM memory_entries
                    WHERE content LIKE ? OR tags LIKE ? OR category LIKE ?
                    ORDER BY importance DESC, id DESC
                    LIMIT ?
                    """,
                    (like, like, like, limit),
                ).fetchall()
            for r in rows:
                d = dict(r)
                d["score"] = 0.5
                results_by_id[d["id"]] = d

        ranked = sorted(
            results_by_id.values(),
            key=lambda x: (x.get("score", 0.0) + 0.08 * x.get("importance", 3)),
            reverse=True,
        )
        return ranked[:limit]

    def list_recent_memories(self, limit: int = 30) -> list[dict[str, Any]]:
        with self.connect() as conn:
            rows = conn.execute(
                """
                SELECT id, category, content, tags, importance, created_at
                FROM memory_entries
                ORDER BY id DESC
                LIMIT ?
                """,
                (limit,),
            ).fetchall()
        return [dict(r) for r in rows]

    def delete_memory_entry(self, entry_id: int) -> bool:
        with self.connect() as conn:
            cur = conn.execute("DELETE FROM memory_entries WHERE id = ?", (entry_id,))
            return cur.rowcount > 0

    # --- Notes ---
    def save_note(self, title: str, content: str, tags: str = "") -> int:
        with self.connect() as conn:
            cur = conn.execute(
                "INSERT INTO notes (title, content, tags, updated_at) VALUES (?, ?, ?, ?)",
                (title, content, tags, utc_now_iso()),
            )
            return int(cur.lastrowid)

    def list_notes(self, query: str = "", limit: int = 20) -> list[dict[str, Any]]:
        with self.connect() as conn:
            if query.strip():
                like = f"%{query.strip()}%"
                rows = conn.execute(
                    """
                    SELECT id, title, content, tags, updated_at
                    FROM notes
                    WHERE title LIKE ? OR content LIKE ? OR tags LIKE ?
                    ORDER BY updated_at DESC
                    LIMIT ?
                    """,
                    (like, like, like, limit),
                ).fetchall()
            else:
                rows = conn.execute(
                    "SELECT id, title, content, tags, updated_at FROM notes ORDER BY updated_at DESC LIMIT ?",
                    (limit,),
                ).fetchall()
        return [dict(r) for r in rows]

    # --- Scheduled Jobs ---
    def upsert_scheduled_job(
        self,
        name: str,
        job_type: str,
        schedule_expr: str,
        payload: dict[str, Any],
        job_id: str | None = None,
        enabled: bool = True,
    ) -> str:
        jid = job_id or f"job_{uuid.uuid4().hex[:8]}"
        with self.connect() as conn:
            conn.execute(
                """
                INSERT INTO scheduled_jobs (id, name, job_type, schedule_expr, payload_json, enabled, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(id) DO UPDATE SET
                    name=excluded.name,
                    job_type=excluded.job_type,
                    schedule_expr=excluded.schedule_expr,
                    payload_json=excluded.payload_json,
                    enabled=excluded.enabled
                """,
                (
                    jid,
                    name,
                    job_type,
                    schedule_expr,
                    json.dumps(payload, ensure_ascii=False),
                    1 if enabled else 0,
                    utc_now_iso(),
                ),
            )
        return jid

    def list_scheduled_jobs(self, only_enabled: bool = False) -> list[dict[str, Any]]:
        sql = "SELECT * FROM scheduled_jobs"
        if only_enabled:
            sql += " WHERE enabled = 1"
        sql += " ORDER BY created_at DESC"
        with self.connect() as conn:
            rows = conn.execute(sql).fetchall()
        out = []
        for r in rows:
            d = dict(r)
            d["payload"] = json.loads(d.pop("payload_json") or "{}")
            d["enabled"] = bool(d["enabled"])
            out.append(d)
        return out

    def mark_job_ran(self, job_id: str) -> None:
        with self.connect() as conn:
            conn.execute(
                "UPDATE scheduled_jobs SET last_run_at = ? WHERE id = ?",
                (utc_now_iso(), job_id),
            )

    def delete_scheduled_job(self, job_id: str) -> bool:
        with self.connect() as conn:
            cur = conn.execute("DELETE FROM scheduled_jobs WHERE id = ?", (job_id,))
            return cur.rowcount > 0

    def set_job_enabled(self, job_id: str, enabled: bool) -> bool:
        with self.connect() as conn:
            cur = conn.execute(
                "UPDATE scheduled_jobs SET enabled = ? WHERE id = ?",
                (1 if enabled else 0, job_id),
            )
            return cur.rowcount > 0

    # --- Web Watchers ---
    def add_web_watcher(
        self,
        name: str,
        url: str,
        instruction: str,
        selector: str = "",
        interval_minutes: int = 60,
    ) -> str:
        wid = f"watch_{uuid.uuid4().hex[:8]}"
        with self.connect() as conn:
            conn.execute(
                """
                INSERT INTO web_watchers (id, name, url, selector, instruction, interval_minutes, enabled, created_at)
                VALUES (?, ?, ?, ?, ?, ?, 1, ?)
                """,
                (wid, name, url, selector, instruction, max(10, interval_minutes), utc_now_iso()),
            )
        return wid

    def list_web_watchers(self, only_enabled: bool = False) -> list[dict[str, Any]]:
        sql = "SELECT * FROM web_watchers"
        if only_enabled:
            sql += " WHERE enabled = 1"
        sql += " ORDER BY created_at DESC"
        with self.connect() as conn:
            rows = conn.execute(sql).fetchall()
        return [dict(r) for r in rows]

    def update_web_watcher_state(self, watcher_id: str, content_hash: str, excerpt: str) -> None:
        with self.connect() as conn:
            conn.execute(
                """
                UPDATE web_watchers
                SET last_hash = ?, last_excerpt = ?, last_checked_at = ?
                WHERE id = ?
                """,
                (content_hash, excerpt[:1500], utc_now_iso(), watcher_id),
            )

    def delete_web_watcher(self, watcher_id: str) -> bool:
        with self.connect() as conn:
            cur = conn.execute("DELETE FROM web_watchers WHERE id = ?", (watcher_id,))
            return cur.rowcount > 0

    # --- Approvals (Human-in-the-loop safety gate) ---
    def create_approval(
        self,
        session_id: str,
        channel: str,
        tool_name: str,
        args: dict[str, Any],
        summary: str,
    ) -> str:
        aid = f"apr_{uuid.uuid4().hex[:8]}"
        with self.connect() as conn:
            conn.execute(
                """
                INSERT INTO pending_approvals
                (id, session_id, channel, tool_name, args_json, summary, status, created_at)
                VALUES (?, ?, ?, ?, ?, ?, 'pending', ?)
                """,
                (
                    aid,
                    session_id,
                    channel,
                    tool_name,
                    json.dumps(args, ensure_ascii=False),
                    summary,
                    utc_now_iso(),
                ),
            )
        return aid

    def get_approval(self, approval_id: str) -> dict[str, Any] | None:
        with self.connect() as conn:
            row = conn.execute(
                "SELECT * FROM pending_approvals WHERE id = ?", (approval_id,)
            ).fetchone()
        if not row:
            return None
        d = dict(row)
        d["args"] = json.loads(d.pop("args_json") or "{}")
        return d

    def list_approvals(self, status: str | None = None, limit: int = 30) -> list[dict[str, Any]]:
        with self.connect() as conn:
            if status:
                rows = conn.execute(
                    "SELECT * FROM pending_approvals WHERE status = ? ORDER BY created_at DESC LIMIT ?",
                    (status, limit),
                ).fetchall()
            else:
                rows = conn.execute(
                    "SELECT * FROM pending_approvals ORDER BY created_at DESC LIMIT ?",
                    (limit,),
                ).fetchall()
        out = []
        for r in rows:
            d = dict(r)
            d["args"] = json.loads(d.pop("args_json") or "{}")
            out.append(d)
        return out

    def resolve_approval(self, approval_id: str, status: str, result_text: str = "") -> bool:
        with self.connect() as conn:
            cur = conn.execute(
                """
                UPDATE pending_approvals
                SET status = ?, result_text = ?, resolved_at = ?
                WHERE id = ? AND status = 'pending'
                """,
                (status, result_text, utc_now_iso(), approval_id),
            )
            return cur.rowcount > 0

    # --- Audit & Token Accounting ---
    def log_event(
        self,
        event_type: str,
        detail: str,
        tokens_in: int = 0,
        tokens_out: int = 0,
        model: str = "",
    ) -> None:
        with self.connect() as conn:
            conn.execute(
                """
                INSERT INTO audit_logs (event_type, detail, tokens_in, tokens_out, model, created_at)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (event_type, detail[:2000], tokens_in, tokens_out, model, utc_now_iso()),
            )

    def list_audit_logs(self, limit: int = 50) -> list[dict[str, Any]]:
        with self.connect() as conn:
            rows = conn.execute(
                "SELECT * FROM audit_logs ORDER BY id DESC LIMIT ?", (limit,)
            ).fetchall()
        return [dict(r) for r in rows]

    def get_usage_stats(self) -> dict[str, Any]:
        with self.connect() as conn:
            totals = conn.execute(
                """
                SELECT
                    COUNT(*) AS total_events,
                    COALESCE(SUM(tokens_in), 0) AS total_tokens_in,
                    COALESCE(SUM(tokens_out), 0) AS total_tokens_out
                FROM audit_logs
                """
            ).fetchone()
            msg_count = conn.execute("SELECT COUNT(*) FROM messages").fetchone()[0]
            mem_count = conn.execute("SELECT COUNT(*) FROM memory_entries").fetchone()[0]
            job_count = conn.execute(
                "SELECT COUNT(*) FROM scheduled_jobs WHERE enabled = 1"
            ).fetchone()[0]
            pending_count = conn.execute(
                "SELECT COUNT(*) FROM pending_approvals WHERE status = 'pending'"
            ).fetchone()[0]
        return {
            "total_events": int(totals["total_events"]),
            "total_tokens_in": int(totals["total_tokens_in"]),
            "total_tokens_out": int(totals["total_tokens_out"]),
            "messages_count": int(msg_count),
            "memories_count": int(mem_count),
            "active_jobs_count": int(job_count),
            "pending_approvals_count": int(pending_count),
        }
