"""Gemini speech adapter (standard library only).

Text to speech returns raw 16-bit PCM from the model; it is wrapped as a WAV file so the recording can be
kept as evidence. Speech to text sends the WAV file and returns the transcript. The API key is read from the
environment and is never written to logs, reports or exceptions.
"""

from __future__ import annotations

import base64
import io
import os
import re
import wave
from dataclasses import dataclass

from voiceshop.net import ServiceError, post_json

ENDPOINT = "https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
DEFAULT_TTS_MODEL = "gemini-2.5-flash-preview-tts"
DEFAULT_STT_MODEL = "gemini-3.5-flash"
DEFAULT_VOICE = "Kore"


class SpeechError(RuntimeError):
    """The speech service failed or returned something unusable."""


def api_key_from_env() -> str | None:
    return os.environ.get("VOICESHOP_GEMINI_API_KEY") or os.environ.get("GEMINI_API_KEY") or None


@dataclass(frozen=True)
class GeminiSpeech:
    api_key: str
    tts_model: str = DEFAULT_TTS_MODEL
    stt_model: str = DEFAULT_STT_MODEL
    timeout_s: float = 120.0

    def _call(self, model: str, body: dict) -> dict:
        try:
            return post_json(ENDPOINT.format(model=model), body, {"x-goog-api-key": self.api_key},
                             timeout_s=self.timeout_s, attempts=3)
        except ServiceError as exc:
            raise SpeechError(f"{model}: {exc}") from None

    def synthesize(self, text: str, voice: str = DEFAULT_VOICE) -> bytes:
        """Return WAV bytes of the spoken text."""
        body = {
            "contents": [{"parts": [{"text": text}]}],
            "generationConfig": {
                "responseModalities": ["AUDIO"],
                "speechConfig": {"voiceConfig": {"prebuiltVoiceConfig": {"voiceName": voice}}},
            },
        }
        data = self._call(self.tts_model, body)
        try:
            inline = data["candidates"][0]["content"]["parts"][0]["inlineData"]
        except (KeyError, IndexError, TypeError):
            raise SpeechError("text to speech returned no audio") from None
        match = re.search(r"rate=(\d+)", inline.get("mimeType", ""))
        rate = int(match.group(1)) if match else 24000
        pcm = base64.b64decode(inline["data"])
        buf = io.BytesIO()
        with wave.open(buf, "wb") as w:
            w.setnchannels(1)
            w.setsampwidth(2)
            w.setframerate(rate)
            w.writeframes(pcm)
        return buf.getvalue()

    def transcribe(self, wav: bytes) -> str:
        """Return the transcript of a WAV recording, exactly as spoken."""
        body = {"contents": [{"parts": [
            {"inline_data": {"mime_type": "audio/wav", "data": base64.b64encode(wav).decode("ascii")}},
            {"text": "Transcribe this audio exactly. Output only the transcript."},
        ]}]}
        data = self._call(self.stt_model, body)
        try:
            text = data["candidates"][0]["content"]["parts"][0]["text"]
        except (KeyError, IndexError, TypeError):
            raise SpeechError("speech to text returned no transcript") from None
        return " ".join(text.split())
