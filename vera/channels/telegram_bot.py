"""VERA 7/24 Telegram Gateway (Sesli Komut STT/TTS, PDF/Office/Görsel, Inline Onay Butonları)."""

from __future__ import annotations

import io
import logging
from pathlib import Path
from typing import TYPE_CHECKING

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.constants import ChatAction
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

from vera.tools.skill_manager import scan_skills
from vera.tools.system_ops import get_system_health

if TYPE_CHECKING:
    from vera.agent import AgentReply, VeraAgent

logger = logging.getLogger("vera.telegram")


def _split_chunks(text: str, limit: int = 3900) -> list[str]:
    if len(text) <= limit:
        return [text]
    chunks: list[str] = []
    remaining = text
    while remaining:
        if len(remaining) <= limit:
            chunks.append(remaining)
            break
        split_at = remaining.rfind("\n", 0, limit)
        if split_at < limit // 2:
            split_at = limit
        chunks.append(remaining[:split_at])
        remaining = remaining[split_at:].lstrip()
    return chunks


class TelegramGateway:
    """Runs inside FastAPI's asyncio event loop with zero extra container overhead."""

    def __init__(self, agent: VeraAgent):
        self.agent = agent
        self.app: Application | None = None
        self.running = False
        self._voice_mode_override: bool | None = None

    def _is_owner(self, update: Update) -> bool:
        user = update.effective_user
        if not user:
            return False
        owners = self.agent.settings.owner_ids
        if not owners:
            return False
        return user.id in owners

    async def _reject_if_unauthorized(self, update: Update) -> bool:
        if self._is_owner(update):
            return False
        user = update.effective_user
        uid = user.id if user else "unknown"
        if not self.agent.settings.owner_ids:
            if update.effective_message:
                await update.effective_message.reply_text(
                    f"🔒 VERA güvenlik kilidi aktif.\n"
                    f"Senin Telegram ID'n: `{uid}`\n"
                    f"Bu numarayı `.env` dosyasındaki `TELEGRAM_OWNER_ID={uid}` alanına ekleyip "
                    f"VERA'yı yeniden başlattığında yalnızca sana açılacağım.",
                    parse_mode="Markdown",
                )
        else:
            self.agent.db.log_event("telegram_unauthorized", f"Reddedilen kullanıcı ID: {uid}")
            logger.warning("Yetkisiz Telegram erişim denemesi reddedildi: %s", uid)
        return True

    async def start(self) -> None:
        token = self.agent.settings.telegram_bot_token.strip()
        if not token:
            logger.warning("TELEGRAM_BOT_TOKEN boş — Telegram Gateway başlatılmadı (Web Panel aktif).")
            return

        self.app = Application.builder().token(token).build()
        self.app.add_handler(CommandHandler("start", self._cmd_start))
        self.app.add_handler(CommandHandler("status", self._cmd_status))
        self.app.add_handler(CommandHandler("memory", self._cmd_memory))
        self.app.add_handler(CommandHandler("profile", self._cmd_profile))
        self.app.add_handler(CommandHandler("skills", self._cmd_skills))
        self.app.add_handler(CommandHandler("jobs", self._cmd_jobs))
        self.app.add_handler(CommandHandler("briefing", self._cmd_briefing))
        self.app.add_handler(CommandHandler("dream", self._cmd_dream))
        self.app.add_handler(CommandHandler("voice", self._cmd_voice))
        self.app.add_handler(CommandHandler("clear", self._cmd_clear))
        self.app.add_handler(CallbackQueryHandler(self._on_callback))
        self.app.add_handler(MessageHandler(filters.VOICE | filters.AUDIO, self._on_voice))
        self.app.add_handler(MessageHandler(filters.PHOTO, self._on_photo))
        self.app.add_handler(MessageHandler(filters.Document.ALL, self._on_document))
        self.app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, self._on_text))

        await self.app.initialize()
        await self.app.start()
        if self.app.updater:
            await self.app.updater.start_polling(drop_pending_updates=True)
        self.running = True
        self.agent.proactive_sender = self.send_proactive
        logger.info("VERA Telegram Gateway 7/24 aktif!")

    async def stop(self) -> None:
        if self.app and self.running:
            if self.app.updater:
                await self.app.updater.stop()
            await self.app.stop()
            await self.app.shutdown()
            self.running = False
            logger.info("VERA Telegram Gateway durduruldu.")

    async def send_proactive(self, text: str, send_voice: bool = False) -> None:
        if not self.app or not self.running:
            return
        for owner_id in self.agent.settings.owner_ids:
            for chunk in _split_chunks(text):
                try:
                    await self.app.bot.send_message(chat_id=owner_id, text=chunk, parse_mode="Markdown")
                except Exception:
                    await self.app.bot.send_message(chat_id=owner_id, text=chunk)
            if send_voice:
                try:
                    ogg = await self.agent.voice.synthesize_telegram_ogg(text)
                    bio = io.BytesIO(ogg)
                    bio.name = "vera_reply.ogg"
                    await self.app.bot.send_voice(chat_id=owner_id, voice=bio)
                except Exception as exc:
                    logger.warning("Proaktif sesli yanıt üretilemedi: %s", exc)

    async def _send_reply(
        self, update: Update, reply: AgentReply, speak: bool = False
    ) -> None:
        msg = update.effective_message
        if not msg:
            return

        for chunk in _split_chunks(reply.text):
            try:
                await msg.reply_text(chunk, parse_mode="Markdown")
            except Exception:
                await msg.reply_text(chunk)

        if speak:
            try:
                await msg.chat.send_action(ChatAction.RECORD_VOICE)
                ogg = await self.agent.voice.synthesize_telegram_ogg(reply.text)
                bio = io.BytesIO(ogg)
                bio.name = "vera.ogg"
                await msg.reply_voice(voice=bio)
            except Exception as exc:
                logger.warning("Sesli yanıt gönderilemedi: %s", exc)

        for ap in reply.pending_approvals:
            kb = InlineKeyboardMarkup(
                [
                    [
                        InlineKeyboardButton("✅ Onayla", callback_data=f"apr:ok:{ap.id}"),
                        InlineKeyboardButton("❌ İptal Et", callback_data=f"apr:no:{ap.id}"),
                    ]
                ]
            )
            await msg.reply_text(
                f"⚠️ **Onay Bekleyen Kritik İşlem**\n\n{ap.summary}",
                reply_markup=kb,
                parse_mode="Markdown",
            )

    # --- Slash Commands ---
    async def _cmd_start(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if await self._reject_if_unauthorized(update):
            return
        await update.effective_message.reply_text(
            "👋 Merhaba Yusuf! Ben **VERA** — 7/24 çalışan kişisel yapay zeka asistanın.\n\n"
            "Bana yazabilir, **Türkçe sesli mesaj** atabilir, **PDF / PowerPoint / Word / Excel / fotoğraf** "
            "gönderebilir veya şu komutları kullanabilirsin:\n"
            "• `/status` — Sunucu (CPU/RAM/Swap) & istatistikler\n"
            "• `/profile` — Seni nasıl tanıdığımı gör (`USER.md`)\n"
            "• `/memory` — Uzun vadeli hafızam (`MEMORY.md`)\n"
            "• `/skills` — Modüler yeteneklerim\n"
            "• `/jobs` — 7/24 hatırlatıcı ve zamanlanmış görevler\n"
            "• `/briefing` — Anında Sabah Brifingi al\n"
            "• `/dream` — Gece hafıza konsolidasyonunu şimdi çalıştır\n"
            "• `/voice` — Her mesaja sesli yanıt modunu aç/kapat\n"
            "• `/clear` — Kısa dönem sohbet geçmişini sıfırla (kalıcı hafıza korunur)",
            parse_mode="Markdown",
        )

    async def _cmd_status(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if await self._reject_if_unauthorized(update):
            return
        h = get_system_health()
        s = self.agent.db.get_usage_stats()
        await update.effective_message.reply_text(
            f"📊 **VERA 7/24 Sistem Durumu**\n\n"
            f"• **CPU:** %{h['cpu_percent']}\n"
            f"• **RAM:** {h['ram_used_mb']} / {h['ram_total_mb']} MB (%{h['ram_percent']})\n"
            f"• **Swap:** {h['swap_used_mb']} / {h['swap_total_mb']} MB\n"
            f"• **Disk:** {h['disk_used_gb']} / {h['disk_total_gb']} GB (%{h['disk_percent']})\n"
            f"• **Uptime:** {h['uptime_hours']} saat\n\n"
            f"🧠 **Hafıza:** {s['memories_count']} anı | 💬 {s['messages_count']} mesaj\n"
            f"⏰ **Aktif Görevler:** {s['active_jobs_count']} | ⚠️ **Bekleyen Onay:** {s['pending_approvals_count']}",
            parse_mode="Markdown",
        )

    async def _cmd_memory(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if await self._reject_if_unauthorized(update):
            return
        doc = self.agent.memory.read_doc("MEMORY.md")
        await update.effective_message.reply_text(doc[:3800])

    async def _cmd_profile(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if await self._reject_if_unauthorized(update):
            return
        doc = self.agent.memory.read_doc("USER.md")
        await update.effective_message.reply_text(doc[:3800])

    async def _cmd_skills(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if await self._reject_if_unauthorized(update):
            return
        skills = scan_skills(self.agent.settings.skills_dir)
        tools = self.agent.registry.list_tools()
        lines = [f"🧩 **Yetenekler ({len(skills)}) & Araçlar ({len(tools)})**\n"]
        for sk in skills:
            lines.append(f"• `{sk['slug']}` — {sk['description']}")
        await update.effective_message.reply_text("\n".join(lines), parse_mode="Markdown")

    async def _cmd_jobs(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if await self._reject_if_unauthorized(update):
            return
        jobs = self.agent.db.list_scheduled_jobs()
        if not jobs:
            await update.effective_message.reply_text("Aktif zamanlanmış görev yok.")
            return
        lines = ["⏰ **7/24 Zamanlanmış Görevler**\n"]
        for j in jobs:
            state = "🟢" if j["enabled"] else "⚪"
            lines.append(f"{state} `{j['id']}` — **{j['name']}** (`{j['schedule_expr']}`)")
        await update.effective_message.reply_text("\n".join(lines), parse_mode="Markdown")

    async def _cmd_briefing(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if await self._reject_if_unauthorized(update):
            return
        await update.effective_message.chat.send_action(ChatAction.TYPING)
        briefing = await self.agent.generate_morning_briefing()
        await update.effective_message.reply_text(briefing[:3900])

    async def _cmd_dream(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if await self._reject_if_unauthorized(update):
            return
        await update.effective_message.reply_text("🌙 Hafıza konsolidasyonu (Dreaming) çalıştırılıyor...")
        res = await self.agent.memory.consolidate_dream(self.agent.llm, hours=48)
        await update.effective_message.reply_text(
            f"✨ Konsolidasyon tamamlandı:\n{res}"
        )

    async def _cmd_voice(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if await self._reject_if_unauthorized(update):
            return
        self._voice_mode_override = not bool(self._voice_mode_override)
        state = "AÇIK (yazılı mesajlara da sesli döneceğim)" if self._voice_mode_override else "NORMAL (sesli mesajlara sesli döneceğim)"
        await update.effective_message.reply_text(f"🎙️ Sesli yanıt modu: **{state}**", parse_mode="Markdown")

    async def _cmd_clear(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if await self._reject_if_unauthorized(update):
            return
        sid = f"tg_{update.effective_chat.id}"
        count = self.agent.db.clear_session_messages(sid)
        await update.effective_message.reply_text(
            f"🧹 Kısa dönem sohbet geçmişi temizlendi ({count} mesaj). Kalıcı hafızan (`USER.md` / `MEMORY.md`) korundu."
        )

    # --- Message Handlers ---
    async def _on_text(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if await self._reject_if_unauthorized(update):
            return
        msg = update.effective_message
        if not msg or not msg.text:
            return
        await msg.chat.send_action(ChatAction.TYPING)
        sid = f"tg_{update.effective_chat.id}"
        reply = await self.agent.handle_message(
            user_text=msg.text, session_id=sid, channel="telegram"
        )
        await self._send_reply(update, reply, speak=bool(self._voice_mode_override))

    async def _on_voice(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if await self._reject_if_unauthorized(update):
            return
        msg = update.effective_message
        if not msg:
            return
        await msg.chat.send_action(ChatAction.TYPING)
        media = msg.voice or msg.audio
        if not media:
            return
        tg_file = await media.get_file()
        buf = io.BytesIO()
        await tg_file.download_to_memory(buf)
        audio_bytes = buf.getvalue()
        mime = getattr(media, "mime_type", None) or "audio/ogg"
        filename = "voice.ogg" if msg.voice else (getattr(media, "file_name", None) or "audio.mp3")

        try:
            transcript = await self.agent.voice.transcribe(audio_bytes, filename=filename, mime_type=mime)
        except Exception as exc:
            await msg.reply_text(f"🎙️ Ses çözümlenemedi: {exc}")
            return

        await msg.reply_text(f"🎙️ _\"{transcript}\"_", parse_mode="Markdown")
        await msg.chat.send_action(ChatAction.TYPING)
        sid = f"tg_{update.effective_chat.id}"
        reply = await self.agent.handle_message(
            user_text=transcript, session_id=sid, channel="telegram"
        )
        await self._send_reply(update, reply, speak=self.agent.settings.tts_reply_to_voice)

    async def _on_photo(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if await self._reject_if_unauthorized(update):
            return
        msg = update.effective_message
        if not msg or not msg.photo:
            return
        await msg.chat.send_action(ChatAction.TYPING)
        tg_file = await msg.photo[-1].get_file()
        buf = io.BytesIO()
        await tg_file.download_to_memory(buf)
        caption = msg.caption or "Bu görseli detaylı incele ve önemli noktaları Türkçe özetle."
        sid = f"tg_{update.effective_chat.id}"
        reply = await self.agent.handle_message(
            user_text=caption,
            session_id=sid,
            channel="telegram",
            image_attachments=[{"bytes": buf.getvalue(), "mime_type": "image/jpeg"}],
        )
        await self._send_reply(update, reply, speak=bool(self._voice_mode_override))

    async def _on_document(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if await self._reject_if_unauthorized(update):
            return
        msg = update.effective_message
        if not msg or not msg.document:
            return
        await msg.chat.send_action(ChatAction.TYPING)
        doc = msg.document
        safe_name = Path(doc.file_name or "belge.bin").name
        dest = self.agent.settings.uploads_dir / safe_name
        tg_file = await doc.get_file()
        await tg_file.download_to_drive(custom_path=str(dest))
        caption = msg.caption or f"Bu dosyayı ({safe_name}) analiz et ve önemli maddeleri özetle."
        sid = f"tg_{update.effective_chat.id}"
        reply = await self.agent.handle_message(
            user_text=caption,
            session_id=sid,
            channel="telegram",
            file_paths=[dest],
        )
        await self._send_reply(update, reply, speak=bool(self._voice_mode_override))

    async def _on_callback(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        query = update.callback_query
        if not query or not query.data:
            return
        if not self._is_owner(update):
            await query.answer("Yetkisiz işlem.", show_alert=True)
            return
        parts = query.data.split(":")
        if len(parts) == 3 and parts[0] == "apr":
            decision = parts[1] == "ok"
            aid = parts[2]
            await query.answer("İşleniyor...")
            res = await self.agent.resolve_approval(aid, approved=decision)
            icon = "✅" if decision and res.get("ok") else "❌"
            await query.edit_message_text(f"{icon} {res.get('message', '')}")
