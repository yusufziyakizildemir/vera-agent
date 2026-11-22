"""İzole çalışma alanı, Python otomasyon motoru, onaylı kabuk ve VPS sağlık izleme araçları."""

from __future__ import annotations

import asyncio
import sys
import tempfile
import time
from pathlib import Path
from typing import Any

import psutil

from vera.tools.registry import ToolContext, ToolDef, ToolRegistry


def _resolve_workspace_file(workspace: Path, relative_path: str) -> Path:
    workspace = workspace.resolve()
    target = (workspace / relative_path).resolve()
    if not str(target).startswith(str(workspace)):
        raise ValueError("Çalışma alanı dışına çıkılamaz.")
    return target


def get_system_health() -> dict[str, Any]:
    mem = psutil.virtual_memory()
    swap = psutil.swap_memory()
    disk = psutil.disk_usage("/")
    boot = psutil.boot_time()
    uptime_hours = round((time.time() - boot) / 3600, 1)
    return {
        "cpu_percent": psutil.cpu_percent(interval=0.2),
        "ram_used_mb": round(mem.used / (1024 * 1024), 1),
        "ram_total_mb": round(mem.total / (1024 * 1024), 1),
        "ram_percent": mem.percent,
        "swap_used_mb": round(swap.used / (1024 * 1024), 1),
        "swap_total_mb": round(swap.total / (1024 * 1024), 1),
        "disk_used_gb": round(disk.used / (1024**3), 2),
        "disk_total_gb": round(disk.total / (1024**3), 2),
        "disk_percent": disk.percent,
        "uptime_hours": uptime_hours,
    }


def register_system_tools(registry: ToolRegistry) -> None:
    async def vps_health_check(ctx: ToolContext) -> dict[str, Any]:
        return await asyncio.to_thread(get_system_health)

    async def workspace_list_files(ctx: ToolContext, subdir: str = "") -> dict[str, Any]:
        base = _resolve_workspace_file(ctx.settings.workspace_dir, subdir or ".")
        if not base.exists():
            return {"files": []}
        items = []
        for p in sorted(base.iterdir())[:100]:
            items.append(
                {
                    "name": str(p.relative_to(ctx.settings.workspace_dir.resolve())),
                    "is_dir": p.is_dir(),
                    "size_bytes": p.stat().st_size if p.is_file() else 0,
                }
            )
        return {"count": len(items), "items": items}

    async def workspace_read_file(
        ctx: ToolContext, path: str, max_chars: int = 12000
    ) -> dict[str, Any]:
        target = _resolve_workspace_file(ctx.settings.workspace_dir, path)
        if not target.exists() or not target.is_file():
            return {"error": f"Dosya bulunamadı: {path}"}
        content = target.read_text(encoding="utf-8", errors="replace")
        return {
            "path": path,
            "content": content[:max_chars],
            "truncated": len(content) > max_chars,
        }

    async def workspace_write_file(
        ctx: ToolContext, path: str, content: str
    ) -> dict[str, Any]:
        target = _resolve_workspace_file(ctx.settings.workspace_dir, path)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
        return {"status": "written", "path": path, "bytes": len(content.encode("utf-8"))}

    async def run_python_sandbox(
        ctx: ToolContext, code: str, timeout_seconds: int = 20
    ) -> dict[str, Any]:
        """Executes a Python script inside VERA's isolated workspace with strict timeout."""
        timeout = max(2, min(45, timeout_seconds))
        ctx.settings.workspace_dir.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(
            mode="w",
            suffix=".py",
            dir=str(ctx.settings.workspace_dir),
            delete=False,
            encoding="utf-8",
        ) as tmp:
            tmp.write(code)
            script_path = Path(tmp.name)

        try:
            proc = await asyncio.create_subprocess_exec(
                sys.executable,
                str(script_path),
                cwd=str(ctx.settings.workspace_dir),
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            try:
                stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout)
            except asyncio.TimeoutError:
                proc.kill()
                return {"error": f"Python scripti {timeout} saniye zaman aşımına uğradı."}
            return {
                "exit_code": proc.returncode,
                "stdout": stdout.decode("utf-8", errors="replace")[:6000],
                "stderr": stderr.decode("utf-8", errors="replace")[:3000],
            }
        finally:
            script_path.unlink(missing_ok=True)

    async def run_shell_command(
        ctx: ToolContext, command: str, timeout_seconds: int = 25
    ) -> dict[str, Any]:
        """Runs a shell command in workspace after explicit user approval."""
        timeout = max(2, min(60, timeout_seconds))
        proc = await asyncio.create_subprocess_shell(
            command,
            cwd=str(ctx.settings.workspace_dir),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout)
        except asyncio.TimeoutError:
            proc.kill()
            return {"error": f"Komut {timeout} saniye zaman aşımına uğradı."}
        return {
            "exit_code": proc.returncode,
            "stdout": stdout.decode("utf-8", errors="replace")[:6000],
            "stderr": stderr.decode("utf-8", errors="replace")[:3000],
        }

    registry.register(
        ToolDef(
            name="vps_health_check",
            category="system",
            description="7/24 bulut sunucunun anlık CPU, RAM, Swap, Disk ve çalışma süresi durumunu raporlar.",
            parameters={"type": "object", "properties": {}, "required": []},
            handler=vps_health_check,
        )
    )
    registry.register(
        ToolDef(
            name="workspace_list_files",
            category="system",
            description="VERA'nın izole çalışma alanındaki dosya ve klasörleri listeler.",
            parameters={
                "type": "object",
                "properties": {"subdir": {"type": "string", "description": "Alt klasör (opsiyonel)."}},
                "required": [],
            },
            handler=workspace_list_files,
        )
    )
    registry.register(
        ToolDef(
            name="workspace_read_file",
            category="system",
            description="VERA çalışma alanındaki bir metin/kod dosyasını okur.",
            parameters={
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "Dosya yolu (örn. rapor.md)."},
                    "max_chars": {"type": "integer", "description": "Maksimum karakter."},
                },
                "required": ["path"],
            },
            handler=workspace_read_file,
        )
    )
    registry.register(
        ToolDef(
            name="workspace_write_file",
            category="system",
            description="VERA çalışma alanına yeni dosya/kod/rapor yazar.",
            parameters={
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "Yazılacak dosya yolu."},
                    "content": {"type": "string", "description": "Dosya içeriği."},
                },
                "required": ["path", "content"],
            },
            handler=workspace_write_file,
        )
    )
    registry.register(
        ToolDef(
            name="run_python_sandbox",
            category="system",
            description=(
                "Hesaplama, veri işleme, grafik/CSV/Excel üretimi veya otomasyon için "
                "VERA'nın izole çalışma alanında Python kodu çalıştırır."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "code": {"type": "string", "description": "Çalıştırılacak Python kodu."},
                    "timeout_seconds": {"type": "integer", "description": "Zaman aşımı (varsayılan 20s)."},
                },
                "required": ["code"],
            },
            handler=run_python_sandbox,
        )
    )
    registry.register(
        ToolDef(
            name="run_shell_command",
            category="system",
            description="Sunucu çalışma alanında terminal/bash komutu çalıştırır (kullanıcı onayı gerektirir).",
            requires_approval=True,
            approval_template="Sunucuda komut çalıştırılacak: `{command}`",
            parameters={
                "type": "object",
                "properties": {
                    "command": {"type": "string", "description": "Çalıştırılacak kabuk komutu."},
                    "timeout_seconds": {"type": "integer", "description": "Zaman aşımı (saniye)."},
                },
                "required": ["command"],
            },
            handler=run_shell_command,
        )
    )
