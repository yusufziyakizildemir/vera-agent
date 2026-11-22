"""VERA Ücretsiz Türkçe Ses Motoru (Groq Whisper STT + Edge-TTS -> FFmpeg OGG Opus)."""

from __future__ import annotations

import asyncio
import logging
import re
import shutil
import tempfile
from pathlib import Path

import httpx

from vera.config import Settings
from vera.llm import LLMRouter

logger = logging.getLogger("vera.voice")


def clean_text_for_speech(text: str, max_chars: int = 1200) -> str:
    """Strips Markdown symbols, emojis, code blocks, and URLs for fluent Turkish voice output."""
    text = re.sub(r"```[\s\S]*?```", " kod bloğu. ", text)
    text = re.sub(r"`([^`]+)`", r"\1", text)
    text = re.sub(r"\[([^\]]+)\]\([^\)]+\)", r"\1", text)
    text = re.sub(r"\[([^\]]+)\]", r"\1: ", text)
    text = re.sub(r"https?://\S+", "", text)
    # Convert Turkish percentage format %84.0 -> yüzde 84
    text = re.sub(r"%\s*(\d+)(?:[.,]\d+)?", r"yüzde \1", text)
    # Remove emojis and pictographs so TTS never spells out emoji names
    text = re.sub(r"[\U00010000-\U0010ffff\u2600-\u27bf\ufe0f]+", " ", text)
    text = re.sub(r"[-─═]{2,}", ". ", text)
    text = re.sub(r"(?m)^\s*[•*\-+]\s+", "", text)
    text = re.sub(r"[#*_~>|]+", " ", text)
    text = re.sub(r"\n+", ". ", text)
    text = re.sub(r"(\.\s*){2,}", ". ", text)
    text = re.sub(r"\s+", " ", text).strip()
    if len(text) > max_chars:
        text = text[:max_chars].rsplit(" ", 1)[0] + "... Detaylar ekranda mevcut Efendim."
    return text


class VoiceEngine:
    """Handles Speech-to-Text (Groq Whisper + Gemini fallback) and Text-to-Speech (Edge-TTS)."""

    def __init__(self, settings: Settings, llm: LLMRouter):
        self.settings = settings
        self.llm = llm

    async def transcribe(
        self, audio_bytes: bytes, filename: str = "voice.ogg", mime_type: str = "audio/ogg"
    ) -> str:
        """Transcribes incoming Turkish voice notes via Groq Whisper (free) with Gemini fallback."""
        if self.settings.groq_api_key:
            try:
                async with httpx.AsyncClient(timeout=35.0) as client:
                    resp = await client.post(
                        "https://api.groq.com/openai/v1/audio/transcriptions",
                        headers={"Authorization": f"Bearer {self.settings.groq_api_key}"},
                        files={"file": (filename, audio_bytes, mime_type)},
                        data={
                            "model": "whisper-large-v3-turbo",
                            "language": self.settings.language or "tr",
                            "response_format": "json",
                        },
                    )
                    resp.raise_for_status()
                    text = (resp.json().get("text") or "").strip()
                    if text:
                        return text
            except Exception as exc:
                logger.warning("Groq Whisper hata verdi, Gemini STT yedeğine geçiliyor: %s", exc)

        # Fallback to Gemini native audio understanding
        return await self.llm.transcribe_with_gemini(audio_bytes=audio_bytes, mime_type=mime_type)

    async def synthesize_mp3(self, text: str, voice: str | None = None) -> bytes:
        """Synthesizes fluent Turkish MP3 audio in-memory via free Microsoft Edge-TTS."""
        import edge_tts

        speech_text = clean_text_for_speech(text)
        if not speech_text:
            raise ValueError("Seslendirilecek metin boş.")

        selected_voice = (voice or self.settings.tts_voice or "tr-TR-AhmetNeural").strip()
        communicate = edge_tts.Communicate(speech_text, voice=selected_voice, rate="+12%")
        audio_chunks: list[bytes] = []
        async for chunk in communicate.stream():
            if chunk.get("type") == "audio" and chunk.get("data"):
                audio_chunks.append(chunk["data"])
        if not audio_chunks:
            raise RuntimeError("TTS ses verisi üretilemedi.")
        return b"".join(audio_chunks)

    async def synthesize_telegram_ogg(self, text: str) -> bytes:
        """Synthesizes Turkish speech and encodes to OGG/Opus for native Telegram voice bubbles."""
        mp3_bytes = await self.synthesize_mp3(text)
        ffmpeg = shutil.which("ffmpeg")
        if not ffmpeg:
            logger.warning("ffmpeg bulunamadı, ham MP3 döndürülüyor.")
            return mp3_bytes

        with tempfile.TemporaryDirectory() as tmpdir:
            mp3_path = Path(tmpdir) / "in.mp3"
            ogg_path = Path(tmpdir) / "out.ogg"
            mp3_path.write_bytes(mp3_bytes)

            proc = await asyncio.create_subprocess_exec(
                ffmpeg,
                "-y",
                "-i",
                str(mp3_path),
                "-c:a",
                "libopus",
                "-b:a",
                "48k",
                "-ar",
                "48000",
                "-ac",
                "1",
                str(ogg_path),
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.PIPE,
            )
            _, stderr = await proc.communicate()
            if proc.returncode != 0 or not ogg_path.exists():
                logger.warning("ffmpeg ogg dönüşümü başarısız: %s", stderr.decode(errors="ignore")[:300])
                return mp3_bytes
            return ogg_path.read_bytes()
