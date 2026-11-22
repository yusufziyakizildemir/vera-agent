"""VERA merkezi yapılandırma yönetimi."""

from __future__ import annotations

import secrets
from functools import lru_cache
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


BASE_DIR = Path(__file__).resolve().parent.parent


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=str(BASE_DIR / ".env"),
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # Identity & Locale
    agent_name: str = Field(default="VERA")
    timezone: str = Field(default="Europe/Istanbul")
    language: str = Field(default="tr")

    # Telegram
    telegram_bot_token: str = Field(default="")
    telegram_owner_id: str = Field(default="")

    # LLM Providers
    gemini_api_key: str = Field(default="")
    gemini_fast_model: str = Field(default="gemini-3.8-flash")
    gemini_smart_model: str = Field(default="gemini-3.8-flash")
    gemini_live_model: str = Field(default="gemini-3.8-flash")
    gemini_embed_model: str = Field(default="gemini-embedding-001")

    openrouter_api_key: str = Field(default="")
    openrouter_fallback_model: str = Field(
        default="meta-llama/llama-3.3-70b-instruct:free"
    )

    # Voice
    groq_api_key: str = Field(default="")
    tts_voice: str = Field(default="tr-TR-EmelNeural")
    tts_reply_to_voice: bool = Field(default=True)

    # Web Control Panel & Security
    web_host: str = Field(default="0.0.0.0")
    web_port: int = Field(default=7860)
    admin_password: str = Field(default="vera-admin-degistir")
    jwt_secret: str = Field(default_factory=lambda: secrets.token_urlsafe(32))
    public_base_url: str = Field(default="http://localhost:7860")
    render_external_url: str = Field(default="")

    # Hugging Face Spaces Private Dataset Persistence (0 TL Kalıcı Hafıza Yedeği)
    hf_token: str = Field(default="")
    hf_backup_repo: str = Field(default="")
    hf_sync_interval_minutes: int = Field(default=10)

    def model_post_init(self, __context: Any) -> None:
        if self.render_external_url and self.public_base_url.startswith("http://localhost"):
            self.public_base_url = self.render_external_url.rstrip("/")

    # Google Workspace OAuth2
    google_client_id: str = Field(default="")
    google_client_secret: str = Field(default="")

    # Hardware & Proactive Schedules
    enable_playwright: bool = Field(default=False)
    dream_schedule_time: str = Field(default="03:30")
    morning_briefing_time: str = Field(default="08:30")
    max_agent_steps: int = Field(default=10)
    memory_char_budget: int = Field(default=8000)

    # Storage paths
    data_dir: Path = Field(default=BASE_DIR / "data")
    skills_dir: Path = Field(default=BASE_DIR / "skills")
    workspace_dir: Path = Field(default=BASE_DIR / "workspace")

    @property
    def memory_dir(self) -> Path:
        return self.data_dir / "memory"

    @property
    def db_path(self) -> Path:
        return self.data_dir / "db" / "vera.db"

    @property
    def uploads_dir(self) -> Path:
        return self.data_dir / "uploads"

    @property
    def google_token_path(self) -> Path:
        return self.data_dir / "db" / "google_token.json"

    @property
    def mcp_config_path(self) -> Path:
        return self.data_dir / "mcp_servers.json"

    @property
    def tz(self) -> ZoneInfo:
        try:
            return ZoneInfo(self.timezone)
        except Exception:
            return ZoneInfo("UTC")

    @property
    def owner_ids(self) -> set[int]:
        ids: set[int] = set()
        for raw in self.telegram_owner_id.replace(";", ",").split(","):
            raw = raw.strip()
            if raw.isdigit():
                ids.add(int(raw))
        return ids

    def ensure_dirs(self) -> None:
        for path in (
            self.data_dir,
            self.memory_dir,
            self.db_path.parent,
            self.uploads_dir,
            self.skills_dir,
            self.workspace_dir,
        ):
            path.mkdir(parents=True, exist_ok=True)


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    settings = Settings()
    settings.ensure_dirs()
    return settings
