"""VERA Hermes-Tarzı Çok Katmanlı Hafıza & Gece Konsolidasyon ("Dreaming") Motoru."""

from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Any

from vera.config import Settings
from vera.database import Database

if TYPE_CHECKING:
    from vera.llm import LLMRouter

logger = logging.getLogger("vera.memory")

DEFAULT_FILES: dict[str, str] = {
    "SOUL.md": (
        "# VERA — Karakter ve İlkeler\n\n"
        "- Adın VERA. Kullanıcını tanıyan, 7/24 çalışan, akıllı ve proaktif kişisel yapay zeka asistanısın.\n"
        "- Doğal, net, samimi Türkçe konuş.\n"
        "- Kritik işlemlerde (mail gönderme, silme, sistem değişikliği) onay al.\n"
    ),
    "USER.md": (
        "# Kullanıcı Profili\n\n"
        "- **İsim:** Yusuf\n"
        "- **Saat Dilimi:** Europe/Istanbul\n"
        "- **Tercihler:** Zamanla konuşmalardan otomatik öğrenilecek.\n"
    ),
    "MEMORY.md": (
        "# Uzun Vadeli Küratörlü Hafıza\n\n"
        "- Aktif projeler, kararlar ve kalıcı notlar burada saklanır.\n"
    ),
}


class MemoryManager:
    """Manages SOUL.md, USER.md, MEMORY.md + SQLite FTS5/Vector episodic memory + Dreaming loop."""

    def __init__(self, settings: Settings, db: Database):
        self.settings = settings
        self.db = db
        self.memory_dir = settings.memory_dir
        self._ensure_memory_files()

    def _ensure_memory_files(self) -> None:
        self.memory_dir.mkdir(parents=True, exist_ok=True)
        for filename, default_content in DEFAULT_FILES.items():
            path = self.memory_dir / filename
            if not path.exists():
                path.write_text(default_content, encoding="utf-8")

    def _safe_path(self, name: str) -> Path:
        allowed = {"SOUL.md", "USER.md", "MEMORY.md"}
        clean = Path(name).name
        if clean not in allowed:
            raise ValueError(f"İzin verilmeyen hafıza dosyası: {name}")
        return self.memory_dir / clean

    def read_doc(self, name: str) -> str:
        path = self._safe_path(name)
        if not path.exists():
            return DEFAULT_FILES.get(path.name, "")
        return path.read_text(encoding="utf-8")

    def write_doc(self, name: str, content: str) -> None:
        path = self._safe_path(name)
        budget = self.settings.memory_char_budget
        trimmed = content.strip()
        if len(trimmed) > budget:
            trimmed = trimmed[:budget] + "\n\n*(Bütçe limiti nedeniyle kısaltıldı)*"
        path.write_text(trimmed + "\n", encoding="utf-8")
        self.db.log_event("memory_doc_updated", f"{path.name} güncellendi ({len(trimmed)} karakter)")

    def append_to_doc(self, name: str, bullet: str, section_hint: str = "") -> str:
        current = self.read_doc(name)
        clean_bullet = bullet.strip().lstrip("- ").strip()
        if not clean_bullet:
            return "Boş kayıt atlandı."
        if clean_bullet.lower() in current.lower():
            return f"{name} içinde benzer bilgi zaten mevcut."

        stamp = datetime.now(self.settings.tz).strftime("%Y-%m-%d")
        line = f"- [{stamp}] {clean_bullet}"
        if section_hint and f"## {section_hint}" in current:
            updated = current.replace(
                f"## {section_hint}", f"## {section_hint}\n{line}", 1
            )
        else:
            updated = current.rstrip() + f"\n{line}\n"
        self.write_doc(name, updated)
        return f"{name} güncellendi: {clean_bullet}"

    async def remember(
        self,
        content: str,
        category: str = "general",
        tags: str = "",
        importance: int = 3,
        promote_to_doc: str | None = None,
        llm: LLMRouter | None = None,
    ) -> dict[str, Any]:
        embedding = None
        if llm is not None:
            try:
                embedding = await llm.embed(content)
            except Exception as exc:
                logger.debug("Embedding atlandı (FTS5 ile devam): %s", exc)

        entry_id = self.db.add_memory_entry(
            category=category,
            content=content,
            tags=tags,
            importance=importance,
            embedding=embedding,
        )
        promoted_msg = ""
        if promote_to_doc in {"USER.md", "MEMORY.md"}:
            promoted_msg = self.append_to_doc(promote_to_doc, content)

        return {
            "id": entry_id,
            "category": category,
            "promoted": promoted_msg,
            "has_embedding": embedding is not None,
        }

    async def recall(
        self, query: str, limit: int = 6, llm: LLMRouter | None = None
    ) -> list[dict[str, Any]]:
        query_embedding = None
        if llm is not None and query.strip():
            try:
                query_embedding = await llm.embed(query)
            except Exception:
                query_embedding = None
        return self.db.search_memory(query=query, query_embedding=query_embedding, limit=limit)

    def build_system_prompt(
        self,
        recalled_memories: list[dict[str, Any]] | None = None,
        skills_index: str = "",
        channel: str = "telegram",
    ) -> str:
        now_str = datetime.now(self.settings.tz).strftime("%Y-%m-%d %H:%M (%A)")
        soul = self.read_doc("SOUL.md")
        user_profile = self.read_doc("USER.md")
        curated = self.read_doc("MEMORY.md")

        recalled_block = ""
        if recalled_memories:
            lines = [
                f"- ({m['category']}, önem:{m['importance']}) {m['content']}"
                for m in recalled_memories
            ]
            recalled_block = "\n\n## İlgili Hatırlanan Anılar (Episodik Hafıza)\n" + "\n".join(lines)

        skills_block = ""
        if skills_index:
            skills_block = f"\n\n## Yüklü Modüler Yetenekler (Skills)\n{skills_index}"

        channel_hint = (
            "Kullanıcıyla Telegram üzerinden konuşuyorsun. Net, mobil dostu ve okunaklı yaz."
            if channel == "telegram"
            else "Kullanıcıyla VERA Web/Ses paneli üzerinden konuşuyorsun."
        )

        return (
            f"{soul}\n\n"
            f"---\n"
            f"## Anlık Bağlam\n"
            f"- Şu anki tarih/saat ({self.settings.timezone}): {now_str}\n"
            f"- Kanal: {channel_hint}\n"
            f"- Kullanıcı kendisiyle ilgili yeni bir tercih, alışkanlık, kişi, hedef veya önemli bilgi paylaştığında "
            f"`remember_fact` veya `update_user_profile` aracını kullanarak bunu proaktif biçimde kalıcı hafızana kaydet.\n\n"
            f"---\n"
            f"{user_profile}\n\n"
            f"---\n"
            f"{curated}"
            f"{recalled_block}"
            f"{skills_block}"
        )

    async def consolidate_dream(self, llm: LLMRouter, hours: int = 24) -> dict[str, Any]:
        """Hermes-style nightly memory consolidation ('Dreaming') loop."""
        since = (datetime.now(timezone.utc) - timedelta(hours=hours)).isoformat()
        recent_msgs = self.db.get_messages_since(since, limit=120)
        if not recent_msgs:
            return {"status": "skipped", "reason": "Son dönemde işlenecek yeni konuşma yok."}

        transcript = "\n".join(
            f"[{m['created_at'][:16]}] {m['role']}: {m['content'][:500]}"
            for m in recent_msgs
        )
        current_user = self.read_doc("USER.md")
        current_mem = self.read_doc("MEMORY.md")

        prompt = (
            "Sen VERA'nın gece hafıza konsolidasyon (Dreaming) modülüsün.\n"
            "Aşağıdaki son sohbet kayıtlarını incele ve sadece KALICI değeri olan yeni bilgileri çıkar.\n"
            "Kesinlikle geçici selamlaşmaları veya tek seferlik önemsiz şeyleri ekleme.\n\n"
            f"MEVCUT USER.md:\n{current_user}\n\n"
            f"MEVCUT MEMORY.md:\n{current_mem}\n\n"
            f"SON KONUŞMALAR:\n{transcript}\n\n"
            "Sadece geçerli bir JSON nesnesi döndür:\n"
            "{\n"
            '  "user_updates": ["Kullanıcı hakkında yeni kalıcı tercih/bilgi..."],\n'
            '  "memory_updates": ["Aktif proje, karar veya kalıcı bilgi..."],\n'
            '  "episodic_facts": [{"category": "work|personal|preference|project", "content": "...", "tags": "etiket1,etiket2", "importance": 4}],\n'
            '  "summary": "1-2 cümlelik özet"\n'
            "}"
        )

        resp = await llm.generate_simple(prompt, use_smart=False)
        raw = resp.text.strip()
        # Strip markdown fences if present
        if raw.startswith("```"):
            raw = raw.split("\n", 1)[-1]
            if raw.endswith("```"):
                raw = raw[:-3]
        try:
            parsed = json.loads(raw.strip())
        except Exception as exc:
            logger.warning("Dream konsolidasyon JSON ayrıştırılamadı: %s", exc)
            return {"status": "parse_error", "raw": raw[:400]}

        added_user = 0
        for item in parsed.get("user_updates", [])[:6]:
            if isinstance(item, str) and item.strip():
                self.append_to_doc("USER.md", item)
                added_user += 1

        added_mem = 0
        for item in parsed.get("memory_updates", [])[:6]:
            if isinstance(item, str) and item.strip():
                self.append_to_doc("MEMORY.md", item)
                added_mem += 1

        added_ep = 0
        for fact in parsed.get("episodic_facts", [])[:10]:
            if isinstance(fact, dict) and fact.get("content"):
                await self.remember(
                    content=str(fact["content"]),
                    category=str(fact.get("category", "general")),
                    tags=str(fact.get("tags", "")),
                    importance=int(fact.get("importance", 3)),
                    llm=llm,
                )
                added_ep += 1

        summary = str(parsed.get("summary", "Gece hafıza konsolidasyonu tamamlandı."))
        self.db.log_event(
            "dream_consolidation",
            f"user:+{added_user}, memory:+{added_mem}, episodic:+{added_ep} | {summary}",
        )
        return {
            "status": "completed",
            "user_updates": added_user,
            "memory_updates": added_mem,
            "episodic_facts": added_ep,
            "summary": summary,
        }
