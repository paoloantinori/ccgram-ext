"""Topic identity icons: per-topic avatar via Telegram's forum icon set.

The topic's avatar (leftmost icon in the topic list) is the identity
slot; the state emoji lives in the title and belongs to core's
topic_emoji. This module resolves and applies an identity icon per
window, using the custom-emoji set Telegram exposes through
``getForumTopicIconStickers``.

Resolution chain (first hit wins):
  1. explicit mapping in ``~/.ccgram/toolbar.toml`` ``[topic-icons]``,
     keyed by window display name, any ``▸`` segment of it, or cwd
     basename (all lowercased);
  2. built-in keyword heuristics on the name (opt-in via
     ``heuristics = true``), token-based so "bot" does not match
     "robotics";
  3. deterministic hash pick from a curated, visually distinct subset
     (stable across restarts: same name, same icon).

Inert by default: no ``[topic-icons]`` table and no heuristics flag
means no icon is ever touched.

API calls go through the extension's own ``telegram.Bot`` built from the
configured token. It only makes ``editForumTopic`` /
``getForumTopicIconStickers`` calls; it never polls, so it cannot
collide with the bridge's getUpdates loop.
"""

from __future__ import annotations

import asyncio
import hashlib
import time as _time

import structlog
from ccgram.config import config as app_config
from ccgram.handlers.messaging_pipeline.message_sender import (
    rate_limit_send_message,
    send_kwargs,
)
from ccgram.handlers.status.topic_emoji import _flood_paused
from ccgram.handlers.status.topic_emoji import (
    _pause_renames_for_flood as pause_renames_for_flood,
)
from ccgram.telegram_client import PTBTelegramClient
from ccgram.thread_router import thread_router
from ccgram.window_query import view_window
from telegram import Bot
from telegram.error import RetryAfter, TelegramError

from .config import load_config


def renames_flood_paused(chat_id: int) -> bool:
    """Flood-cooldown check (the core helper wants an explicit clock)."""
    return _flood_paused(chat_id, _time.monotonic())


logger = structlog.get_logger()

ICON_EDIT_SPACING_SECONDS = 1.5

# Curated hash-fallback pool: visually distinct, no near-duplicates, no
# emoji that read as status (hearts, fire, check) so they never clash
# with the title's state glyphs. Every member must be in the forum icon
# set; membership is checked at apply time against the fetched ids.
_HASH_POOL = (
    "📰",
    "💡",
    "🎙",
    "📝",
    "📆",
    "📁",
    "🔎",
    "💎",
    "🎮",
    "💻",
    "📱",
    "🏠",
    "🎬",
    "🎵",
    "📚",
    "👀",
    "🍓",
    "🦄",
    "🤖",
    "🎓",
    "🔭",
    "🔬",
    "🎤",
    "💼",
    "🧪",
    "🧮",
    "🎨",
    "🔮",
    "🧠",
    "🐈",
)

# Keyword heuristics, matched on name/basename tokens. Order matters:
# specific before generic ("docker" before "doc").
_KEYWORD_RULES: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("🎙", ("voice", "audio", "tts", "whisper", "vocale", "speech", "podcast")),
    ("🤖", ("bot", "agent", "ai", "llm", "claude", "gpt")),
    ("🧠", ("ml", "model", "train", "nn", "pqc", "quantum", "crypto", "cipher")),
    ("🏠", ("home", "hassio", "homeassistant", "smarthome", "iot")),
    ("🐟", ("docker", "podman", "k8s", "container", "compose")),
    ("💻", ("api", "rest", "grpc", "backend", "endpoint", "server")),
    ("💬", ("web", "frontend", "ui", "site", "landing")),
    ("📚", ("repo", "fork", "port", "upstream", "monorepo")),
    ("🪩", ("mirror", "sync", "replica", "backup")),
    ("🧪", ("test", "lab", "experiment", "spike", "poc", "analyzer", "trial")),
    ("🖨", ("tool", "script", "util", "fix", "patch")),
    ("📝", ("doc", "docs", "readme", "guide", "wiki", "notes")),
    ("🔭", ("planner", "plan", "roadmap", "task", "todo", "backlog")),
    ("🧼", ("config", "infra", "ansible", "deploy", "devops", "setup")),
)

# Applied icons this run: window_id -> emoji (the avatar is identity, not
# state; never re-apply within a run).
_applied: dict[str, str] = {}
_bot: Bot | None = None


def reset_for_testing() -> None:
    """Clear per-run state (tests only)."""
    _applied.clear()
    global _bot
    _bot = None


def _client() -> Bot:
    """The extension's own API-only bot (never polls)."""
    global _bot
    if _bot is None:
        _bot = Bot(token=app_config.telegram_bot_token)
    return _bot


def _name_candidates(window_name: str, cwd: str = "") -> list[str]:
    """Lowercased lookup candidates for the explicit mapping."""
    out: list[str] = []
    lowered = window_name.lower()
    out.append(lowered)
    out.extend(seg.strip().lower() for seg in lowered.split("▸") if seg.strip())
    if cwd:
        basename = cwd.rstrip("/").rsplit("/", 1)[-1].lower()
        if basename:
            out.append(basename)
    return out


def _text_tokens(window_name: str, cwd: str = "") -> set[str]:
    """Tokens for keyword matching: words and hyphen/underscore parts."""
    basename = cwd.rsplit("/", 1)[-1] if cwd else ""
    text = f"{window_name} {basename}".lower()
    rough = text.replace("-", " ").replace("_", " ").split()
    return {t for t in rough if t}


def _feature_on() -> bool:
    cfg = load_config()
    return bool(cfg.topic_icons) or cfg.topic_icon_heuristics


def resolve_icon_emoji(window_name: str, cwd: str = "") -> str | None:
    """Resolve the identity emoji for a window via the strategy chain."""
    cfg = load_config()
    for cand in _name_candidates(window_name, cwd):
        if cand in cfg.topic_icons:
            return cfg.topic_icons[cand]
    if not cfg.topic_icon_heuristics:
        return None
    tokens = _text_tokens(window_name, cwd)
    for emoji, words in _KEYWORD_RULES:
        if tokens & set(words):
            return emoji
    digest = hashlib.sha256(window_name.encode("utf-8")).digest()
    return _HASH_POOL[digest[0] % len(_HASH_POOL)]


async def fetch_allowed_icon_ids() -> dict[str, str] | None:
    """emoji -> custom_emoji_id from Telegram's forum icon set.

    ``None`` means the fetch failed (transient): callers must not latch,
    a later pass retries.
    """
    try:
        stickers = await _client().get_forum_topic_icon_stickers()
    except TelegramError as exc:
        logger.warning("could not fetch topic icon set", error=str(exc))
        return None
    out: dict[str, str] = {}
    for st in stickers or ():
        emoji = getattr(st, "emoji", None)
        cid = getattr(st, "custom_emoji_id", None)
        if emoji and cid:
            out[emoji] = str(cid)
    return out


async def on_topic_bound(
    *,
    chat_id: int | None,
    thread_id: int | None,
    window_id: str,
    window_name: str,
    cwd: str = "",
    **_extra: object,
) -> None:
    """Core domain event: a topic was just bound to a live window."""
    if chat_id is None or thread_id is None or not _feature_on():
        return
    await apply_topic_icon(chat_id, thread_id, window_id, window_name, cwd=cwd)


async def apply_topic_icon(
    chat_id: int,
    thread_id: int,
    window_id: str,
    window_name: str,
    cwd: str = "",
    allowed_ids: dict[str, str] | None = None,
) -> bool:
    """Set the topic's avatar once per run for this window."""
    if not _feature_on():
        return False
    if window_id in _applied:
        return False
    emoji = resolve_icon_emoji(window_name, cwd)
    if not emoji:
        return False
    if allowed_ids is None:
        allowed_ids = await fetch_allowed_icon_ids()
    if allowed_ids is None:
        return False  # transient fetch failure: not latched, retry later
    custom_id = allowed_ids.get(emoji)
    if not custom_id:
        logger.warning("emoji not in forum icon set, skipping", emoji=emoji)
        _applied[window_id] = emoji
        return False
    _applied[window_id] = emoji
    return await _edit_icon_safely(
        chat_id, thread_id, custom_id, window_id, window_name, emoji
    )


async def _edit_icon_safely(
    chat_id: int,
    thread_id: int,
    custom_id: str,
    window_id: str,
    window_name: str,
    emoji: str,
) -> bool:
    """One icon edit with the error contract. True = icon in place."""
    try:
        await _client().edit_forum_topic(
            chat_id=chat_id,
            message_thread_id=thread_id,
            icon_custom_emoji_id=custom_id,
        )
    except RetryAfter:
        # Icon edits share the per-chat editForumTopic bucket with title
        # renames (#199/#206): un-latch so a later pass retries, and put
        # the chat's rename machinery on cooldown instead of hammering
        # the next topics into the same saturated bucket.
        _applied.pop(window_id, None)
        pause_renames_for_flood(chat_id)
        logger.warning(
            "topic icon apply hit flood control; will retry on the next pass or bind",
            window=window_name,
        )
    except TelegramError as exc:
        msg = str(exc).lower().replace(" ", "_")
        if "not_modified" in msg or "topic_not_modified" in msg:
            return True  # Telegram's "already exactly this"
        logger.warning("topic icon apply failed", error=str(exc))
    else:
        logger.info("topic icon applied", window=window_name, emoji=emoji)
        return True
    return False


def _cwd_for_window(window_id: str) -> str:
    """Best-effort cwd from the persisted window state."""
    try:
        view = view_window(window_id)
        return view.cwd if view else ""
    except Exception:  # noqa: BLE001  # cosmetic: never block the pass
        return ""


async def apply_icons_for_bound_topics() -> tuple[int, int]:
    """Apply icons to every bound topic. Returns (applied, considered)."""
    if not _feature_on():
        return (0, 0)
    allowed = await fetch_allowed_icon_ids()
    if allowed is None:
        logger.warning("icon set unavailable; skipping this pass")
        return (0, 0)
    # Snapshot the bindings: the pass sleeps between edits while the poll
    # loop mutates the binding dicts concurrently.
    bindings = list(thread_router.iter_thread_bindings_with_chat())
    applied = 0
    spaced = False
    for user_id, chat_id, thread_id, window_id in bindings:
        if not window_id:
            continue
        if chat_id is None:
            # Legacy/unpromoted bindings carry no chat scope; resolve it
            # the way core's settle pass does.
            chat_id = thread_router.resolve_chat_id(user_id, thread_id)
            if chat_id is None:
                continue
        if renames_flood_paused(chat_id):
            logger.warning(
                "icon pass aborted: chat %d in rename flood cooldown", chat_id
            )
            break
        name = thread_router.get_display_name(window_id) or window_id
        if spaced and window_id not in _applied:
            await asyncio.sleep(ICON_EDIT_SPACING_SECONDS)
        spaced = True
        ok = await apply_topic_icon(
            chat_id,
            thread_id,
            window_id,
            name,
            cwd=_cwd_for_window(window_id),
            allowed_ids=allowed,
        )
        applied += int(ok)
    return (applied, len(bindings))


async def icons_command(update, context) -> None:
    """/icons: apply identity icons to every bound topic, then report."""
    if update.message is None or update.effective_user is None:
        return
    if not app_config.is_user_allowed(update.effective_user.id):
        return
    client = PTBTelegramClient(context.bot)
    thread = update.message.message_thread_id
    if not _feature_on():
        await rate_limit_send_message(
            client,
            update.effective_chat.id,
            "topic icons are off: add a [topic-icons] table or heuristics = true",
            **send_kwargs(thread),
        )
        return
    applied, considered = await apply_icons_for_bound_topics()
    await rate_limit_send_message(
        client,
        update.effective_chat.id,
        f"icons: {applied} applied, {considered} bound topics considered",
        **send_kwargs(thread),
    )
