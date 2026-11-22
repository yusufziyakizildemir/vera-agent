"""VERA Canlı İki Yönlü Sesli Görüşme Köprüsü (Web PWA <-> Gemini Live Native Audio API)."""

from __future__ import annotations

import asyncio
import base64
import json
import logging
from typing import TYPE_CHECKING, Any

from fastapi import WebSocket, WebSocketDisconnect

if TYPE_CHECKING:
    from vera.agent import VeraAgent

logger = logging.getLogger("vera.live_voice")


async def run_gemini_live_bridge(websocket: WebSocket, agent: VeraAgent, session_id: str = "live_voice") -> None:
    """
    Bidirectional real-time voice bridge between browser PWA and Gemini Live API.
    - Inbound WS JSON: {"type": "audio", "data": "<base64 16kHz PCM16>"} or {"type": "text", "text": "..."}
    - Outbound WS JSON:
        {"type": "ready"}
        {"type": "audio", "data": "<base64 24kHz PCM16>"}
        {"type": "transcript", "role": "user"|"assistant", "text": "..."}
        {"type": "tool_event", "name": "...", "status": "running"|"done"}
        {"type": "interrupted"}
        {"type": "turn_complete"}
        {"type": "error", "message": "..."}
    """
    settings = agent.settings
    if not settings.gemini_api_key:
        await websocket.send_json(
            {"type": "error", "message": "Canlı sesli görüşme için GEMINI_API_KEY tanımlı olmalıdır."}
        )
        return

    from google import genai
    from google.genai import types

    # Expose non-approval tools directly inside live voice calls!
    live_tools = [
        t.to_gemini_declaration()
        for t in agent.registry.list_tools(only_enabled=True)
        if not t.requires_approval
    ]

    system_prompt = (
        agent.memory.build_system_prompt(channel="live_voice")
        + "\n\nŞu anda kullanıcıyla CANLI SESLİ GÖRÜŞME yapıyorsun. "
        "Kısa, doğal, sıcak ve akıcı Türkçe konuş; markdown veya sembol kullanma. "
        "Kullanıcı bir görev istediğinde araçları sessizce çalıştır ve sonucu konuşma diliyle aktar."
    )

    config: dict[str, Any] = {
        "response_modalities": ["AUDIO"],
        "system_instruction": {"parts": [{"text": system_prompt}]},
        "output_audio_transcription": {},
        "input_audio_transcription": {},
    }
    if live_tools:
        config["tools"] = [{"function_declarations": live_tools}]

    client = genai.Client(api_key=settings.gemini_api_key)
    model_name = settings.gemini_live_model

    user_transcript_buf: list[str] = []
    model_transcript_buf: list[str] = []

    try:
        async with client.aio.live.connect(model=model_name, config=config) as session:
            await websocket.send_json({"type": "ready", "model": model_name})

            async def client_to_gemini() -> None:
                try:
                    while True:
                        raw = await websocket.receive_text()
                        msg = json.loads(raw)
                        mtype = msg.get("type")
                        if mtype == "audio" and msg.get("data"):
                            pcm_bytes = base64.b64decode(msg["data"])
                            await session.send_realtime_input(
                                audio=types.Blob(data=pcm_bytes, mime_type="audio/pcm;rate=16000")
                            )
                        elif mtype == "text" and msg.get("text"):
                            await session.send_client_content(
                                turns={"role": "user", "parts": [{"text": msg["text"]}]},
                                turn_complete=True,
                            )
                        elif mtype == "end":
                            break
                except WebSocketDisconnect:
                    pass
                except Exception as exc:
                    logger.debug("Live client_to_gemini sonlandı: %s", exc)

            async def gemini_to_client() -> None:
                try:
                    async for chunk in session.receive():
                        sc = getattr(chunk, "server_content", None)
                        if sc:
                            if getattr(sc, "interrupted", False):
                                await websocket.send_json({"type": "interrupted"})

                            in_tr = getattr(sc, "input_transcription", None)
                            if in_tr and getattr(in_tr, "text", None):
                                user_transcript_buf.append(in_tr.text)
                                await websocket.send_json(
                                    {"type": "transcript", "role": "user", "text": in_tr.text}
                                )

                            out_tr = getattr(sc, "output_transcription", None)
                            if out_tr and getattr(out_tr, "text", None):
                                model_transcript_buf.append(out_tr.text)
                                await websocket.send_json(
                                    {"type": "transcript", "role": "assistant", "text": out_tr.text}
                                )

                            mt = getattr(sc, "model_turn", None)
                            if mt and getattr(mt, "parts", None):
                                for part in mt.parts:
                                    inline = getattr(part, "inline_data", None)
                                    if inline and getattr(inline, "data", None):
                                        b64 = base64.b64encode(inline.data).decode("ascii")
                                        await websocket.send_json({"type": "audio", "data": b64})

                            if getattr(sc, "turn_complete", False):
                                # Persist turn transcripts to VERA memory log
                                u_txt = "".join(user_transcript_buf).strip()
                                m_txt = "".join(model_transcript_buf).strip()
                                if u_txt:
                                    agent.db.add_message(session_id, "live_voice", "user", u_txt)
                                if m_txt:
                                    agent.db.add_message(session_id, "live_voice", "assistant", m_txt)
                                user_transcript_buf.clear()
                                model_transcript_buf.clear()
                                await websocket.send_json({"type": "turn_complete"})

                        tc = getattr(chunk, "tool_call", None)
                        if tc and getattr(tc, "function_calls", None):
                            responses = []
                            ctx = agent.make_tool_context(session_id=session_id, channel="live_voice")
                            for fc in tc.function_calls:
                                await websocket.send_json(
                                    {"type": "tool_event", "name": fc.name, "status": "running"}
                                )
                                args = dict(fc.args) if fc.args else {}
                                result = await agent.registry.execute(fc.name, args, ctx)
                                await websocket.send_json(
                                    {"type": "tool_event", "name": fc.name, "status": "done"}
                                )
                                responses.append(
                                    types.FunctionResponse(
                                        name=fc.name,
                                        id=fc.id,
                                        response={"result": result},
                                    )
                                )
                            await session.send_tool_response(function_responses=responses)
                except WebSocketDisconnect:
                    pass
                except Exception as exc:
                    logger.warning("Gemini Live akışı kapandı: %s", exc)
                    try:
                        await websocket.send_json({"type": "error", "message": str(exc)})
                    except Exception:
                        pass

            up_task = asyncio.create_task(client_to_gemini())
            down_task = asyncio.create_task(gemini_to_client())
            done, pending = await asyncio.wait(
                {up_task, down_task}, return_when=asyncio.FIRST_COMPLETED
            )
            for t in pending:
                t.cancel()
    except Exception as exc:
        logger.exception("Gemini Live bağlantı hatası: %s", exc)
        try:
            await websocket.send_json(
                {
                    "type": "error",
                    "message": (
                        f"Canlı ses bağlantısı kurulamadı ({model_name}): {exc}. "
                        "GEMINI_API_KEY ve GEMINI_LIVE_MODEL ayarlarını kontrol edin."
                    ),
                }
            )
        except Exception:
            pass
