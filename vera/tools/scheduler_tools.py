"""7/24 proaktif hatırlatıcı, zamanlanmış görev (cron) ve sabah brifingi araçları."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from vera.tools.registry import ToolContext, ToolDef, ToolRegistry


def register_scheduler_tools(registry: ToolRegistry) -> None:
    async def schedule_reminder(
        ctx: ToolContext,
        title: str,
        run_at_iso: str,
        message: str,
        send_voice: bool = False,
    ) -> dict[str, Any]:
        # Validate ISO datetime
        dt = datetime.fromisoformat(run_at_iso)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=ctx.settings.tz)

        job_id = ctx.db.upsert_scheduled_job(
            name=title,
            job_type="reminder",
            schedule_expr=dt.isoformat(),
            payload={"message": message, "send_voice": send_voice},
        )
        if ctx.scheduler:
            ctx.scheduler.sync_jobs()
        return {
            "status": "scheduled",
            "job_id": job_id,
            "title": title,
            "run_at": dt.isoformat(),
            "send_voice": send_voice,
        }

    async def schedule_recurring_task(
        ctx: ToolContext,
        name: str,
        cron_expression: str,
        prompt: str,
        send_voice: bool = False,
    ) -> dict[str, Any]:
        parts = cron_expression.strip().split()
        if len(parts) != 5:
            return {
                "error": "Geçersiz cron ifadesi. 5 alanlı olmalı: 'dakika saat gün ay haftanın_günü' (örn. '30 8 * * *')"
            }
        job_id = ctx.db.upsert_scheduled_job(
            name=name,
            job_type="agent_cron",
            schedule_expr=cron_expression.strip(),
            payload={"prompt": prompt, "send_voice": send_voice},
        )
        if ctx.scheduler:
            ctx.scheduler.sync_jobs()
        return {
            "status": "scheduled",
            "job_id": job_id,
            "name": name,
            "cron": cron_expression.strip(),
        }

    async def list_scheduled_jobs(ctx: ToolContext) -> dict[str, Any]:
        jobs = ctx.db.list_scheduled_jobs()
        return {"count": len(jobs), "jobs": jobs}

    async def cancel_scheduled_job(ctx: ToolContext, job_id: str) -> dict[str, Any]:
        ok = ctx.db.delete_scheduled_job(job_id)
        if ctx.scheduler:
            ctx.scheduler.sync_jobs()
        return {"deleted": ok, "job_id": job_id}

    registry.register(
        ToolDef(
            name="schedule_reminder",
            category="scheduler",
            description="Belirli tarih ve saatte Telegram'dan tek seferlik hatırlatma gönderir.",
            parameters={
                "type": "object",
                "properties": {
                    "title": {"type": "string", "description": "Hatırlatma başlığı."},
                    "run_at_iso": {
                        "type": "string",
                        "description": "Çalışma zamanı ISO-8601 (örn. 2026-11-23T09:30:00+03:00).",
                    },
                    "message": {"type": "string", "description": "Gönderilecek hatırlatma metni."},
                    "send_voice": {
                        "type": "boolean",
                        "description": "Hatırlatma ayrıca Türkçe sesli not olarak da gönderilsin mi?",
                    },
                },
                "required": ["title", "run_at_iso", "message"],
            },
            handler=schedule_reminder,
        )
    )

    registry.register(
        ToolDef(
            name="schedule_recurring_task",
            category="scheduler",
            description=(
                "VERA'nın 7/24 periyodik olarak çalıştıracağı otonom bir cron görevi ekler "
                "(örn. her sabah 08:30 brifing, her Pazartesi haftalık özet)."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "name": {"type": "string", "description": "Görev adı."},
                    "cron_expression": {
                        "type": "string",
                        "description": "5 alanlı cron ifadesi (örn. '30 8 * * 1-5' hafta içi 08:30).",
                    },
                    "prompt": {
                        "type": "string",
                        "description": "Zamanı geldiğinde VERA'nın kendi kendine çalıştıracağı talimat.",
                    },
                    "send_voice": {
                        "type": "boolean",
                        "description": "Sonuç sesli not olarak da iletilsin mi?",
                    },
                },
                "required": ["name", "cron_expression", "prompt"],
            },
            handler=schedule_recurring_task,
        )
    )

    registry.register(
        ToolDef(
            name="list_scheduled_jobs",
            category="scheduler",
            description="Kayıtlı tüm hatırlatıcı ve 7/24 periyodik görevleri listeler.",
            parameters={"type": "object", "properties": {}, "required": []},
            handler=list_scheduled_jobs,
        )
    )

    registry.register(
        ToolDef(
            name="cancel_scheduled_job",
            category="scheduler",
            description="ID'si verilen hatırlatıcı veya cron görevini iptal eder.",
            parameters={
                "type": "object",
                "properties": {"job_id": {"type": "string", "description": "İptal edilecek görev ID'si."}},
                "required": ["job_id"],
            },
            handler=cancel_scheduled_job,
        )
    )
