"""Reaction-triggered actions: the fork's #195 feature, out of tree.

A reaction on a bot-sent content message triggers a configurable action
on the window that produced it: screenshot (PNG of the pane), speak
(voice note of the message text via an OpenAI-compatible endpoint), or
any toolbar key/text action. The emoji -> action map lives in the
``[reactions]`` table of the same ``~/.ccgram/toolbar.toml`` the core
toolbar uses. Inert by default: no table, no behavior.
"""

from __future__ import annotations

import asyncio
import io
import structlog
import time
from collections import OrderedDict
from typing import NamedTuple

from telegram import Update
from telegram.error import TelegramError

from ccgram.config import config as app_config
from ccgram.handlers.messaging_pipeline.message_sender import (
    rate_limit_send_message,
    send_kwargs,
)
from ccgram.multiplexer import multiplexer as tmux_manager
from ccgram.screenshot import text_to_image
from ccgram.telegram_client import PTBTelegramClient

from .config import load_config
from .tts_client import OpenAITtsSynthesizer, TtsSynthesisError

logger = structlog.get_logger()

# Bounded LRU: only recent bot messages are reaction-triggerable, with
# their text and topic (the Bot API's reaction update carries neither).
_MAX_TRACKED = 500
_TRACK_TTL_SECONDS = 3600.0


class _TrackedEntry(NamedTuple):
    window_id: str
    ts: float
    text: str
    thread_id: int | None


_tracked: OrderedDict[tuple[int, int], _TrackedEntry] = OrderedDict()


def on_message_delivered(
    *,
    chat_id: int,
    message_id: int,
    window_id: str,
    text: str,
    thread_id: int | None,
) -> None:
    """Core domain event: record a delivered message as triggerable."""
    if not window_id:
        return
    key = (chat_id, message_id)
    _tracked[key] = _TrackedEntry(
        window_id, time.monotonic(), text[:2000], thread_id
    )
    _tracked.move_to_end(key)
    while len(_tracked) > _MAX_TRACKED:
        _tracked.popitem(last=False)


def _lookup(chat_id: int, message_id: int) -> _TrackedEntry | None:
    key = (chat_id, message_id)
    entry = _tracked.get(key)
    if entry is None:
        return None
    if time.monotonic() - entry[1] > _TRACK_TTL_SECONDS:
        _tracked.pop(key, None)
        return None
    return entry


def _reaction_emojis(mr) -> set[str]:
    """Emoji newly ADDED by this update (delta, not the full set)."""
    if mr is None:
        return set()
    new = {getattr(r, "emoji", None) for r in (mr.new_reaction or ())}
    old = {getattr(r, "emoji", None) for r in (mr.old_reaction or ())}
    return {e for e in new - old if isinstance(e, str)}


async def handle_reaction_update(update: Update, context) -> None:
    """Dispatch reaction actions for tracked bot messages."""
    mr = update.message_reaction
    if mr is None or mr.user is None:
        return
    if not app_config.is_user_allowed(mr.user.id):
        return
    tracked = _lookup(mr.chat.id, mr.message_id)
    if tracked is None:
        return
    cfg = load_config()
    if not cfg.reaction_map:
        return  # feature off
    emoji, action = _resolve(cfg.reaction_map, mr)
    if action is None:
        return
    window_id, _ts, message_text, thread_id = tracked
    logger.info(
        "reaction trigger", emoji=emoji, action=action, window_id=window_id
    )
    client = PTBTelegramClient(context.bot)
    try:
        if action == "screenshot":
            await _action_screenshot(client, mr.chat.id, window_id, thread_id)
        elif action == "speak":
            await _action_speak(client, mr.chat.id, message_text, thread_id)
        else:
            await _action_toolbar(
                client, mr.chat.id, window_id, action, thread_id
            )
    except TelegramError as exc:
        logger.warning("reaction action failed", action=action, error=str(exc))


def _resolve(
    reaction_map: dict[str, str], mr
) -> tuple[str, str | None]:
    for emoji in sorted(_reaction_emojis(mr)):
        name = reaction_map.get(emoji)
        if name:
            return emoji, name
    return "", None


async def _action_screenshot(
    client, chat_id: int, window_id: str, thread_id: int | None = None
) -> None:
    pane_text = await tmux_manager.capture_pane(window_id, with_ansi=True)
    if not pane_text:
        await rate_limit_send_message(
            client, chat_id, "⚠️ nothing to capture", **send_kwargs(thread_id)
        )
        return
    image_bytes = await text_to_image(pane_text)
    await client.send_document(
        chat_id=chat_id,
        document=image_bytes,
        filename="screenshot.png",
        **send_kwargs(thread_id),
    )


async def _clear_progress(client, chat_id: int, progress) -> None:
    if progress is None:
        return
    try:
        await client.delete_message(chat_id=chat_id, message_id=progress.message_id)
    except TelegramError as exc:
        logger.debug("could not clear progress message", error=str(exc))


async def _action_speak(
    client, chat_id: int, message_text: str, thread_id: int | None = None
) -> None:
    speak_cfg = load_config().reaction_speak
    url = speak_cfg.get("url", "")
    timeout = float(speak_cfg.get("timeout", "240"))
    progress = await rate_limit_send_message(
        client, chat_id, "🔊 synthesizing…", **send_kwargs(thread_id)
    )

    def _synth(model: str) -> OpenAITtsSynthesizer:
        # Key falls back to the whisper env var: same LAN service, one
        # credential to keep current instead of two copies drifting apart.
        api_key = speak_cfg.get("api_key", "") or app_config.whisper_api_key
        return OpenAITtsSynthesizer(
            api_key=api_key,
            model=model,
            voice=speak_cfg.get("voice", "alloy"),
            base_url=url,
            response_format=speak_cfg.get("response_format", "opus"),
            # LAN engines can cold-start the model on the first request
            # after idle (30-90s observed); give them room.
            timeout=timeout,
        )

    try:
        audio = None
        if url:
            primary = speak_cfg.get("model", "tts-1")
            fallback = speak_cfg.get("fallback_model", "")
            try:
                audio = await _synth(primary).synthesize(message_text)
            except TtsSynthesisError as exc:
                retryable = "Timeout" in str(exc) or exc.status_code == 429
                if not fallback or not retryable:
                    raise
                # Cold start pays the model load on the first attempt;
                # 429 means honor Retry-After, then use the fast fallback.
                if exc.retry_after:
                    await asyncio.sleep(min(exc.retry_after, 30.0))
                logger.warning(
                    "speak retry on fallback model",
                    primary=primary,
                    fallback=fallback,
                    error=str(exc),
                )
                audio = await _synth(fallback).synthesize(message_text)
    except (TtsSynthesisError, ValueError) as exc:
        logger.warning("speak action failed", error=str(exc))
        await _clear_progress(client, chat_id, progress)
        await rate_limit_send_message(
            client, chat_id, f"⚠️ {exc}", **send_kwargs(thread_id)
        )
        return
    await _clear_progress(client, chat_id, progress)
    if audio is None:
        await rate_limit_send_message(
            client,
            chat_id,
            "⚠️ no TTS configured ([reactions.speak] url)",
            **send_kwargs(thread_id),
        )
        return
    voice = io.BytesIO(audio.data)
    voice.name = audio.filename
    await client.send_voice(
        chat_id=chat_id, voice=voice, **send_kwargs(thread_id)
    )


async def _action_toolbar(
    client,
    chat_id: int,
    window_id: str,
    action_name: str,
    thread_id: int | None = None,
) -> None:
    """Run a toolbar key/text action by name (builtins rejected)."""
    import tomllib

    from .config import _toolbar_path

    try:
        with open(_toolbar_path(), "rb") as f:
            actions = tomllib.load(f).get("actions", {})
    except (tomllib.TOMLDecodeError, OSError):
        actions = {}
    action = actions.get(action_name)
    payload = action.get("payload", "") if isinstance(action, dict) else ""
    action_type = action.get("type") if isinstance(action, dict) else None
    if action_type not in ("key", "text"):
        await rate_limit_send_message(
            client,
            chat_id,
            f"⚠️ reaction action '{action_name}' not found or not key/text",
            **send_kwargs(thread_id),
        )
        return
    enter = action_type == "text"
    literal = (
        bool(action.get("literal", False)) if isinstance(action, dict) else False
    ) or enter
    ok = await tmux_manager.send_keys(
        window_id, payload, enter=enter, literal=literal
    )
    if not ok:
        await rate_limit_send_message(
            client, chat_id, "⚠️ window not found", **send_kwargs(thread_id)
        )
