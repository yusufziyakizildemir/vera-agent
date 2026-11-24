"""VERA FastAPI Sunucusu, JWT Güvenlik, Web Panel REST API & Gemini Live WebSocket."""

from __future__ import annotations

import base64
import logging
import secrets
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import jwt
from fastapi import (
    Depends,
    FastAPI,
    File,
    Form,
    HTTPException,
    Query,
    UploadFile,
    WebSocket,
    status,
)
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from vera import __version__
from vera.agent import VeraAgent
from vera.channels.telegram_bot import TelegramGateway
from vera.config import Settings, get_settings
from vera.live_voice import run_gemini_live_bridge
from vera.scheduler import VeraScheduler
from vera.tools.google_workspace import (
    exchange_google_code,
    get_google_auth_url,
    is_google_connected,
)
from vera.tools.skill_manager import scan_skills
from vera.tools.system_ops import get_system_health

logger = logging.getLogger("vera.api")
WEB_DIR = Path(__file__).resolve().parent / "web"
security = HTTPBearer(auto_error=False)


class LoginRequest(BaseModel):
    password: str


class ChatRequest(BaseModel):
    message: str
    session_id: str = "web_main"
    voice: str | None = None
    include_audio: bool = False


class MemoryDocUpdate(BaseModel):
    content: str


class SkillCreateRequest(BaseModel):
    slug: str
    name: str
    description: str
    instructions: str


class JobCreateRequest(BaseModel):
    name: str
    job_type: str = "agent_cron"
    schedule_expr: str
    prompt: str = ""
    message: str = ""
    send_voice: bool = False


class WatcherCreateRequest(BaseModel):
    name: str
    url: str
    instruction: str
    selector: str = ""
    interval_minutes: int = 60


class ApprovalDecisionRequest(BaseModel):
    approved: bool


def create_app(settings: Settings | None = None, start_background: bool = True) -> FastAPI:
    cfg = settings or get_settings()
    agent = VeraAgent(cfg)
    scheduler = VeraScheduler(agent)
    agent.scheduler = scheduler
    tg_gateway = TelegramGateway(agent)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        if start_background:
            scheduler.start()
            await tg_gateway.start()
        yield
        if start_background:
            await tg_gateway.stop()
            scheduler.stop()
            if agent.cloud_sync.enabled:
                await agent.cloud_sync.backup_async()

    app = FastAPI(title="VERA Personal AI Agent", version=__version__, lifespan=lifespan)
    app.state.agent = agent
    app.state.scheduler = scheduler
    app.state.telegram = tg_gateway
    app.state.settings = cfg

    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    def _create_token() -> str:
        exp = datetime.now(timezone.utc) + timedelta(days=14)
        return jwt.encode({"sub": "owner", "exp": exp}, cfg.jwt_secret, algorithm="HS256")

    def _verify_token(token: str) -> bool:
        try:
            payload = jwt.decode(token, cfg.jwt_secret, algorithms=["HS256"])
            return payload.get("sub") == "owner"
        except Exception:
            return False

    def require_auth(
        creds: HTTPAuthorizationCredentials | None = Depends(security),
    ) -> str:
        if not creds or not _verify_token(creds.credentials):
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Oturum doğrulanamadı. Lütfen VERA paneline giriş yapın.",
            )
        return "owner"

    # --- Public Health & Auth ---
    @app.get("/api/ping")
    async def ping() -> dict[str, Any]:
        return {"status": "ok", "agent": cfg.agent_name, "version": __version__}

    @app.post("/api/auth/login")
    async def login(body: LoginRequest) -> dict[str, Any]:
        if not secrets.compare_digest(body.password, cfg.admin_password):
            raise HTTPException(status_code=401, detail="Hatalı parola.")
        return {"token": _create_token(), "agent": cfg.agent_name}

    # --- Chat & Multimodal Upload ---
    @app.get("/api/chat/history")
    async def chat_history(
        session_id: str = "web_main",
        limit: int = 40,
        _=Depends(require_auth),
    ) -> dict[str, Any]:
        return {"messages": agent.db.get_recent_messages(session_id, limit=min(100, limit))}

    @app.post("/api/chat")
    async def chat_send(body: ChatRequest, _=Depends(require_auth)) -> dict[str, Any]:
        reply = await agent.handle_message(
            user_text=body.message, session_id=body.session_id, channel="web"
        )
        audio_b64 = ""
        if body.include_audio and reply.text:
            try:
                mp3 = await agent.voice.synthesize_mp3(reply.text, voice=body.voice)
                audio_b64 = base64.b64encode(mp3).decode("ascii")
            except Exception as exc:
                logger.warning("Inline TTS üretilemedi: %s", exc)
        return {
            "reply": reply.text,
            "tools_used": reply.tools_used,
            "model": reply.model,
            "audio_b64": audio_b64,
            "pending_approvals": [
                {"id": a.id, "tool_name": a.tool_name, "summary": a.summary, "args": a.args}
                for a in reply.pending_approvals
            ],
        }

    @app.post("/api/chat/upload")
    async def chat_upload(
        file: UploadFile = File(...),
        message: str = Form(default="Bu dosyayı analiz et ve özetle."),
        session_id: str = Form(default="web_main"),
        _=Depends(require_auth),
    ) -> dict[str, Any]:
        safe_name = Path(file.filename or "upload.bin").name
        raw = await file.read()
        mime = file.content_type or ""
        if mime.startswith("image/"):
            reply = await agent.handle_message(
                user_text=message,
                session_id=session_id,
                channel="web",
                image_attachments=[{"bytes": raw, "mime_type": mime}],
            )
        else:
            dest = cfg.uploads_dir / safe_name
            dest.write_bytes(raw)
            reply = await agent.handle_message(
                user_text=message,
                session_id=session_id,
                channel="web",
                file_paths=[dest],
            )
        return {"reply": reply.text, "tools_used": reply.tools_used, "filename": safe_name}

    @app.post("/api/voice/tts")
    async def voice_tts(body: ChatRequest, _=Depends(require_auth)) -> Response:
        mp3 = await agent.voice.synthesize_mp3(body.message, voice=body.voice)
        return Response(content=mp3, media_type="audio/mpeg")

    @app.post("/api/voice/converse")
    async def voice_converse(
        file: UploadFile = File(...),
        session_id: str = Form(default="voice_jarvis"),
        voice: str = Form(default=""),
        _=Depends(require_auth),
    ) -> dict[str, Any]:
        raw = await file.read()
        raw_mime = (file.content_type or "audio/webm").split(";")[0].strip()
        if raw_mime not in {"audio/webm", "audio/mp4", "audio/ogg", "audio/wav", "audio/mpeg", "audio/aac"}:
            raw_mime = "audio/webm"
        fname = file.filename or "speech.webm"

        transcript = ""
        if cfg.groq_api_key:
            try:
                transcript = await agent.voice.transcribe(raw, filename=fname, mime_type=raw_mime)
            except Exception:
                transcript = ""

        if transcript.strip():
            reply = await agent.handle_message(
                user_text=transcript, session_id=session_id, channel="live_voice"
            )
        else:
            # Single-pass Gemini native multimodal audio -> tool execution + spoken Turkish reply!
            reply = await agent.handle_message(
                user_text=(
                    "Kullanıcının gönderdiği bu Türkçe ses kaydını doğrudan dinle. "
                    "Eğer kayıtta anlaşılır bir konuşma yoksa sadece 'Sizi duyamadım Yusuf Bey, tekrar eder misiniz?' de. "
                    "Konuşma varsa gerekiyorsa araçları çalıştır ve 1-2 kısa cümlelik çok seri Türkçe yanıt ver."
                ),
                session_id=session_id,
                channel="live_voice",
                image_attachments=[{"bytes": raw, "mime_type": raw_mime}],
            )

        audio_b64 = ""
        if reply.text:
            try:
                mp3 = await agent.voice.synthesize_mp3(reply.text, voice=voice or None)
                audio_b64 = base64.b64encode(mp3).decode("ascii")
            except Exception as exc:
                logger.warning("Converse inline TTS hatası: %s", exc)

        return {
            "transcript": transcript,
            "reply": reply.text,
            "tools_used": reply.tools_used,
            "model": reply.model,
            "audio_b64": audio_b64,
        }

    # --- Gemini Live Real-Time Bidirectional Voice WebSocket ---
    @app.websocket("/ws/live")
    async def ws_live_voice(websocket: WebSocket, token: str = Query(default="")) -> None:
        if not _verify_token(token):
            await websocket.close(code=4401)
            return
        await websocket.accept()
        await run_gemini_live_bridge(websocket, agent, session_id="live_voice")

    # --- Hermes Memory Center ---
    @app.get("/api/memory/docs")
    async def get_all_memory_docs(_=Depends(require_auth)) -> dict[str, str]:
        return {
            "SOUL.md": agent.memory.read_doc("SOUL.md"),
            "USER.md": agent.memory.read_doc("USER.md"),
            "MEMORY.md": agent.memory.read_doc("MEMORY.md"),
        }

    @app.put("/api/memory/docs/{doc_name}")
    async def update_memory_doc(
        doc_name: str, body: MemoryDocUpdate, _=Depends(require_auth)
    ) -> dict[str, str]:
        try:
            agent.memory.write_doc(doc_name, body.content)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc))
        if agent.cloud_sync.enabled:
            await agent.cloud_sync.backup_async()
        return {"status": "saved", "doc": doc_name}

    @app.get("/api/memory/entries")
    async def list_memory_entries(q: str = "", _=Depends(require_auth)) -> dict[str, Any]:
        if q.strip():
            items = await agent.memory.recall(q, limit=20, llm=agent.llm)
        else:
            items = agent.db.list_recent_memories(limit=40)
        return {"items": items}

    @app.delete("/api/memory/entries/{entry_id}")
    async def delete_memory_entry(entry_id: int, _=Depends(require_auth)) -> dict[str, Any]:
        return {"deleted": agent.db.delete_memory_entry(entry_id)}

    @app.post("/api/memory/dream")
    async def trigger_dream(_=Depends(require_auth)) -> dict[str, Any]:
        return await agent.memory.consolidate_dream(agent.llm, hours=48)

    # --- Skills & Tools ---
    @app.get("/api/skills")
    async def list_all_skills(_=Depends(require_auth)) -> dict[str, Any]:
        skills = scan_skills(cfg.skills_dir)
        tools = [
            {
                "name": t.name,
                "category": t.category,
                "description": t.description,
                "requires_approval": t.requires_approval,
                "enabled": t.enabled,
            }
            for t in agent.registry.list_tools(only_enabled=False)
        ]
        return {"skills": skills, "tools": tools}

    @app.post("/api/skills")
    async def create_new_skill(body: SkillCreateRequest, _=Depends(require_auth)) -> dict[str, Any]:
        ctx = agent.make_tool_context("web_admin", "web")
        return await agent.registry.execute(
            "create_skill",
            body.model_dump(),
            ctx,
        )

    @app.post("/api/tools/{tool_name}/toggle")
    async def toggle_tool(tool_name: str, enabled: bool = True, _=Depends(require_auth)) -> dict[str, Any]:
        ok = agent.registry.set_enabled(tool_name, enabled)
        if not ok:
            raise HTTPException(status_code=404, detail="Araç bulunamadı.")
        return {"tool": tool_name, "enabled": enabled}

    # --- 7/24 Scheduled Jobs & Watchers ---
    @app.get("/api/jobs")
    async def get_jobs(_=Depends(require_auth)) -> dict[str, Any]:
        return {
            "jobs": agent.db.list_scheduled_jobs(),
            "watchers": agent.db.list_web_watchers(),
        }

    @app.post("/api/jobs")
    async def create_job(body: JobCreateRequest, _=Depends(require_auth)) -> dict[str, Any]:
        payload = {"prompt": body.prompt, "message": body.message, "send_voice": body.send_voice}
        jid = agent.db.upsert_scheduled_job(
            name=body.name,
            job_type=body.job_type,
            schedule_expr=body.schedule_expr,
            payload=payload,
        )
        scheduler.sync_jobs()
        return {"id": jid, "status": "created"}

    @app.delete("/api/jobs/{job_id}")
    async def remove_job(job_id: str, _=Depends(require_auth)) -> dict[str, Any]:
        ok = agent.db.delete_scheduled_job(job_id)
        scheduler.sync_jobs()
        return {"deleted": ok}

    @app.post("/api/briefing/run")
    async def run_briefing_now(_=Depends(require_auth)) -> dict[str, Any]:
        text = await agent.generate_morning_briefing()
        return {"briefing": text}

    @app.post("/api/watchers")
    async def create_watcher(body: WatcherCreateRequest, _=Depends(require_auth)) -> dict[str, Any]:
        wid = agent.db.add_web_watcher(
            name=body.name,
            url=body.url,
            instruction=body.instruction,
            selector=body.selector,
            interval_minutes=body.interval_minutes,
        )
        return {"id": wid, "status": "created"}

    @app.delete("/api/watchers/{watcher_id}")
    async def remove_watcher(watcher_id: str, _=Depends(require_auth)) -> dict[str, Any]:
        return {"deleted": agent.db.delete_web_watcher(watcher_id)}

    # --- Approvals ---
    @app.get("/api/approvals")
    async def list_approvals(status_filter: str | None = None, _=Depends(require_auth)) -> dict[str, Any]:
        return {"approvals": agent.db.list_approvals(status=status_filter)}

    @app.post("/api/approvals/{approval_id}")
    async def resolve_approval_endpoint(
        approval_id: str, body: ApprovalDecisionRequest, _=Depends(require_auth)
    ) -> dict[str, Any]:
        return await agent.resolve_approval(approval_id, approved=body.approved)

    # --- Google Workspace OAuth ---
    @app.get("/api/google/status")
    async def google_status(_=Depends(require_auth)) -> dict[str, Any]:
        return {
            "connected": is_google_connected(cfg),
            "configured": bool(cfg.google_client_id and cfg.google_client_secret),
        }

    @app.get("/api/google/connect")
    async def google_connect(_=Depends(require_auth)) -> dict[str, str]:
        try:
            return {"auth_url": get_google_auth_url(cfg)}
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc))

    @app.get("/api/google/callback")
    async def google_callback(code: str = Query(default="")) -> Response:
        if not code:
            return HTMLResponse("<h3>Google yetkilendirme kodu alınamadı.</h3>", status_code=400)
        try:
            exchange_google_code(cfg, code)
            agent.db.log_event("google_oauth", "Google Workspace başarıyla bağlandı.")
            if agent.cloud_sync.enabled:
                await agent.cloud_sync.backup_async()
            return RedirectResponse(url="/?google=connected")
        except Exception as exc:
            return HTMLResponse(f"<h3>Google bağlantı hatası: {exc}</h3>", status_code=400)

    # --- System Health & Logs ---
    @app.get("/api/system/status")
    async def system_status(_=Depends(require_auth)) -> dict[str, Any]:
        return {
            "agent": cfg.agent_name,
            "version": __version__,
            "health": get_system_health(),
            "stats": agent.db.get_usage_stats(),
            "integrations": {
                "telegram_running": tg_gateway.running,
                "gemini_configured": bool(cfg.gemini_api_key),
                "groq_configured": bool(cfg.groq_api_key),
                "openrouter_configured": bool(cfg.openrouter_api_key),
                "google_connected": is_google_connected(cfg),
                "cloud_backup_enabled": agent.cloud_sync.enabled,
                "cloud_backup_status": agent.cloud_sync.last_sync_status,
            },
            "models": {
                "fast": cfg.gemini_fast_model,
                "smart": cfg.gemini_smart_model,
                "live": cfg.gemini_live_model,
                "tts_voice": cfg.tts_voice,
            },
        }

    @app.post("/api/system/backup")
    async def trigger_cloud_backup(_=Depends(require_auth)) -> dict[str, Any]:
        return await agent.cloud_sync.backup_async()

    @app.get("/api/logs")
    async def get_logs(limit: int = 50, _=Depends(require_auth)) -> dict[str, Any]:
        return {"logs": agent.db.list_audit_logs(limit=min(200, limit))}

    # --- Static PWA Frontend ---
    if WEB_DIR.exists():
        app.mount("/static", StaticFiles(directory=str(WEB_DIR)), name="static")

        @app.get("/manifest.json")
        async def manifest() -> Response:
            return Response(
                content=(WEB_DIR / "manifest.json").read_text(encoding="utf-8"),
                media_type="application/manifest+json",
            )

        @app.get("/sw.js")
        async def service_worker() -> Response:
            return Response(
                content=(WEB_DIR / "sw.js").read_text(encoding="utf-8"),
                media_type="application/javascript",
                headers={"Cache-Control": "no-store, no-cache, must-revalidate"},
            )

        @app.get("/", response_class=HTMLResponse)
        async def index() -> Response:
            return HTMLResponse(
                content=(WEB_DIR / "index.html").read_text(encoding="utf-8"),
                headers={"Cache-Control": "no-store, no-cache, must-revalidate"},
            )

    return app
