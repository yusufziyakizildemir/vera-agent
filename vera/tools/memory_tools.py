"""Hermes-tarzı hafıza, profil öğrenme ve not alma araçları."""

from __future__ import annotations

from typing import Any

from vera.tools.registry import ToolContext, ToolDef, ToolRegistry


def register_memory_tools(registry: ToolRegistry) -> None:
    async def remember_fact(
        ctx: ToolContext,
        content: str,
        category: str = "general",
        tags: str = "",
        importance: int = 4,
        also_add_to_memory_md: bool = False,
    ) -> dict[str, Any]:
        promote = "MEMORY.md" if also_add_to_memory_md or importance >= 5 else None
        return await ctx.memory.remember(
            content=content,
            category=category,
            tags=tags,
            importance=importance,
            promote_to_doc=promote,
            llm=ctx.llm,
        )

    async def update_user_profile(
        ctx: ToolContext,
        fact: str,
        section: str = "",
    ) -> dict[str, Any]:
        msg = ctx.memory.append_to_doc("USER.md", fact, section_hint=section)
        await ctx.memory.remember(
            content=fact,
            category="user_profile",
            tags=section or "profile",
            importance=5,
            llm=ctx.llm,
        )
        return {"status": "ok", "message": msg}

    async def search_memory(
        ctx: ToolContext,
        query: str,
        limit: int = 6,
    ) -> dict[str, Any]:
        hits = await ctx.memory.recall(query=query, limit=min(15, max(1, limit)), llm=ctx.llm)
        return {"count": len(hits), "results": hits}

    async def save_note(
        ctx: ToolContext,
        title: str,
        content: str,
        tags: str = "",
    ) -> dict[str, Any]:
        note_id = ctx.db.save_note(title=title, content=content, tags=tags)
        await ctx.memory.remember(
            content=f"Not [{title}]: {content[:400]}",
            category="note",
            tags=tags,
            importance=3,
            llm=ctx.llm,
        )
        return {"status": "saved", "note_id": note_id, "title": title}

    async def list_notes(
        ctx: ToolContext,
        query: str = "",
        limit: int = 10,
    ) -> dict[str, Any]:
        notes = ctx.db.list_notes(query=query, limit=min(30, max(1, limit)))
        return {"count": len(notes), "notes": notes}

    registry.register(
        ToolDef(
            name="remember_fact",
            category="memory",
            description=(
                "Kullanıcıyla ilgili önemli bir bilgiyi, kararı, projeyi veya olayı kalıcı "
                "episodik/semantik hafızaya kaydeder."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "content": {"type": "string", "description": "Hatırlanacak net bilgi."},
                    "category": {
                        "type": "string",
                        "description": "Kategori: work, personal, project, preference, contact, general",
                    },
                    "tags": {"type": "string", "description": "Virgülle ayrılmış etiketler."},
                    "importance": {
                        "type": "integer",
                        "description": "Önem derecesi (1=düşük, 5=çok kritik).",
                    },
                    "also_add_to_memory_md": {
                        "type": "boolean",
                        "description": "Kalıcı MEMORY.md dosyasına da işlensin mi?",
                    },
                },
                "required": ["content"],
            },
            handler=remember_fact,
        )
    )

    registry.register(
        ToolDef(
            name="update_user_profile",
            category="memory",
            description=(
                "Kullanıcının alışkanlıklarını, tercihlerini, mesleğini, rutinlerini veya önemli "
                "kişilerini kalıcı USER.md profil dosyasına ekler."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "fact": {"type": "string", "description": "Kullanıcı profiline eklenecek bilgi."},
                    "section": {
                        "type": "string",
                        "description": "Opsiyonel başlık ipucu (örn. Rutinler & Alışkanlıklar).",
                    },
                },
                "required": ["fact"],
            },
            handler=update_user_profile,
        )
    )

    registry.register(
        ToolDef(
            name="search_memory",
            category="memory",
            description="Geçmiş konuşmalar, notlar ve uzun vadeli hafızada anahtar kelime ve anlam bazlı arama yapar.",
            parameters={
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "Aranacak konu veya soru."},
                    "limit": {"type": "integer", "description": "Maksimum sonuç sayısı (varsayılan 6)."},
                },
                "required": ["query"],
            },
            handler=search_memory,
        )
    )

    registry.register(
        ToolDef(
            name="save_note",
            category="memory",
            description="Başlıklı ve etiketli yapılandırılmış bir not kaydeder.",
            parameters={
                "type": "object",
                "properties": {
                    "title": {"type": "string", "description": "Not başlığı."},
                    "content": {"type": "string", "description": "Not içeriği."},
                    "tags": {"type": "string", "description": "Virgülle ayrılmış etiketler."},
                },
                "required": ["title", "content"],
            },
            handler=save_note,
        )
    )

    registry.register(
        ToolDef(
            name="list_notes",
            category="memory",
            description="Kayıtlı notları listeler veya belirli bir kelimeye göre filtreler.",
            parameters={
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "Opsiyonel arama kelimesi."},
                    "limit": {"type": "integer", "description": "Maksimum not sayısı."},
                },
                "required": [],
            },
            handler=list_notes,
        )
    )
