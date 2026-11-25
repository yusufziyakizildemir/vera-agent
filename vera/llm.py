"""VERA Çoklu Model Beyin Yönlendiricisi (Gemini Native + OpenRouter Otomatik Fallback)."""

from __future__ import annotations

import asyncio
import base64
import json
import logging
import time
from dataclasses import dataclass, field
from typing import Any

import httpx

from vera.config import Settings
from vera.database import Database

logger = logging.getLogger("vera.llm")


@dataclass
class ToolCallRequest:
    id: str
    name: str
    args: dict[str, Any]


@dataclass
class LLMResponse:
    text: str = ""
    tool_calls: list[ToolCallRequest] = field(default_factory=list)
    tokens_in: int = 0
    tokens_out: int = 0
    model: str = ""
    raw_assistant_parts: Any = None


class LLMRouter:
    """Routes chat, tool-use, vision, and embeddings across Gemini and OpenRouter."""

    def __init__(self, settings: Settings, db: Database):
        self.settings = settings
        self.db = db
        self._genai_client = None
        self._model_cooldown_until: dict[str, float] = {}

    def _get_gemini_client(self):
        if not self.settings.gemini_api_key:
            return None
        if self._genai_client is None:
            from google import genai
            from google.genai import types

            try:
                http_opts = types.HttpOptions(
                    timeout=15000,
                    retry_options=types.HttpRetryOptions(attempts=1),
                )
                self._genai_client = genai.Client(
                    api_key=self.settings.gemini_api_key,
                    http_options=http_opts,
                )
            except TypeError:
                try:
                    self._genai_client = genai.Client(
                        api_key=self.settings.gemini_api_key,
                        http_options=types.HttpOptions(timeout=15000),
                    )
                except Exception:
                    self._genai_client = genai.Client(api_key=self.settings.gemini_api_key)
        return self._genai_client

    async def embed(self, text: str) -> list[float] | None:
        client = self._get_gemini_client()
        if not client or not text.strip():
            return None

        def _call() -> list[float] | None:
            res = client.models.embed_content(
                model=self.settings.gemini_embed_model,
                contents=text[:3000],
            )
            if res and res.embeddings:
                return list(res.embeddings[0].values)
            return None

        try:
            return await asyncio.to_thread(_call)
        except Exception as exc:
            logger.debug("Gemini embedding başarısız (FTS5 yedek devrede): %s", exc)
            return None

    async def generate_simple(self, prompt: str, use_smart: bool = False) -> LLMResponse:
        return await self.chat(
            system_prompt="Sen VERA'nın yardımcı iç değerlendirme modülüsün.",
            messages=[{"role": "user", "content": prompt}],
            tools_gemini=None,
            tools_openai=None,
            use_smart=use_smart,
        )

    async def transcribe_with_gemini(self, audio_bytes: bytes, mime_type: str = "audio/ogg") -> str:
        """Fallback STT using Gemini native audio understanding."""
        client = self._get_gemini_client()
        if not client:
            raise RuntimeError("Ne GROQ_API_KEY ne de GEMINI_API_KEY tanımlı.")

        from google.genai import types

        def _call() -> str:
            res = client.models.generate_content(
                model=self.settings.gemini_fast_model,
                contents=[
                    types.Part.from_bytes(data=audio_bytes, mime_type=mime_type),
                    "Bu ses kaydını birebir, eksiksiz ve yalnızca konuşulan metin olarak Türkçe yazıya dök. Ek yorum ekleme.",
                ],
            )
            return (res.text or "").strip()

        return await asyncio.to_thread(_call)

    async def chat(
        self,
        system_prompt: str,
        messages: list[dict[str, Any]],
        tools_gemini: list[dict[str, Any]] | None = None,
        tools_openai: list[dict[str, Any]] | None = None,
        use_smart: bool = False,
        attachments: list[dict[str, Any]] | None = None,
        max_output_tokens: int | None = None,
    ) -> LLMResponse:
        """Executes a chat completion with automatic Gemini -> OpenRouter fallback."""
        errors: list[str] = []

        if self.settings.gemini_api_key:
            preferred = (
                (
                    "gemini-3.5-flash",
                    self.settings.gemini_smart_model,
                    "gemini-3.5-flash-lite",
                    "gemini-3.8-flash",
                    "gemini-3.7-flash",
                )
                if (use_smart or attachments)
                else (
                    "gemini-3.5-flash-lite",
                    "gemini-3.5-flash",
                    self.settings.gemini_fast_model,
                    "gemini-3.8-flash",
                    "gemini-3.7-flash",
                )
            )
            candidates: list[str] = []
            for m in preferred:
                if m and m not in candidates:
                    candidates.append(m)

            now = time.monotonic()
            active = [m for m in candidates if self._model_cooldown_until.get(m, 0.0) <= now]
            for model_name in active or candidates:
                try:
                    resp = await self._call_gemini(
                        model_name=model_name,
                        system_prompt=system_prompt,
                        messages=messages,
                        tools_gemini=tools_gemini,
                        attachments=attachments,
                        max_output_tokens=max_output_tokens,
                    )
                    self.db.log_event(
                        "llm_call",
                        f"gemini/{model_name} tools={len(resp.tool_calls)}",
                        tokens_in=resp.tokens_in,
                        tokens_out=resp.tokens_out,
                        model=model_name,
                    )
                    return resp
                except Exception as exc:
                    msg = str(exc).lower()
                    if any(k in msg for k in ("503", "429", "404", "unavailable", "exhausted")):
                        self._model_cooldown_until[model_name] = time.monotonic() + 300.0
                    logger.warning("Gemini (%s) hata verdi: %s", model_name, exc)
                    errors.append(f"Gemini({model_name}): {exc}")

        if self.settings.openrouter_api_key:
            try:
                resp = await self._call_openrouter(
                    system_prompt=system_prompt,
                    messages=messages,
                    tools_openai=tools_openai,
                )
                self.db.log_event(
                    "llm_call",
                    f"openrouter/{resp.model} tools={len(resp.tool_calls)}",
                    tokens_in=resp.tokens_in,
                    tokens_out=resp.tokens_out,
                    model=resp.model,
                )
                return resp
            except Exception as exc:
                logger.warning("OpenRouter fallback hata verdi: %s", exc)
                errors.append(f"OpenRouter: {exc}")

        if not self.settings.gemini_api_key and not self.settings.openrouter_api_key:
            return LLMResponse(
                text=(
                    "Henüz bir yapay zeka API anahtarı yapılandırılmadı. "
                    "`.env` dosyasına ücretsiz `GEMINI_API_KEY` (aistudio.google.com) veya "
                    "`OPENROUTER_API_KEY` ekleyip VERA'yı yeniden başlatın."
                ),
                model="unconfigured",
            )

        return LLMResponse(
            text=f"Yapay zeka sağlayıcılarına şu an ulaşılamıyor. Detay: {' | '.join(errors)[:400]}",
            model="error",
        )

    async def _call_gemini(
        self,
        model_name: str,
        system_prompt: str,
        messages: list[dict[str, Any]],
        tools_gemini: list[dict[str, Any]] | None,
        attachments: list[dict[str, Any]] | None = None,
        max_output_tokens: int | None = None,
    ) -> LLMResponse:
        from google.genai import types

        client = self._get_gemini_client()
        assert client is not None

        contents: list[types.Content] = []
        idx = 0
        while idx < len(messages):
            msg = messages[idx]
            role = msg["role"]
            if role == "tool":
                # Group consecutive tool responses into a single user Content turn for Gemini compliance
                tool_parts = []
                while idx < len(messages) and messages[idx]["role"] == "tool":
                    tmsg = messages[idx]
                    payload = tmsg["content"]
                    if not isinstance(payload, dict):
                        payload = {"result": payload}
                    else:
                        payload = {"result": payload}
                    tool_parts.append(
                        types.Part.from_function_response(
                            name=tmsg["name"],
                            response=payload,
                        )
                    )
                    idx += 1
                contents.append(types.Content(role="user", parts=tool_parts))
                continue
            elif role == "assistant_tool_calls":
                # Preserve exact Gemini response parts (including thought signatures) when available
                raw_parts = msg.get("raw_parts")
                if raw_parts:
                    contents.append(types.Content(role="model", parts=raw_parts))
                else:
                    parts = []
                    if msg.get("text"):
                        parts.append(types.Part.from_text(text=msg["text"]))
                    for tc in msg.get("tool_calls", []):
                        parts.append(
                            types.Part.from_function_call(name=tc["name"], args=tc["args"])
                        )
                    contents.append(types.Content(role="model", parts=parts))
            else:
                g_role = "model" if role == "assistant" else "user"
                parts = [types.Part.from_text(text=str(msg.get("content", "")))]
                # Attach binary images/media to the latest user turn
                if attachments and idx == len(messages) - 1 and g_role == "user":
                    for att in attachments:
                        if att.get("bytes") and att.get("mime_type"):
                            parts.append(
                                types.Part.from_bytes(
                                    data=att["bytes"], mime_type=att["mime_type"]
                                )
                            )
                contents.append(types.Content(role=g_role, parts=parts))
            idx += 1

        config_kwargs: dict[str, Any] = {
            "system_instruction": system_prompt,
            "temperature": 0.35,
        }
        if max_output_tokens:
            config_kwargs["max_output_tokens"] = max_output_tokens
        if tools_gemini:
            config_kwargs["tools"] = [{"function_declarations": tools_gemini}]
        if "lite" not in model_name.lower():
            try:
                config_kwargs["thinking_config"] = types.ThinkingConfig(thinking_budget=0)
            except Exception:
                pass

        config = types.GenerateContentConfig(**config_kwargs)

        def _invoke():
            try:
                return client.models.generate_content(
                    model=model_name,
                    contents=contents,
                    config=config,
                )
            except Exception as e:
                err_l = str(e).lower()
                if "thinking_config" in config_kwargs and (
                    "thinking" in err_l or "invalid_argument" in err_l or "400" in err_l
                ):
                    config_kwargs.pop("thinking_config", None)
                    return client.models.generate_content(
                        model=model_name,
                        contents=contents,
                        config=types.GenerateContentConfig(**config_kwargs),
                    )
                raise

        raw = await asyncio.to_thread(_invoke)

        text_chunks: list[str] = []
        tool_calls: list[ToolCallRequest] = []
        raw_parts = None
        if raw.candidates:
            cand = raw.candidates[0]
            if cand.content and cand.content.parts:
                raw_parts = list(cand.content.parts)
                for p_idx, part in enumerate(cand.content.parts):
                    if getattr(part, "text", None):
                        text_chunks.append(part.text)
                    fc = getattr(part, "function_call", None)
                    if fc and getattr(fc, "name", None):
                        args = dict(fc.args) if fc.args else {}
                        tc_id = getattr(fc, "id", None) or f"call_{p_idx}_{fc.name}"
                        tool_calls.append(ToolCallRequest(id=tc_id, name=fc.name, args=args))

        usage = getattr(raw, "usage_metadata", None)
        tokens_in = int(getattr(usage, "prompt_token_count", 0) or 0)
        tokens_out = int(getattr(usage, "candidates_token_count", 0) or 0)

        return LLMResponse(
            text="\n".join(text_chunks).strip(),
            tool_calls=tool_calls,
            tokens_in=tokens_in,
            tokens_out=tokens_out,
            model=model_name,
            raw_assistant_parts=raw_parts,
        )

    async def _call_openrouter(
        self,
        system_prompt: str,
        messages: list[dict[str, Any]],
        tools_openai: list[dict[str, Any]] | None,
    ) -> LLMResponse:
        model = self.settings.openrouter_fallback_model
        oai_messages: list[dict[str, Any]] = [{"role": "system", "content": system_prompt}]
        for msg in messages:
            role = msg["role"]
            if role == "tool":
                oai_messages.append(
                    {
                        "role": "tool",
                        "tool_call_id": msg.get("tool_call_id", "call_0"),
                        "name": msg["name"],
                        "content": json.dumps(msg["content"], ensure_ascii=False),
                    }
                )
            elif role == "assistant_tool_calls":
                oai_messages.append(
                    {
                        "role": "assistant",
                        "content": msg.get("text") or "",
                        "tool_calls": [
                            {
                                "id": tc["id"],
                                "type": "function",
                                "function": {
                                    "name": tc["name"],
                                    "arguments": json.dumps(tc["args"], ensure_ascii=False),
                                },
                            }
                            for tc in msg.get("tool_calls", [])
                        ],
                    }
                )
            else:
                oai_messages.append(
                    {"role": "assistant" if role == "assistant" else "user", "content": str(msg.get("content", ""))}
                )

        payload: dict[str, Any] = {
            "model": model,
            "messages": oai_messages,
            "temperature": 0.4,
        }
        if tools_openai:
            payload["tools"] = tools_openai

        async with httpx.AsyncClient(timeout=45.0) as client:
            resp = await client.post(
                "https://openrouter.ai/api/v1/chat/completions",
                headers={
                    "Authorization": f"Bearer {self.settings.openrouter_api_key}",
                    "Content-Type": "application/json",
                    "HTTP-Referer": self.settings.public_base_url,
                    "X-Title": self.settings.agent_name,
                },
                json=payload,
            )
            resp.raise_for_status()
            data = resp.json()

        choice = data["choices"][0]["message"]
        text = (choice.get("content") or "").strip()
        tool_calls: list[ToolCallRequest] = []
        for tc in choice.get("tool_calls") or []:
            fn = tc.get("function", {})
            raw_args = fn.get("arguments", "{}")
            try:
                args = json.loads(raw_args) if isinstance(raw_args, str) else dict(raw_args)
            except Exception:
                args = {}
            tool_calls.append(
                ToolCallRequest(
                    id=tc.get("id", f"or_{fn.get('name', 'tool')}"),
                    name=fn.get("name", ""),
                    args=args,
                )
            )

        usage = data.get("usage", {})
        return LLMResponse(
            text=text,
            tool_calls=tool_calls,
            tokens_in=int(usage.get("prompt_tokens", 0)),
            tokens_out=int(usage.get("completion_tokens", 0)),
            model=model,
        )
