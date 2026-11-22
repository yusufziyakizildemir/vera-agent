"""VERA 7/24 Proaktif Zamanlayıcı (Hatırlatmalar, Cron, Sabah Brifingi, Web Takipçisi, Gece Dreaming)."""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import TYPE_CHECKING, Any

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.date import DateTrigger
from apscheduler.triggers.interval import IntervalTrigger

from vera.tools.web_tools import fetch_clean_text

if TYPE_CHECKING:
    from vera.agent import VeraAgent

logger = logging.getLogger("vera.scheduler")


class VeraScheduler:
    def __init__(self, agent: VeraAgent):
        self.agent = agent
        self.scheduler = AsyncIOScheduler(timezone=agent.settings.tz)
        self._started = False

    def start(self) -> None:
        if self._started:
            return
        self._ensure_default_jobs()
        self.sync_jobs()

        # Nightly Hermes memory consolidation ('Dreaming')
        hour, minute = self._parse_hhmm(self.agent.settings.dream_schedule_time, 3, 30)
        self.scheduler.add_job(
            self._run_dreaming,
            trigger=CronTrigger(hour=hour, minute=minute, timezone=self.agent.settings.tz),
            id="system_nightly_dream",
            replace_existing=True,
        )

        # Periodic Web Watchers checker every 15 minutes
        self.scheduler.add_job(
            self._check_web_watchers,
            trigger=IntervalTrigger(minutes=15),
            id="system_web_watchers",
            replace_existing=True,
        )

        # Periodic Cloud Persistence sync (Hugging Face Private Dataset)
        if self.agent.cloud_sync.enabled:
            self.scheduler.add_job(
                self._run_cloud_backup,
                trigger=IntervalTrigger(minutes=max(5, self.agent.settings.hf_sync_interval_minutes)),
                id="system_cloud_backup",
                replace_existing=True,
            )

        # 7/24 Self Keep-Alive Ping every 10 minutes (prevents Render 15-min idle sleep)
        self.scheduler.add_job(
            self._run_keepalive_ping,
            trigger=IntervalTrigger(minutes=10),
            id="system_keepalive_ping",
            replace_existing=True,
        )

        self.scheduler.start()
        self._started = True
        logger.info("VERA 7/24 proaktif zamanlayıcı başlatıldı (%s).", self.agent.settings.timezone)

    def stop(self) -> None:
        if self._started:
            self.scheduler.shutdown(wait=False)
            self._started = False

    @staticmethod
    def _parse_hhmm(value: str, default_h: int, default_m: int) -> tuple[int, int]:
        try:
            h, m = value.strip().split(":")
            return max(0, min(23, int(h))), max(0, min(59, int(m)))
        except Exception:
            return default_h, default_m

    def _ensure_default_jobs(self) -> None:
        existing = {j["id"] for j in self.agent.db.list_scheduled_jobs()}
        if "morning_briefing" not in existing:
            h, m = self._parse_hhmm(self.agent.settings.morning_briefing_time, 8, 30)
            self.agent.db.upsert_scheduled_job(
                job_id="morning_briefing",
                name="Günlük Sabah Brifingi",
                job_type="morning_briefing",
                schedule_expr=f"{m} {h} * * *",
                payload={"send_voice": True},
                enabled=True,
            )

    def sync_jobs(self) -> None:
        """Synchronizes SQLite scheduled_jobs table with APScheduler."""
        # Remove old dynamic jobs (keep system_* jobs)
        for job in list(self.scheduler.get_jobs()):
            if not job.id.startswith("system_"):
                job.remove()

        now = datetime.now(self.agent.settings.tz)
        for row in self.agent.db.list_scheduled_jobs(only_enabled=True):
            jid = row["id"]
            jtype = row["job_type"]
            expr = row["schedule_expr"]
            payload = row["payload"]

            try:
                if jtype == "reminder":
                    run_dt = datetime.fromisoformat(expr)
                    if run_dt.tzinfo is None:
                        run_dt = run_dt.replace(tzinfo=self.agent.settings.tz)
                    if run_dt <= now:
                        # Missed reminder while offline -> fire in 5 seconds then disable
                        run_dt = now + timedelta(seconds=5)
                    self.scheduler.add_job(
                        self._execute_db_job,
                        trigger=DateTrigger(run_date=run_dt),
                        args=[row],
                        id=jid,
                        replace_existing=True,
                    )
                else:
                    parts = expr.split()
                    if len(parts) != 5:
                        continue
                    minute, hour, day, month, dow = parts
                    self.scheduler.add_job(
                        self._execute_db_job,
                        trigger=CronTrigger(
                            minute=minute,
                            hour=hour,
                            day=day,
                            month=month,
                            day_of_week=dow,
                            timezone=self.agent.settings.tz,
                        ),
                        args=[row],
                        id=jid,
                        replace_existing=True,
                    )
            except Exception as exc:
                logger.warning("Görev senkronize edilemedi (%s): %s", jid, exc)

    async def _notify(self, text: str, send_voice: bool = False) -> None:
        if self.agent.proactive_sender:
            try:
                await self.agent.proactive_sender(text, send_voice)
            except Exception as exc:
                logger.warning("Proaktif bildirim gönderilemedi: %s", exc)
        else:
            logger.info("[Proaktif Bildirim - Kanal Bağlı Değil]: %s", text[:300])

    async def _execute_db_job(self, job_row: dict[str, Any]) -> None:
        jid = job_row["id"]
        jtype = job_row["job_type"]
        payload = job_row.get("payload", {})
        send_voice = bool(payload.get("send_voice", False))
        logger.info("Zamanlanmış görev tetiklendi: %s (%s)", job_row["name"], jtype)

        try:
            if jtype == "reminder":
                msg = f"⏰ **Hatırlatma — {job_row['name']}**\n\n{payload.get('message', '')}"
                await self._notify(msg, send_voice=send_voice)
                self.agent.db.set_job_enabled(jid, False)

            elif jtype == "morning_briefing":
                briefing = await self.agent.generate_morning_briefing()
                await self._notify(f"☀️ **Günaydın — VERA Sabah Brifingi**\n\n{briefing}", send_voice=send_voice)

            elif jtype == "agent_cron":
                prompt = payload.get("prompt", "")
                if prompt:
                    reply = await self.agent.handle_message(
                        user_text=prompt,
                        session_id=f"cron_{jid}",
                        channel="scheduler",
                        persist_history=False,
                    )
                    await self._notify(
                        f"🤖 **7/24 Otomatik Rapor — {job_row['name']}**\n\n{reply.text}",
                        send_voice=send_voice,
                    )
            self.agent.db.mark_job_ran(jid)
            self.agent.db.log_event("scheduled_job_ran", f"{jid} ({job_row['name']})")
        except Exception as exc:
            logger.exception("Zamanlanmış görev hatası (%s): %s", jid, exc)
            self.agent.db.log_event("scheduled_job_error", f"{jid}: {exc}")

    async def _run_dreaming(self) -> None:
        logger.info("Gece Hermes hafıza konsolidasyonu (Dreaming) başlıyor...")
        try:
            res = await self.agent.memory.consolidate_dream(self.agent.llm, hours=24)
            logger.info("Dreaming sonucu: %s", res)
            if self.agent.cloud_sync.enabled:
                await self.agent.cloud_sync.backup_async()
        except Exception as exc:
            logger.exception("Dreaming hatası: %s", exc)

    async def _run_cloud_backup(self) -> None:
        await self.agent.cloud_sync.backup_async()

    async def _run_keepalive_ping(self) -> None:
        base = (self.agent.settings.public_base_url or "").rstrip("/")
        if not base.startswith("https://"):
            return
        try:
            import httpx

            async with httpx.AsyncClient(timeout=10.0) as client:
                await client.get(f"{base}/api/ping")
        except Exception as exc:
            logger.debug("Keepalive ping atlandı: %s", exc)

    async def _check_web_watchers(self) -> None:
        watchers = self.agent.db.list_web_watchers(only_enabled=True)
        now = datetime.now(timezone.utc)
        for w in watchers:
            try:
                last_checked = w.get("last_checked_at")
                if last_checked:
                    last_dt = datetime.fromisoformat(last_checked)
                    if (now - last_dt).total_seconds() < int(w["interval_minutes"]) * 60:
                        continue

                snap = await fetch_clean_text(w["url"], selector=w.get("selector", ""), max_chars=2500)
                if "error" in snap:
                    continue
                new_hash = snap["hash"]
                new_text = snap["text"]
                old_hash = w.get("last_hash") or ""
                old_text = w.get("last_excerpt") or ""
                self.agent.db.update_web_watcher_state(w["id"], new_hash, new_text)

                if old_hash and new_hash != old_hash:
                    eval_prompt = (
                        f"İzlenen sayfa değişti: {w['name']} ({w['url']})\n"
                        f"Kullanıcı kuralı: {w['instruction']}\n\n"
                        f"ÖNCEKİ ÖZET:\n{old_text[:900]}\n\n"
                        f"YENİ ÖZET:\n{new_text[:900]}\n\n"
                        "Eğer değişiklik kullanıcının takip kuralına göre ÖNEMLİ ise kısa Türkçe bildirim yaz; "
                        "önemsiz teknik/reklam değişikliği ise sadece 'IGNORE' yaz."
                    )
                    res = await self.agent.llm.generate_simple(eval_prompt, use_smart=False)
                    ans = res.text.strip()
                    if ans and "IGNORE" not in ans.upper():
                        await self._notify(
                            f"🔔 **Web Takip Bildirimi — {w['name']}**\n{w['url']}\n\n{ans}",
                            send_voice=False,
                        )
            except Exception as exc:
                logger.warning("Web takipçisi hata verdi (%s): %s", w.get("name"), exc)
