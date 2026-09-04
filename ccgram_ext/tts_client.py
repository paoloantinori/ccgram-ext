"""Self-contained OpenAI-compatible TTS client for the speak action.

Carries the enhancements the fork's speak path needs (response_format,
long cold-start timeouts, Retry-After on 429) without touching ccgram's
tts/ seam, which stays upstream-shaped.
"""

from __future__ import annotations

from dataclasses import dataclass

import httpx
import structlog

logger = structlog.get_logger()

_RATE_LIMITED_STATUS = 429


@dataclass(frozen=True)
class TtsAudio:
    data: bytes
    filename: str


class TtsSynthesisError(Exception):
    """Synthesis failed in a known way; ``retry_after`` is set on 429."""

    def __init__(
        self,
        message: str,
        *,
        status_code: int | None = None,
        retry_after: float | None = None,
    ) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.retry_after = retry_after


class OpenAITtsSynthesizer:
    """Minimal OpenAI-compatible ``/audio/speech`` client."""

    def __init__(
        self,
        *,
        api_key: str,
        model: str,
        voice: str,
        base_url: str = "https://api.openai.com/v1",
        response_format: str = "mp3",
        timeout: float = 60.0,
    ) -> None:
        self._api_key = api_key
        self._model = model
        self._voice = voice
        self._base_url = base_url.rstrip("/")
        self._response_format = response_format
        self._timeout = timeout

    async def synthesize(self, text: str) -> TtsAudio:
        try:
            async with httpx.AsyncClient(
                base_url=self._base_url, timeout=self._timeout
            ) as client:
                resp = await client.post(
                    "/audio/speech",
                    headers={"Authorization": f"Bearer {self._api_key}"},
                    json={
                        "model": self._model,
                        "voice": self._voice,
                        "input": text,
                        "response_format": self._response_format,
                    },
                )
        except httpx.HTTPError as exc:
            raise TtsSynthesisError(f"TTS failed: {exc}") from exc
        if resp.status_code != 200:
            status = resp.status_code
            retry_after: float | None = None
            if status == _RATE_LIMITED_STATUS:
                header = resp.headers.get("Retry-After", "")
                try:
                    retry_after = float(header) if header else None
                except ValueError:
                    retry_after = None
            raise TtsSynthesisError(
                f"TTS failed: {status} {resp.text}",
                status_code=status,
                retry_after=retry_after,
            )
        return TtsAudio(data=resp.content, filename=f"speech.{self._response_format}")
