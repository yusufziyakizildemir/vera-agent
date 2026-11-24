"""VERA Bulut Hafıza Senkronizasyonu (Hugging Face Private Dataset — 0 TL Kalıcı Depolama).

Hugging Face Spaces ücretsiz katmanında konteyner yeniden başlatılsa bile:
- Açılışta SOUL.md, USER.md, MEMORY.md, vera.db, google_token.json ve özel skills/ otomatik geri yüklenir.
- Her 10 dakikada, kapanışta ve hafıza güncellemelerinde güvenli SQLite yedeği özel (Private) Dataset deposuna aktarılır.
"""

from __future__ import annotations

import asyncio
import logging
import shutil
import sqlite3
import tempfile
from pathlib import Path
from typing import Any

from vera.config import Settings

logger = logging.getLogger("vera.cloud_sync")


class CloudPersistence:
    def __init__(self, settings: Settings):
        self.settings = settings
        self.enabled = bool(settings.hf_token.strip() and settings.hf_backup_repo.strip())
        self.last_sync_status: str = "devre_disi" if not self.enabled else "bekliyor"

    def _safe_sqlite_copy(self, src_db: Path, dst_db: Path) -> bool:
        """Creates a consistent snapshot even while SQLite WAL is active."""
        if not src_db.exists():
            return False
        dst_db.parent.mkdir(parents=True, exist_ok=True)
        src_conn = sqlite3.connect(str(src_db), timeout=15.0)
        dst_conn = sqlite3.connect(str(dst_db))
        try:
            src_conn.backup(dst_conn)
            return True
        finally:
            dst_conn.close()
            src_conn.close()

    def restore_sync(self) -> dict[str, Any]:
        """Synchronously restores VERA state from Private HF Dataset before DB initialization."""
        if not self.enabled:
            return {"status": "disabled"}

        try:
            from huggingface_hub import HfApi, snapshot_download

            api = HfApi(token=self.settings.hf_token)
            api.create_repo(
                repo_id=self.settings.hf_backup_repo,
                repo_type="dataset",
                private=True,
                exist_ok=True,
            )

            with tempfile.TemporaryDirectory() as tmpdir:
                local_snap = Path(
                    snapshot_download(
                        repo_id=self.settings.hf_backup_repo,
                        repo_type="dataset",
                        token=self.settings.hf_token,
                        local_dir=tmpdir,
                    )
                )

                restored_files = 0
                # Restore memory markdowns
                snap_mem = local_snap / "memory"
                if snap_mem.exists():
                    self.settings.memory_dir.mkdir(parents=True, exist_ok=True)
                    for f in snap_mem.glob("*.md"):
                        shutil.copy2(f, self.settings.memory_dir / f.name)
                        restored_files += 1

                # Restore SQLite DB & Google OAuth token
                snap_db = local_snap / "db" / "vera.db"
                if snap_db.exists() and snap_db.stat().st_size > 0:
                    self.settings.db_path.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(snap_db, self.settings.db_path)
                    restored_files += 1

                snap_gtoken = local_snap / "db" / "google_token.json"
                if snap_gtoken.exists():
                    shutil.copy2(snap_gtoken, self.settings.google_token_path)
                    restored_files += 1

                # Restore custom skills
                snap_skills = local_snap / "skills"
                if snap_skills.exists():
                    for skill_dir in snap_skills.iterdir():
                        if skill_dir.is_dir():
                            dest_dir = self.settings.skills_dir / skill_dir.name
                            shutil.copytree(skill_dir, dest_dir, dirs_exist_ok=True)
                            restored_files += 1

                # Restore voice cloning reference sample (private)
                snap_voice = local_snap / "voice" / "reference.mp3"
                if snap_voice.exists():
                    self.settings.voice_dir.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(snap_voice, self.settings.voice_reference_path)
                    restored_files += 1

            self.last_sync_status = f"geri_yuklendi ({restored_files} oge)"
            logger.info("Bulut yedeği geri yüklendi: %d öğe (%s)", restored_files, self.settings.hf_backup_repo)
            return {"status": "restored", "files": restored_files}
        except Exception as exc:
            self.last_sync_status = f"geri_yukleme_hatasi: {str(exc)[:120]}"
            logger.warning("Bulut geri yükleme atlandı: %s", exc)
            return {"status": "error", "error": str(exc)}

    def backup_sync(self) -> dict[str, Any]:
        """Uploads a clean snapshot of memory docs, SQLite DB, and skills to Private HF Dataset."""
        if not self.enabled:
            return {"status": "disabled"}

        try:
            from huggingface_hub import HfApi

            api = HfApi(token=self.settings.hf_token)
            api.create_repo(
                repo_id=self.settings.hf_backup_repo,
                repo_type="dataset",
                private=True,
                exist_ok=True,
            )

            with tempfile.TemporaryDirectory() as tmpdir:
                stage = Path(tmpdir)
                # 1) Memory docs
                if self.settings.memory_dir.exists():
                    (stage / "memory").mkdir(parents=True, exist_ok=True)
                    for f in self.settings.memory_dir.glob("*.md"):
                        shutil.copy2(f, stage / "memory" / f.name)

                # 2) Safe SQLite backup + optional Google token
                (stage / "db").mkdir(parents=True, exist_ok=True)
                self._safe_sqlite_copy(self.settings.db_path, stage / "db" / "vera.db")
                if self.settings.google_token_path.exists():
                    shutil.copy2(self.settings.google_token_path, stage / "db" / "google_token.json")

                # 3) Skills
                if self.settings.skills_dir.exists():
                    shutil.copytree(self.settings.skills_dir, stage / "skills", dirs_exist_ok=True)

                api.upload_folder(
                    folder_path=str(stage),
                    repo_id=self.settings.hf_backup_repo,
                    repo_type="dataset",
                    commit_message="VERA otomatik hafiza ve veritabani yedegi",
                )

            self.last_sync_status = "senkronize"
            logger.info("VERA bulut yedeği güncellendi -> %s", self.settings.hf_backup_repo)
            return {"status": "backed_up", "repo": self.settings.hf_backup_repo}
        except Exception as exc:
            self.last_sync_status = f"yedek_hatasi: {str(exc)[:120]}"
            logger.warning("VERA bulut yedeği başarısız: %s", exc)
            return {"status": "error", "error": str(exc)}

    async def backup_async(self) -> dict[str, Any]:
        return await asyncio.to_thread(self.backup_sync)
