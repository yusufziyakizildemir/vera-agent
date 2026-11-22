"""Hermes-tarzı dinamik Skill (agentskills.io uyumlu) yükleyici, üretici ve MCP yapılandırması."""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from vera.tools.registry import ToolContext, ToolDef, ToolRegistry


def _slugify(name: str) -> str:
    slug = re.sub(r"[^a-zA-Z0-9_-]+", "-", name.strip().lower()).strip("-")
    return slug[:48] or "custom-skill"


def scan_skills(skills_dir: Path) -> list[dict[str, str]]:
    """Scans skills/<slug>/SKILL.md and returns metadata index."""
    if not skills_dir.exists():
        return []
    found: list[dict[str, str]] = []
    for folder in sorted(skills_dir.iterdir()):
        if not folder.is_dir():
            continue
        skill_md = folder / "SKILL.md"
        if not skill_md.exists():
            continue
        text = skill_md.read_text(encoding="utf-8", errors="replace")
        title = folder.name
        desc = ""
        # Parse lightweight YAML frontmatter if present
        if text.startswith("---"):
            parts = text.split("---", 2)
            if len(parts) >= 3:
                for line in parts[1].splitlines():
                    if line.lower().startswith("name:"):
                        title = line.split(":", 1)[1].strip().strip('"')
                    elif line.lower().startswith("description:"):
                        desc = line.split(":", 1)[1].strip().strip('"')
        if not desc:
            for line in text.splitlines():
                stripped = line.strip().lstrip("#").strip()
                if stripped and not stripped.startswith("---"):
                    desc = stripped[:160]
                    break
        found.append(
            {
                "slug": folder.name,
                "name": title,
                "description": desc,
                "path": str(skill_md),
            }
        )
    return found


def build_skills_prompt_index(skills_dir: Path) -> str:
    skills = scan_skills(skills_dir)
    if not skills:
        return ""
    lines = [
        "İlgili bir iş akışı gerektiğinde `read_skill` ile o yeteneğin ayrıntılı talimatlarını oku; "
        "tekrarlayan yeni bir iş akışı keşfettiğinde `create_skill` ile kalıcı yeni yetenek oluştur:"
    ]
    for s in skills:
        lines.append(f"- `{s['slug']}` ({s['name']}): {s['description']}")
    return "\n".join(lines)


def load_mcp_servers_config(config_path: Path) -> dict[str, Any]:
    if not config_path.exists():
        default = {"servers": {}}
        config_path.parent.mkdir(parents=True, exist_ok=True)
        config_path.write_text(json.dumps(default, indent=2, ensure_ascii=False), encoding="utf-8")
        return default
    try:
        return json.loads(config_path.read_text(encoding="utf-8"))
    except Exception:
        return {"servers": {}}


def register_skill_tools(registry: ToolRegistry) -> None:
    async def list_skills(ctx: ToolContext) -> dict[str, Any]:
        skills = scan_skills(ctx.settings.skills_dir)
        return {"count": len(skills), "skills": skills}

    async def read_skill(ctx: ToolContext, slug: str) -> dict[str, Any]:
        clean = _slugify(slug)
        skill_md = ctx.settings.skills_dir / clean / "SKILL.md"
        if not skill_md.exists():
            return {"error": f"Yetenek bulunamadı: {clean}"}
        return {
            "slug": clean,
            "content": skill_md.read_text(encoding="utf-8", errors="replace"),
        }

    async def create_skill(
        ctx: ToolContext,
        slug: str,
        name: str,
        description: str,
        instructions: str,
    ) -> dict[str, Any]:
        """Hermes autonomous skill creation: persists a new reusable SKILL.md."""
        clean = _slugify(slug or name)
        folder = ctx.settings.skills_dir / clean
        folder.mkdir(parents=True, exist_ok=True)
        skill_md = folder / "SKILL.md"
        body = (
            f"---\n"
            f"name: {name}\n"
            f"description: {description}\n"
            f"---\n\n"
            f"# {name}\n\n"
            f"{instructions.strip()}\n"
        )
        skill_md.write_text(body, encoding="utf-8")
        ctx.db.log_event("skill_created", f"{clean} ({name})")
        return {"status": "created", "slug": clean, "path": str(skill_md)}

    registry.register(
        ToolDef(
            name="list_skills",
            category="skills",
            description="VERA'nın sahip olduğu tüm modüler yetenekleri (SKILL.md) listeler.",
            parameters={"type": "object", "properties": {}, "required": []},
            handler=list_skills,
        )
    )
    registry.register(
        ToolDef(
            name="read_skill",
            category="skills",
            description="Bir yeteneğin (skill) adım adım uygulama rehberini okur.",
            parameters={
                "type": "object",
                "properties": {"slug": {"type": "string", "description": "Yetenek klasör adı (slug)."}},
                "required": ["slug"],
            },
            handler=read_skill,
        )
    )
    registry.register(
        ToolDef(
            name="create_skill",
            category="skills",
            description=(
                "VERA'nın yeni ve tekrar kullanılabilir bir iş akışını kalıcı yetenek (SKILL.md) "
                "olarak kendi kendine öğrenip kaydetmesini sağlar."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "slug": {"type": "string", "description": "Kısa İngilizce/ASCII kimlik (örn. haftalik-rapor)."},
                    "name": {"type": "string", "description": "Yeteneğin görünen adı."},
                    "description": {"type": "string", "description": "Ne zaman kullanılacağının kısa özeti."},
                    "instructions": {"type": "string", "description": "Adım adım markdown uygulama talimatları."},
                },
                "required": ["slug", "name", "description", "instructions"],
            },
            handler=create_skill,
        )
    )
