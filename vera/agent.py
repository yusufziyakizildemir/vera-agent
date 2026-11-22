"""VERA Otonom ReAct Ajan Çekirdeği (Çok Adımlı Araç Yürütme + Onay Kapısı + Hafıza)."""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any, Awaitable, Callable

from vera.cloud_sync import CloudPersistence
from vera.config import Settings
from vera.database import Database
from vera.llm import LLMRouter
from vera.memory import MemoryManager
from vera.tools.documents import extract_document_text, register_document_tools
from vera.tools.google_workspace import register_google_workspace_tools
from vera.tools.mcp_client import register_mcp_tools
from vera.tools.memory_tools import register_memory_tools
from vera.tools.registry import ToolContext, ToolRegistry
from vera.tools.scheduler_tools import register_scheduler_tools
from vera.tools.skill_manager import build_skills_prompt_index, register_skill_tools
from vera.tools.system_ops import register_system_tools
from vera.tools.web_tools import register_web_tools
from vera.voice import VoiceEngine

if TYPE_CHECKING:
    from vera.scheduler import VeraScheduler

logger = logging.getLogger("vera.agent")


@dataclass
class PendingApprovalInfo:
    id: str
    tool_name: str
    summary: str
    args: dict[str, Any]


@dataclass
class AgentReply:
    text: str
    tools_used: list[str] = field(default_factory=list)
    pending_approvals: list[PendingApprovalInfo] = field(default_factory=list)
    model: str = ""


def build_default_registry() -> ToolRegistry:
    registry = ToolRegistry()
    register_memory_tools(registry)
    register_web_tools(registry)
    register_document_tools(registry)
    register_google_workspace_tools(registry)
    register_system_tools(registry)
    register_scheduler_tools(registry)
    register_skill_tools(registry)
    register_mcp_tools(registry)
    return registry


class VeraAgent:
    """Central autonomous agent combining Hermes memory + OpenClaw tool/skill execution."""

    def __init__(self, settings: Settings):
        self.settings = settings
        self.cloud_sync = CloudPersistence(settings)
        # Restore persisted memory/DB/skills from Private HF Dataset before opening SQLite
        self.cloud_sync.restore_sync()
        self.db = Database(settings.db_path)
        self.memory = MemoryManager(settings, self.db)
        self.llm = LLMRouter(settings, self.db)
        self.voice = VoiceEngine(settings, self.llm)
        self.registry = build_default_registry()
        self.scheduler: VeraScheduler | None = None
        self.proactive_sender: Callable[[str, bool], Awaitable[None]] | None = None

    def make_tool_context(self, session_id: str, channel: str) -> ToolContext:
        return ToolContext(
            settings=self.settings,
            db=self.db,
            memory=self.memory,
            llm=self.llm,
            scheduler=self.scheduler,
            session_id=session_id,
            channel=channel,
        )

    def _should_use_smart_model(self, user_text: str) -> bool:
        """Selects smart model for deep reasoning, planning, coding, or long analysis."""
        lower = user_text.lower()
        triggers = (
            "planla", "strateji", "analiz et", "kod yaz", "mimari", "rapor hazırla",
            "karşılaştır", "özetle", "detaylı", "derinlemesine", "hata ayıkla",
        )
        return len(user_text) > 500 or any(t in lower for t in triggers)

    async def handle_message(
        self,
        user_text: str,
        session_id: str = "main",
        channel: str = "telegram",
        image_attachments: list[dict[str, Any]] | None = None,
        file_paths: list[Path] | None = None,
        persist_history: bool = True,
    ) -> AgentReply:
        """Processes a user message through memory recall, multi-step tools, and safety gates."""
        enriched_text = user_text.strip()

        # Automatically extract text from attached documents (PDF, PPTX, DOCX, XLSX, etc.)
        if file_paths:
            doc_blocks = []
            for fp in file_paths:
                extracted = extract_document_text(fp, max_chars=14000)
                if "text" in extracted and extracted["text"]:
                    doc_blocks.append(
                        f"[Yüklenen Belge: {fp.name}]\n{extracted['text']}"
                    )
                else:
                    doc_blocks.append(
                        f"[Yüklenen Dosya: {fp.name} — metin çıkarılamadı: {extracted.get('error', '')}]"
                    )
            if doc_blocks:
                enriched_text = (enriched_text + "\n\n" + "\n\n".join(doc_blocks)).strip()

        if persist_history:
            self.db.add_message(session_id, channel, "user", enriched_text[:12000])

        # 1) Recall relevant episodic memories
        recalled = await self.memory.recall(query=user_text[:300], limit=6, llm=self.llm)
        skills_index = build_skills_prompt_index(self.settings.skills_dir)
        system_prompt = self.memory.build_system_prompt(
            recalled_memories=recalled,
            skills_index=skills_index,
            channel=channel,
        )

        # 2) Build recent conversation window
        history = self.db.get_recent_messages(session_id, limit=16) if persist_history else []
        messages: list[dict[str, Any]] = [
            {"role": m["role"], "content": m["content"]} for m in history
        ]
        if not persist_history:
            messages.append({"role": "user", "content": enriched_text})

        use_smart = self._should_use_smart_model(user_text)
        tools_gemini = self.registry.gemini_declarations()
        tools_openai = self.registry.openai_tools()
        ctx = self.make_tool_context(session_id=session_id, channel=channel)

        tools_used: list[str] = []
        pending_approvals: list[PendingApprovalInfo] = []
        final_text = ""
        last_model = ""

        # 3) Autonomous ReAct multi-step loop
        for step in range(self.settings.max_agent_steps):
            resp = await self.llm.chat(
                system_prompt=system_prompt,
                messages=messages,
                tools_gemini=tools_gemini,
                tools_openai=tools_openai,
                use_smart=use_smart,
                attachments=image_attachments if step == 0 else None,
            )
            last_model = resp.model

            if not resp.tool_calls:
                final_text = resp.text or "İşlem tamamlandı."
                break

            # Record assistant tool-call turn
            messages.append(
                {
                    "role": "assistant_tool_calls",
                    "text": resp.text,
                    "raw_parts": resp.raw_assistant_parts,
                    "tool_calls": [
                        {"id": tc.id, "name": tc.name, "args": tc.args}
                        for tc in resp.tool_calls
                    ],
                }
            )

            for tc in resp.tool_calls:
                tool_def = self.registry.get(tc.name)
                if tool_def and tool_def.requires_approval:
                    summary = tool_def.format_approval_summary(tc.args)
                    aid = self.db.create_approval(
                        session_id=session_id,
                        channel=channel,
                        tool_name=tc.name,
                        args=tc.args,
                        summary=summary,
                    )
                    pending_approvals.append(
                        PendingApprovalInfo(
                            id=aid, tool_name=tc.name, summary=summary, args=tc.args
                        )
                    )
                    tool_result: Any = {
                        "status": "awaiting_user_approval",
                        "approval_id": aid,
                        "summary": summary,
                        "note": "Bu işlem kritik olduğu için kullanıcı onayına sunuldu. Kullanıcıya netçe bilgi ver.",
                    }
                else:
                    tools_used.append(tc.name)
                    tool_result = await self.registry.execute(tc.name, tc.args, ctx)
                    self.db.log_event(
                        "tool_exec",
                        f"{tc.name}({json.dumps(tc.args, ensure_ascii=False)[:300]})",
                    )

                messages.append(
                    {
                        "role": "tool",
                        "tool_call_id": tc.id,
                        "name": tc.name,
                        "content": tool_result,
                    }
                )
        else:
            final_text = final_text or "Maksimum adım sınırına ulaştım; mevcut bulgularla devam etmemi ister misin?"

        if persist_history:
            self.db.add_message(
                session_id,
                channel,
                "assistant",
                final_text[:12000],
                metadata={"tools_used": tools_used, "model": last_model},
            )

        return AgentReply(
            text=final_text,
            tools_used=tools_used,
            pending_approvals=pending_approvals,
            model=last_model,
        )

    async def resolve_approval(self, approval_id: str, approved: bool) -> dict[str, Any]:
        """Executes or rejects a pending sensitive tool action."""
        item = self.db.get_approval(approval_id)
        if not item:
            return {"ok": False, "message": "Onay kaydı bulunamadı."}
        if item["status"] != "pending":
            return {"ok": False, "message": f"Bu onay zaten sonuçlandırılmış ({item['status']})."}

        if not approved:
            self.db.resolve_approval(approval_id, "rejected", "Kullanıcı tarafından iptal edildi.")
            self.db.log_event("approval_rejected", f"{approval_id}: {item['tool_name']}")
            return {"ok": True, "status": "rejected", "message": "İşlem iptal edildi."}

        ctx = self.make_tool_context(session_id=item["session_id"], channel=item["channel"])
        result = await self.registry.execute(item["tool_name"], item["args"], ctx)
        res_str = json.dumps(result, ensure_ascii=False, indent=2)
        self.db.resolve_approval(approval_id, "approved", res_str[:2000])
        self.db.log_event("approval_executed", f"{approval_id}: {item['tool_name']}")
        return {
            "ok": True,
            "status": "approved",
            "tool_name": item["tool_name"],
            "result": result,
            "message": f"`{item['tool_name']}` başarıyla çalıştırıldı.",
        }

    async def generate_morning_briefing(self) -> str:
        prompt = (
            "Günaydın! Benim için bugünün Sabah Brifingini hazırla.\n"
            "1) `get_weather` ile İstanbul (veya USER.md'de kayıtlı şehrim) hava durumunu kontrol et.\n"
            "2) Google bağlıysa `calendar_list_events(days_ahead=1)` ile bugünkü toplantıları ve "
            "`gmail_list_messages(query='is:unread', max_results=5)` ile önemli okunmamış e-postaları özetle "
            "(bağlı değilse bunu atla, hata gibi gösterme).\n"
            "3) `list_scheduled_jobs` ve `list_web_watchers` ile bugünkü hatırlatmaları/takipleri kontrol et.\n"
            "4) Kısa, enerjik, düzenli ve motive edici bir Türkçe sabah özeti sun."
        )
        reply = await self.handle_message(
            user_text=prompt,
            session_id="proactive_briefing",
            channel="scheduler",
            persist_history=False,
        )
        return reply.text
