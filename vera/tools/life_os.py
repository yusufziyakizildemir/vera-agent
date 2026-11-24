"""VERA Kişisel Yaşam İşletim Sistemi (SQLite Kalıcı Görevler & Harcama Takibi)."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

from vera.database import utc_now_iso
from vera.tools.registry import ToolContext, ToolDef, ToolRegistry

_LIFE_OS_SCHEMA = """
CREATE TABLE IF NOT EXISTS personal_tasks (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    title TEXT NOT NULL,
    priority TEXT NOT NULL DEFAULT 'normal',
    due_date TEXT DEFAULT '',
    status TEXT NOT NULL DEFAULT 'open',
    created_at TEXT NOT NULL,
    completed_at TEXT DEFAULT ''
);
CREATE TABLE IF NOT EXISTS personal_expenses (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    amount REAL NOT NULL,
    currency TEXT NOT NULL DEFAULT 'TRY',
    category TEXT NOT NULL DEFAULT 'genel',
    note TEXT DEFAULT '',
    created_at TEXT NOT NULL
);
"""


def _ensure_tables(ctx: ToolContext) -> None:
    with ctx.db.connect() as conn:
        conn.executescript(_LIFE_OS_SCHEMA)


def register_life_os_tools(registry: ToolRegistry) -> None:
    """Registers personal tasks and expense tracking backed by SQLite + HF cloud sync."""

    async def manage_tasks(
        ctx: ToolContext,
        action: str = "list",
        title: str = "",
        priority: str = "normal",
        due_date: str = "",
        task_id: int = 0,
    ) -> dict[str, Any]:
        _ensure_tables(ctx)
        act = (action or "list").strip().lower()
        clean_title = (title or "").strip()
        prio = (priority or "normal").strip().lower()
        if prio not in {"low", "normal", "high", "urgent"}:
            prio = "normal"

        with ctx.db.connect() as conn:
            if act == "add":
                if not clean_title:
                    return {"error": "Görev eklemek için başlık ('title') gerekli."}
                now = utc_now_iso()
                cur = conn.execute(
                    "INSERT INTO personal_tasks (title, priority, due_date, status, created_at) VALUES (?, ?, ?, 'open', ?)",
                    (clean_title, prio, (due_date or "").strip(), now),
                )
                return {
                    "ok": True,
                    "id": cur.lastrowid,
                    "title": clean_title,
                    "priority": prio,
                    "due_date": (due_date or "").strip(),
                    "status": "open",
                }

            if act in {"list", "all"}:
                where = "" if act == "all" else "WHERE status = 'open'"
                rows = conn.execute(
                    f"SELECT id, title, priority, due_date, status, created_at, completed_at "
                    f"FROM personal_tasks {where} ORDER BY status DESC, id DESC LIMIT 50"
                ).fetchall()
                return {"count": len(rows), "tasks": [dict(r) for r in rows]}

            if act in {"complete", "done"}:
                now = utc_now_iso()
                if task_id and int(task_id) > 0:
                    cur = conn.execute(
                        "UPDATE personal_tasks SET status = 'done', completed_at = ? WHERE id = ?",
                        (now, int(task_id)),
                    )
                elif clean_title:
                    row = conn.execute(
                        "SELECT id FROM personal_tasks WHERE status = 'open' AND title LIKE ? ORDER BY id DESC LIMIT 1",
                        (f"%{clean_title}%",),
                    ).fetchone()
                    if not row:
                        return {"error": f"Tamamlanacak açık görev bulunamadı: {clean_title}"}
                    cur = conn.execute(
                        "UPDATE personal_tasks SET status = 'done', completed_at = ? WHERE id = ?",
                        (now, row["id"]),
                    )
                else:
                    return {"error": "Görev tamamlamak için `task_id` veya `title` gerekli."}
                if cur.rowcount == 0:
                    return {"error": "Belirtilen görev bulunamadı."}
                return {"ok": True, "status": "done", "updated": cur.rowcount}

            if act in {"delete", "remove"}:
                if task_id and int(task_id) > 0:
                    cur = conn.execute("DELETE FROM personal_tasks WHERE id = ?", (int(task_id),))
                elif clean_title:
                    cur = conn.execute("DELETE FROM personal_tasks WHERE title LIKE ?", (f"%{clean_title}%",))
                else:
                    return {"error": "Silmek için `task_id` veya `title` gerekli."}
                return {"ok": cur.rowcount > 0, "deleted": cur.rowcount}

        return {"error": f"Bilinmeyen görev işlemi: {action}. Kullanım: add, list, all, complete, delete"}

    async def manage_expenses(
        ctx: ToolContext,
        action: str = "summary",
        amount: float = 0.0,
        category: str = "genel",
        note: str = "",
        currency: str = "TRY",
        days: int = 30,
        expense_id: int = 0,
    ) -> dict[str, Any]:
        _ensure_tables(ctx)
        act = (action or "summary").strip().lower()
        curr = (currency or "TRY").strip().upper()[:6]
        cat = (category or "genel").strip().lower()[:40]
        lookback_days = max(1, min(3650, int(days or 30)))
        since_iso = (datetime.now(timezone.utc) - timedelta(days=lookback_days)).isoformat()

        with ctx.db.connect() as conn:
            if act == "add":
                try:
                    val = float(amount)
                except (TypeError, ValueError):
                    return {"error": "Geçerli bir tutar ('amount') girin."}
                if val <= 0:
                    return {"error": "Harcama tutarı sıfırdan büyük olmalı."}
                now = utc_now_iso()
                cur = conn.execute(
                    "INSERT INTO personal_expenses (amount, currency, category, note, created_at) VALUES (?, ?, ?, ?, ?)",
                    (round(val, 2), curr, cat, (note or "").strip(), now),
                )
                return {
                    "ok": True,
                    "id": cur.lastrowid,
                    "amount": round(val, 2),
                    "currency": curr,
                    "category": cat,
                    "note": (note or "").strip(),
                }

            if act == "list":
                rows = conn.execute(
                    "SELECT id, amount, currency, category, note, created_at FROM personal_expenses "
                    "WHERE created_at >= ? ORDER BY id DESC LIMIT 30",
                    (since_iso,),
                ).fetchall()
                return {"days": lookback_days, "count": len(rows), "expenses": [dict(r) for r in rows]}

            if act == "summary":
                totals = conn.execute(
                    "SELECT currency, category, ROUND(SUM(amount), 2) AS total, COUNT(*) AS count "
                    "FROM personal_expenses WHERE created_at >= ? GROUP BY currency, category ORDER BY total DESC",
                    (since_iso,),
                ).fetchall()
                grand = conn.execute(
                    "SELECT currency, ROUND(SUM(amount), 2) AS total, COUNT(*) AS count "
                    "FROM personal_expenses WHERE created_at >= ? GROUP BY currency",
                    (since_iso,),
                ).fetchall()
                recent = conn.execute(
                    "SELECT id, amount, currency, category, note, created_at FROM personal_expenses "
                    "ORDER BY id DESC LIMIT 5"
                ).fetchall()
                return {
                    "days": lookback_days,
                    "grand_totals": [dict(r) for r in grand],
                    "by_category": [dict(r) for r in totals],
                    "recent": [dict(r) for r in recent],
                }

            if act in {"delete", "remove"}:
                if not expense_id or int(expense_id) <= 0:
                    return {"error": "Silmek için geçerli `expense_id` gerekli."}
                cur = conn.execute("DELETE FROM personal_expenses WHERE id = ?", (int(expense_id),))
                return {"ok": cur.rowcount > 0, "deleted": cur.rowcount}

        return {"error": f"Bilinmeyen harcama işlemi: {action}. Kullanım: add, list, summary, delete"}

    registry.register(
        ToolDef(
            name="manage_tasks",
            description="Kalıcı kişisel yapılacaklar/görevler listesi yönetir (ekle, listele, tamamla, sil).",
            parameters={
                "type": "object",
                "properties": {
                    "action": {
                        "type": "string",
                        "enum": ["add", "list", "all", "complete", "delete"],
                        "description": "İşlem türü",
                    },
                    "title": {"type": "string", "description": "Görev başlığı"},
                    "priority": {"type": "string", "enum": ["low", "normal", "high", "urgent"], "description": "Öncelik"},
                    "due_date": {"type": "string", "description": "Son tarih notu (ör: 2026-11-30)"},
                    "task_id": {"type": "integer", "description": "Görev ID (tamamla/sil için)"},
                },
            },
            handler=manage_tasks,
            category="productivity",
        )
    )

    registry.register(
        ToolDef(
            name="manage_expenses",
            description="Kişisel harcamaları ve bütçe özetini SQLite'ta kalıcı olarak takip eder (ekle, listele, özetle, sil).",
            parameters={
                "type": "object",
                "properties": {
                    "action": {
                        "type": "string",
                        "enum": ["add", "list", "summary", "delete"],
                        "description": "İşlem türü",
                    },
                    "amount": {"type": "number", "description": "Tutar"},
                    "category": {"type": "string", "description": "Kategori (market, ulaşım, yemek, fatura, genel vb.)"},
                    "note": {"type": "string", "description": "Kısa açıklama"},
                    "currency": {"type": "string", "description": "Para birimi (TRY, USD, EUR)"},
                    "days": {"type": "integer", "description": "Kaç günlük özet/liste (varsayılan 30)"},
                    "expense_id": {"type": "integer", "description": "Silinecek kayıt ID"},
                },
            },
            handler=manage_expenses,
            category="productivity",
        )
    )
