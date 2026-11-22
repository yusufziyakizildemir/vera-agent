"""VERA Deklaratif Araç Kayıt Sistemi (Gemini & OpenRouter/OpenAI çift şema uyumlu)."""

from __future__ import annotations

import inspect
import logging
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Awaitable, Callable

if TYPE_CHECKING:
    from vera.config import Settings
    from vera.database import Database
    from vera.llm import LLMRouter
    from vera.memory import MemoryManager
    from vera.scheduler import VeraScheduler

logger = logging.getLogger("vera.tools")


@dataclass
class ToolContext:
    settings: Settings
    db: Database
    memory: MemoryManager
    llm: LLMRouter
    scheduler: VeraScheduler | None = None
    session_id: str = "default"
    channel: str = "telegram"
    notify_callback: Callable[[str], Awaitable[None]] | None = None


@dataclass
class ToolDef:
    name: str
    description: str
    parameters: dict[str, Any]
    handler: Callable[..., Awaitable[Any] | Any]
    category: str = "general"
    requires_approval: bool = False
    approval_template: str = ""
    enabled: bool = True

    def format_approval_summary(self, args: dict[str, Any]) -> str:
        if self.approval_template:
            try:
                return self.approval_template.format(**args)
            except Exception:
                pass
        pairs = ", ".join(f"{k}={str(v)[:120]}" for k, v in args.items())
        return f"`{self.name}` aracı çalıştırılacak ({pairs})"

    def to_gemini_declaration(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "description": self.description,
            "parameters": _upper_schema_types(self.parameters),
        }

    def to_openai_tool(self) -> dict[str, Any]:
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": self.parameters,
            },
        }


def _upper_schema_types(schema: dict[str, Any]) -> dict[str, Any]:
    """Converts JSON Schema types to Gemini uppercase enum style cleanly."""
    out: dict[str, Any] = {}
    for k, v in schema.items():
        if k == "type" and isinstance(v, str):
            out[k] = v.upper()
        elif k == "properties" and isinstance(v, dict):
            out[k] = {pk: _upper_schema_types(pv) for pk, pv in v.items()}
        elif k == "items" and isinstance(v, dict):
            out[k] = _upper_schema_types(v)
        else:
            out[k] = v
    return out


class ToolRegistry:
    def __init__(self) -> None:
        self._tools: dict[str, ToolDef] = {}

    def register(self, tool: ToolDef) -> None:
        self._tools[tool.name] = tool

    def get(self, name: str) -> ToolDef | None:
        return self._tools.get(name)

    def list_tools(self, only_enabled: bool = True) -> list[ToolDef]:
        items = list(self._tools.values())
        if only_enabled:
            items = [t for t in items if t.enabled]
        return sorted(items, key=lambda x: (x.category, x.name))

    def set_enabled(self, name: str, enabled: bool) -> bool:
        tool = self._tools.get(name)
        if not tool:
            return False
        tool.enabled = enabled
        return True

    def gemini_declarations(self) -> list[dict[str, Any]]:
        return [t.to_gemini_declaration() for t in self.list_tools(only_enabled=True)]

    def openai_tools(self) -> list[dict[str, Any]]:
        return [t.to_openai_tool() for t in self.list_tools(only_enabled=True)]

    async def execute(self, name: str, args: dict[str, Any], ctx: ToolContext) -> Any:
        tool = self._tools.get(name)
        if not tool or not tool.enabled:
            return {"error": f"Araç bulunamadı veya devre dışı: {name}"}
        try:
            res = tool.handler(ctx=ctx, **(args or {}))
            if inspect.isawaitable(res):
                res = await res
            return res
        except TypeError as exc:
            logger.warning("Araç parametre hatası (%s): %s", name, exc)
            return {"error": f"Parametre hatası ({name}): {exc}"}
        except Exception as exc:
            logger.exception("Araç çalıştırma hatası (%s): %s", name, exc)
            return {"error": f"Çalıştırma hatası ({name}): {exc}"}
