"""Google Workspace (Gmail, Takvim, Drive, Sheets, Docs) OAuth2 entegrasyonu."""

from __future__ import annotations

import asyncio
import base64
import json
from datetime import datetime, timedelta
from email.mime.text import MIMEText
from pathlib import Path
from typing import Any

from vera.config import Settings
from vera.tools.registry import ToolContext, ToolDef, ToolRegistry

GOOGLE_SCOPES = [
    "https://www.googleapis.com/auth/gmail.modify",
    "https://www.googleapis.com/auth/gmail.send",
    "https://www.googleapis.com/auth/calendar",
    "https://www.googleapis.com/auth/drive.readonly",
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/documents",
]


def _client_config(settings: Settings) -> dict[str, Any]:
    redirect_uri = f"{settings.public_base_url.rstrip('/')}/api/google/callback"
    return {
        "web": {
            "client_id": settings.google_client_id,
            "client_secret": settings.google_client_secret,
            "auth_uri": "https://accounts.google.com/o/oauth2/auth",
            "token_uri": "https://oauth2.googleapis.com/token",
            "redirect_uris": [redirect_uri],
        }
    }


def get_google_auth_url(settings: Settings) -> str:
    if not settings.google_client_id or not settings.google_client_secret:
        raise ValueError("GOOGLE_CLIENT_ID ve GOOGLE_CLIENT_SECRET .env dosyasında tanımlı değil.")
    from google_auth_oauthlib.flow import Flow

    redirect_uri = f"{settings.public_base_url.rstrip('/')}/api/google/callback"
    flow = Flow.from_client_config(
        _client_config(settings),
        scopes=GOOGLE_SCOPES,
        redirect_uri=redirect_uri,
    )
    auth_url, _ = flow.authorization_url(
        access_type="offline",
        include_granted_scopes="true",
        prompt="consent",
    )
    return auth_url


def exchange_google_code(settings: Settings, code: str) -> None:
    from google_auth_oauthlib.flow import Flow

    redirect_uri = f"{settings.public_base_url.rstrip('/')}/api/google/callback"
    flow = Flow.from_client_config(
        _client_config(settings),
        scopes=GOOGLE_SCOPES,
        redirect_uri=redirect_uri,
    )
    flow.fetch_token(code=code)
    creds = flow.credentials
    settings.google_token_path.parent.mkdir(parents=True, exist_ok=True)
    settings.google_token_path.write_text(creds.to_json(), encoding="utf-8")


def is_google_connected(settings: Settings) -> bool:
    return settings.google_token_path.exists()


def _load_credentials(settings: Settings):
    from google.auth.transport.requests import Request
    from google.oauth2.credentials import Credentials

    token_path: Path = settings.google_token_path
    if not token_path.exists():
        return None
    creds = Credentials.from_authorized_user_file(str(token_path), GOOGLE_SCOPES)
    if creds and creds.expired and creds.refresh_token:
        creds.refresh(Request())
        token_path.write_text(creds.to_json(), encoding="utf-8")
    return creds


def _require_service(settings: Settings, api: str, version: str):
    creds = _load_credentials(settings)
    if not creds:
        raise RuntimeError(
            "Google Workspace henüz bağlı değil. VERA Web Panel -> Entegrasyonlar -> "
            "'Google Hesabını Bağla' butonundan tek tıkla yetkilendirin."
        )
    from googleapiclient.discovery import build

    return build(api, version, credentials=creds, cache_discovery=False)


def register_google_workspace_tools(registry: ToolRegistry) -> None:
    async def gmail_list_messages(
        ctx: ToolContext, query: str = "is:unread", max_results: int = 8
    ) -> dict[str, Any]:
        def _run() -> dict[str, Any]:
            svc = _require_service(ctx.settings, "gmail", "v1")
            res = svc.users().messages().list(
                userId="me", q=query, maxResults=min(20, max(1, max_results))
            ).execute()
            messages = []
            for m in res.get("messages", []):
                detail = svc.users().messages().get(
                    userId="me",
                    id=m["id"],
                    format="metadata",
                    metadataHeaders=["From", "Subject", "Date"],
                ).execute()
                headers = {
                    h["name"].lower(): h["value"]
                    for h in detail.get("payload", {}).get("headers", [])
                }
                messages.append(
                    {
                        "id": m["id"],
                        "from": headers.get("from", ""),
                        "subject": headers.get("subject", "(Konu yok)"),
                        "date": headers.get("date", ""),
                        "snippet": detail.get("snippet", ""),
                    }
                )
            return {"query": query, "count": len(messages), "messages": messages}

        return await asyncio.to_thread(_run)

    async def gmail_read_message(ctx: ToolContext, message_id: str) -> dict[str, Any]:
        def _run() -> dict[str, Any]:
            svc = _require_service(ctx.settings, "gmail", "v1")
            msg = svc.users().messages().get(userId="me", id=message_id, format="full").execute()
            headers = {
                h["name"].lower(): h["value"]
                for h in msg.get("payload", {}).get("headers", [])
            }
            body_text = ""

            def _extract_parts(part: dict[str, Any]) -> str:
                mime = part.get("mimeType", "")
                data = part.get("body", {}).get("data")
                if mime == "text/plain" and data:
                    return base64.urlsafe_b64decode(data).decode("utf-8", errors="replace")
                for sub in part.get("parts", []) or []:
                    found = _extract_parts(sub)
                    if found:
                        return found
                return ""

            body_text = _extract_parts(msg.get("payload", {})) or msg.get("snippet", "")
            return {
                "id": message_id,
                "from": headers.get("from", ""),
                "to": headers.get("to", ""),
                "subject": headers.get("subject", ""),
                "date": headers.get("date", ""),
                "body": body_text[:8000],
            }

        return await asyncio.to_thread(_run)

    async def gmail_create_draft(
        ctx: ToolContext, to: str, subject: str, body: str
    ) -> dict[str, Any]:
        def _run() -> dict[str, Any]:
            svc = _require_service(ctx.settings, "gmail", "v1")
            mime = MIMEText(body, "plain", "utf-8")
            mime["to"] = to
            mime["subject"] = subject
            raw = base64.urlsafe_b64encode(mime.as_bytes()).decode("utf-8")
            draft = svc.users().drafts().create(userId="me", body={"message": {"raw": raw}}).execute()
            return {"status": "draft_created", "draft_id": draft.get("id"), "to": to, "subject": subject}

        return await asyncio.to_thread(_run)

    async def gmail_send_email(
        ctx: ToolContext, to: str, subject: str, body: str
    ) -> dict[str, Any]:
        def _run() -> dict[str, Any]:
            svc = _require_service(ctx.settings, "gmail", "v1")
            mime = MIMEText(body, "plain", "utf-8")
            mime["to"] = to
            mime["subject"] = subject
            raw = base64.urlsafe_b64encode(mime.as_bytes()).decode("utf-8")
            sent = svc.users().messages().send(userId="me", body={"raw": raw}).execute()
            return {"status": "sent", "message_id": sent.get("id"), "to": to, "subject": subject}

        return await asyncio.to_thread(_run)

    async def calendar_list_events(
        ctx: ToolContext, days_ahead: int = 7, max_results: int = 15
    ) -> dict[str, Any]:
        def _run() -> dict[str, Any]:
            svc = _require_service(ctx.settings, "calendar", "v3")
            now = datetime.now(ctx.settings.tz)
            time_min = now.isoformat()
            time_max = (now + timedelta(days=max(1, min(60, days_ahead)))).isoformat()
            res = svc.events().list(
                calendarId="primary",
                timeMin=time_min,
                timeMax=time_max,
                maxResults=min(30, max(1, max_results)),
                singleEvents=True,
                orderBy="startTime",
            ).execute()
            events = []
            for ev in res.get("items", []):
                events.append(
                    {
                        "id": ev.get("id"),
                        "summary": ev.get("summary", "(Başlıksız)"),
                        "start": ev.get("start", {}).get("dateTime") or ev.get("start", {}).get("date"),
                        "end": ev.get("end", {}).get("dateTime") or ev.get("end", {}).get("date"),
                        "location": ev.get("location", ""),
                        "description": (ev.get("description") or "")[:300],
                    }
                )
            return {"count": len(events), "events": events}

        return await asyncio.to_thread(_run)

    async def calendar_create_event(
        ctx: ToolContext,
        summary: str,
        start_iso: str,
        end_iso: str,
        description: str = "",
        location: str = "",
    ) -> dict[str, Any]:
        def _run() -> dict[str, Any]:
            svc = _require_service(ctx.settings, "calendar", "v3")
            body = {
                "summary": summary,
                "location": location,
                "description": description,
                "start": {"dateTime": start_iso, "timeZone": ctx.settings.timezone},
                "end": {"dateTime": end_iso, "timeZone": ctx.settings.timezone},
            }
            created = svc.events().insert(calendarId="primary", body=body).execute()
            return {
                "status": "created",
                "id": created.get("id"),
                "summary": summary,
                "link": created.get("htmlLink", ""),
            }

        return await asyncio.to_thread(_run)

    async def calendar_delete_event(ctx: ToolContext, event_id: str) -> dict[str, Any]:
        def _run() -> dict[str, Any]:
            svc = _require_service(ctx.settings, "calendar", "v3")
            svc.events().delete(calendarId="primary", eventId=event_id).execute()
            return {"status": "deleted", "event_id": event_id}

        return await asyncio.to_thread(_run)

    async def drive_search_files(
        ctx: ToolContext, query: str, max_results: int = 10
    ) -> dict[str, Any]:
        def _run() -> dict[str, Any]:
            svc = _require_service(ctx.settings, "drive", "v3")
            safe_q = query.replace("'", "\\'")
            res = svc.files().list(
                q=f"name contains '{safe_q}' and trashed = false",
                pageSize=min(25, max(1, max_results)),
                fields="files(id, name, mimeType, modifiedTime, webViewLink)",
            ).execute()
            return {"count": len(res.get("files", [])), "files": res.get("files", [])}

        return await asyncio.to_thread(_run)

    async def sheets_read_range(
        ctx: ToolContext, spreadsheet_id: str, range_a1: str = "A1:Z50"
    ) -> dict[str, Any]:
        def _run() -> dict[str, Any]:
            svc = _require_service(ctx.settings, "sheets", "v4")
            res = svc.spreadsheets().values().get(
                spreadsheetId=spreadsheet_id, range=range_a1
            ).execute()
            values = res.get("values", [])
            return {"range": res.get("range"), "row_count": len(values), "values": values}

        return await asyncio.to_thread(_run)

    async def sheets_append_row(
        ctx: ToolContext, spreadsheet_id: str, range_a1: str, values_json: str
    ) -> dict[str, Any]:
        def _run() -> dict[str, Any]:
            svc = _require_service(ctx.settings, "sheets", "v4")
            row = json.loads(values_json)
            if not isinstance(row, list):
                raise ValueError("values_json bir JSON dizisi olmalı (örn. [\"2026-11-22\", \"150 TL\"])")
            res = svc.spreadsheets().values().append(
                spreadsheetId=spreadsheet_id,
                range=range_a1,
                valueInputOption="USER_ENTERED",
                body={"values": [row]},
            ).execute()
            return {"status": "appended", "updates": res.get("updates", {})}

        return await asyncio.to_thread(_run)

    async def docs_create_document(
        ctx: ToolContext, title: str, content: str
    ) -> dict[str, Any]:
        def _run() -> dict[str, Any]:
            svc = _require_service(ctx.settings, "docs", "v1")
            doc = svc.documents().create(body={"title": title}).execute()
            doc_id = doc["documentId"]
            if content:
                svc.documents().batchUpdate(
                    documentId=doc_id,
                    body={"requests": [{"insertText": {"location": {"index": 1}, "text": content}}]},
                ).execute()
            return {
                "status": "created",
                "document_id": doc_id,
                "url": f"https://docs.google.com/document/d/{doc_id}/edit",
            }

        return await asyncio.to_thread(_run)

    registry.register(
        ToolDef(
            name="gmail_list_messages",
            category="google",
            description="Gmail gelen kutusunda e-postaları arar veya okunmamışları listeler.",
            parameters={
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "Gmail arama filtresi (örn. is:unread, from:ali)."},
                    "max_results": {"type": "integer", "description": "Maksimum e-posta sayısı."},
                },
                "required": [],
            },
            handler=gmail_list_messages,
        )
    )
    registry.register(
        ToolDef(
            name="gmail_read_message",
            category="google",
            description="Belirli bir Gmail mesaj ID'sinin tam metnini okur.",
            parameters={
                "type": "object",
                "properties": {"message_id": {"type": "string", "description": "Gmail mesaj ID."}},
                "required": ["message_id"],
            },
            handler=gmail_read_message,
        )
    )
    registry.register(
        ToolDef(
            name="gmail_create_draft",
            category="google",
            description="Gmail'de güvenli bir e-posta taslağı oluşturur (göndermez).",
            parameters={
                "type": "object",
                "properties": {
                    "to": {"type": "string", "description": "Alıcı e-posta adresi."},
                    "subject": {"type": "string", "description": "Konu."},
                    "body": {"type": "string", "description": "E-posta metni."},
                },
                "required": ["to", "subject", "body"],
            },
            handler=gmail_create_draft,
        )
    )
    registry.register(
        ToolDef(
            name="gmail_send_email",
            category="google",
            description="Gmail üzerinden doğrudan e-posta gönderir (kullanıcı onayı gerektirir).",
            requires_approval=True,
            approval_template="Gmail ile e-posta gönderilecek -> Alıcı: {to} | Konu: {subject}",
            parameters={
                "type": "object",
                "properties": {
                    "to": {"type": "string", "description": "Alıcı e-posta adresi."},
                    "subject": {"type": "string", "description": "Konu."},
                    "body": {"type": "string", "description": "E-posta içeriği."},
                },
                "required": ["to", "subject", "body"],
            },
            handler=gmail_send_email,
        )
    )
    registry.register(
        ToolDef(
            name="calendar_list_events",
            category="google",
            description="Google Takvim'deki yaklaşan etkinlikleri ve toplantıları listeler.",
            parameters={
                "type": "object",
                "properties": {
                    "days_ahead": {"type": "integer", "description": "Kaç gün ileriye bakılsın (varsayılan 7)."},
                    "max_results": {"type": "integer", "description": "Maksimum etkinlik sayısı."},
                },
                "required": [],
            },
            handler=calendar_list_events,
        )
    )
    registry.register(
        ToolDef(
            name="calendar_create_event",
            category="google",
            description="Google Takvim'e yeni etkinlik/toplantı ekler.",
            parameters={
                "type": "object",
                "properties": {
                    "summary": {"type": "string", "description": "Etkinlik başlığı."},
                    "start_iso": {"type": "string", "description": "Başlangıç ISO-8601 (örn. 2026-11-23T14:00:00+03:00)."},
                    "end_iso": {"type": "string", "description": "Bitiş ISO-8601 (örn. 2026-11-23T15:00:00+03:00)."},
                    "description": {"type": "string", "description": "Açıklama."},
                    "location": {"type": "string", "description": "Konum veya toplantı linki."},
                },
                "required": ["summary", "start_iso", "end_iso"],
            },
            handler=calendar_create_event,
        )
    )
    registry.register(
        ToolDef(
            name="calendar_delete_event",
            category="google",
            description="Google Takvim'den belirtilen etkinliği siler (kullanıcı onayı gerektirir).",
            requires_approval=True,
            approval_template="Google Takvim etkinliği silinecek (ID: {event_id})",
            parameters={
                "type": "object",
                "properties": {"event_id": {"type": "string", "description": "Silinecek etkinlik ID."}},
                "required": ["event_id"],
            },
            handler=calendar_delete_event,
        )
    )
    registry.register(
        ToolDef(
            name="drive_search_files",
            category="google",
            description="Google Drive dosyaları arasında isimle arama yapar.",
            parameters={
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "Aranacak dosya adı."},
                    "max_results": {"type": "integer", "description": "Maksimum sonuç."},
                },
                "required": ["query"],
            },
            handler=drive_search_files,
        )
    )
    registry.register(
        ToolDef(
            name="sheets_read_range",
            category="google",
            description="Bir Google Sheets tablosundan belirtilen hücre aralığını okur.",
            parameters={
                "type": "object",
                "properties": {
                    "spreadsheet_id": {"type": "string", "description": "Sheets dosya ID."},
                    "range_a1": {"type": "string", "description": "Hücre aralığı (örn. Sayfa1!A1:F30)."},
                },
                "required": ["spreadsheet_id"],
            },
            handler=sheets_read_range,
        )
    )
    registry.register(
        ToolDef(
            name="sheets_append_row",
            category="google",
            description="Bir Google Sheets tablosunun sonuna yeni satır ekler.",
            parameters={
                "type": "object",
                "properties": {
                    "spreadsheet_id": {"type": "string", "description": "Sheets dosya ID."},
                    "range_a1": {"type": "string", "description": "Hedef sayfa/aralık (örn. Sayfa1!A:D)."},
                    "values_json": {"type": "string", "description": "JSON dizi formatında satır değerleri."},
                },
                "required": ["spreadsheet_id", "range_a1", "values_json"],
            },
            handler=sheets_append_row,
        )
    )
    registry.register(
        ToolDef(
            name="docs_create_document",
            category="google",
            description="Yeni bir Google Docs belgesi oluşturur ve içeriğini yazar.",
            parameters={
                "type": "object",
                "properties": {
                    "title": {"type": "string", "description": "Belge başlığı."},
                    "content": {"type": "string", "description": "Belge içeriği."},
                },
                "required": ["title", "content"],
            },
            handler=docs_create_document,
        )
    )
