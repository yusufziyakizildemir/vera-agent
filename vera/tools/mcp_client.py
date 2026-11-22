"""Harici MCP (Model Context Protocol) HTTP/JSON-RPC sunucu köprüsü."""

from __future__ import annotations

import json
from typing import Any

import httpx

from vera.tools.registry import ToolContext, ToolDef, ToolRegistry
from vera.tools.skill_manager import load_mcp_servers_config


def register_mcp_tools(registry: ToolRegistry) -> None:
    async def mcp_list_servers(ctx: ToolContext) -> dict[str, Any]:
        cfg = load_mcp_servers_config(ctx.settings.mcp_config_path)
        servers = cfg.get("servers", {})
        return {"count": len(servers), "servers": list(servers.keys()), "config": servers}

    async def mcp_add_http_server(
        ctx: ToolContext, name: str, url: str, api_key: str = ""
    ) -> dict[str, Any]:
        cfg = load_mcp_servers_config(ctx.settings.mcp_config_path)
        cfg.setdefault("servers", {})[name.strip()] = {
            "transport": "http",
            "url": url.strip(),
            "headers": {"Authorization": f"Bearer {api_key}"} if api_key else {},
        }
        ctx.settings.mcp_config_path.write_text(
            json.dumps(cfg, indent=2, ensure_ascii=False), encoding="utf-8"
        )
        ctx.db.log_event("mcp_server_added", f"{name} -> {url}")
        return {"status": "saved", "name": name, "url": url}

    async def mcp_call_tool(
        ctx: ToolContext,
        server_name: str,
        method_or_tool: str,
        arguments_json: str = "{}",
    ) -> dict[str, Any]:
        """Calls a remote MCP HTTP JSON-RPC server tool (tools/call or tools/list)."""
        cfg = load_mcp_servers_config(ctx.settings.mcp_config_path)
        srv = cfg.get("servers", {}).get(server_name)
        if not srv:
            return {"error": f"MCP sunucusu bulunamadı: {server_name}"}

        args = json.loads(arguments_json or "{}")
        if method_or_tool == "tools/list":
            payload = {"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}}
        else:
            payload = {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "tools/call",
                "params": {"name": method_or_tool, "arguments": args},
            }

        headers = {"Content-Type": "application/json", **(srv.get("headers") or {})}
        async with httpx.AsyncClient(timeout=30.0) as client:
            resp = await client.post(srv["url"], headers=headers, json=payload)
            resp.raise_for_status()
            return resp.json()

    registry.register(
        ToolDef(
            name="mcp_list_servers",
            category="mcp",
            description="VERA'ya bağlı harici MCP sunucularını listeler.",
            parameters={"type": "object", "properties": {}, "required": []},
            handler=mcp_list_servers,
        )
    )
    registry.register(
        ToolDef(
            name="mcp_add_http_server",
            category="mcp",
            description="VERA'ya yeni bir harici HTTP MCP sunucusu kaydeder.",
            parameters={
                "type": "object",
                "properties": {
                    "name": {"type": "string", "description": "Sunucu kısa adı."},
                    "url": {"type": "string", "description": "MCP HTTP endpoint URL'si."},
                    "api_key": {"type": "string", "description": "Opsiyonel Bearer token."},
                },
                "required": ["name", "url"],
            },
            handler=mcp_add_http_server,
        )
    )
    registry.register(
        ToolDef(
            name="mcp_call_tool",
            category="mcp",
            description="Kayıtlı bir harici MCP sunucusundaki aracı çalıştırır veya 'tools/list' ile listeler.",
            parameters={
                "type": "object",
                "properties": {
                    "server_name": {"type": "string", "description": "Kayıtlı MCP sunucu adı."},
                    "method_or_tool": {"type": "string", "description": "Araç adı veya 'tools/list'."},
                    "arguments_json": {"type": "string", "description": "JSON formatında araç parametreleri."},
                },
                "required": ["server_name", "method_or_tool"],
            },
            handler=mcp_call_tool,
        )
    )
