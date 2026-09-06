"""Simple topic naming: the ccbot model, one shot per bind.

Topic titles are the user's namespace. On every topic.bound this module
sets a plain name once: the window's cwd basename (the project), with a
counter only when another bound topic already holds that name. It then
mirrors what a manual topic rename does in core: the stored clean name
is updated and the multiplexer window is renamed too, so the desktop
side and the Telegram side agree. Nothing ever rewrites the title
afterward: state lives in the status bubble, identity in the topic
icon.

Inert by default: enable with a ``[topic-names]`` table in the same
toolbar.toml (``style = "ccbot"``).
"""

from __future__ import annotations

import asyncio

import structlog
from ccgram.config import config as app_config
from ccgram.handlers.messaging_pipeline.message_sender import (
    rate_limit_send_message,
    send_kwargs,
)
from ccgram.handlers.status.topic_emoji import update_stored_topic_name
from ccgram.multiplexer import multiplexer as tmux_manager
from ccgram.session import session_manager
from ccgram.telegram_client import PTBTelegramClient
from ccgram.thread_router import thread_router
from ccgram.window_query import view_window
from telegram.error import RetryAfter, TelegramError
from telegram.ext import CommandHandler

from .config import load_config
from .icons import (
    ICON_EDIT_SPACING_SECONDS,
    pause_renames_for_flood,
    renames_flood_paused,
)
from .icons import _client as _shared_client

logger = structlog.get_logger()

_applied: set[tuple[int, int]] = set()


def reset_for_testing() -> None:
    """Clear per-run state (tests only)."""
    _applied.clear()


def _feature_on() -> bool:
    return load_config().topic_names == "ccbot"


def _simple_name(window_name: str, cwd: str, taken: set[str]) -> str:
    """The plain name for a topic: cwd basename, else the tab segment.

    A counter disambiguates collisions ("planner", "planner 2"), which
    is the only decoration the name ever gets.
    """
    base = ""
    if cwd:
        base = cwd.rstrip("/").rsplit("/", 1)[-1].strip()
    if not base and window_name:
        segments = [s.strip() for s in window_name.split("▸") if s.strip()]
        # The chain is Provider ▸ workspace ▸ tab ▸ pane: the tab is the
        # human-chosen part; drop a trailing pane token (pN) and take it.
        if segments and segments[-1][0] == "p" and segments[-1][1:].isdigit():
            segments = segments[:-1]
        base = segments[-1] if segments else ""
    base = base or "session"
    name = base
    n = 2
    while name.lower() in taken:
        name = f"{base} {n}"
        n += 1
    return name


async def on_topic_bound(
    *,
    chat_id: int | None,
    thread_id: int | None,
    window_id: str,
    window_name: str,
    cwd: str = "",
    **_extra: object,
) -> None:
    """Core domain event: name the freshly bound topic, once."""
    if chat_id is None or thread_id is None or not _feature_on():
        return
    key = (chat_id, thread_id)
    if key in _applied:
        return
    if not cwd:
        # Recovery binds carry the raw digest as window_name and no cwd:
        # resolve the project from the persisted window state instead,
        # or the topic would be named after the digest.
        try:
            view = view_window(window_id)
            cwd = view.cwd if view else ""
        except Exception:  # noqa: BLE001  # cosmetic
            logger.debug("cwd lookup at bind failed", window_id=window_id)
    if window_name.startswith("herdr-session-v1-"):
        window_name = thread_router.get_display_name(window_id) or window_name
    taken = _names_in_use(chat_id, exclude_thread=thread_id)
    name = _simple_name(window_name, cwd, taken)
    _applied.add(key)
    ok = await _rename_topic(chat_id, thread_id, name)
    if not ok:
        _applied.discard(key)
        return
    update_stored_topic_name(chat_id, thread_id, name)
    await _align_window(window_id, name)
    logger.info("topic named", name=name, window=window_name)


def _names_in_use(chat_id: int, exclude_thread: int | None = None) -> set[str]:
    """Lowercased names other topics of this chat hold or would take.

    Predicts the OTHER bindings' future names (cwd basename, else the
    last display segment): provider and workspace tokens are not names
    and must not force spurious counters.
    """
    taken: set[str] = set()
    for (
        _uid,
        cid,
        thread_id,
        window_id,
    ) in thread_router.iter_thread_bindings_with_chat():
        if cid != chat_id or not window_id or thread_id == exclude_thread:
            continue
        cwd = ""
        try:
            view = view_window(window_id)
            cwd = view.cwd if view else ""
        except Exception:  # noqa: BLE001  # cosmetic
            logger.debug(
                "cwd lookup during collision check failed", window_id=window_id
            )
        name = _simple_name(thread_router.get_display_name(window_id) or "", cwd, set())
        taken.add(name.lower())
    return taken


async def _align_window(window_id: str, name: str) -> None:
    """Mirror core's manual-rename alignment: window + display name."""
    try:
        if await tmux_manager.rename_window(window_id, name):
            session_manager.set_display_name(window_id, name)
    except Exception:  # noqa: BLE001  # cosmetic alignment; never block
        logger.debug("window rename after topic naming failed", window_id=window_id)


async def _rename_topic(chat_id: int, thread_id: int, name: str) -> bool:
    try:
        await _shared_client().edit_forum_topic(
            chat_id=chat_id,
            message_thread_id=thread_id,
            name=name,
        )
    except RetryAfter:
        # Renames share Telegram's per-chat editForumTopic bucket with
        # title renames and icon edits: join the shared cooldown instead
        # of hammering the next topics into it.
        pause_renames_for_flood(chat_id)
        logger.warning("topic rename hit flood control; later pass retries")
        return False
    except TelegramError as exc:
        msg = str(exc).lower().replace(" ", "_")
        if "not_modified" in msg or "topic_not_modified" in msg:
            return True  # already exactly this name: done
        logger.warning("topic rename failed", error=str(exc))
        return False
    except Exception as exc:  # noqa: BLE001  # report, never raise into emit
        logger.warning("topic rename failed", error=str(exc))
        return False
    return True


async def propose_names() -> list[tuple[int, int, str, str, str]]:
    """Dry run: (chat, thread, window, current, proposed) per binding."""
    rows: list[tuple[int, int, str, str, str]] = []
    ordered = []
    for (
        user_id,
        chat_id,
        thread_id,
        window_id,
    ) in thread_router.iter_thread_bindings_with_chat():
        if not window_id or chat_id is None:
            continue
        display = thread_router.get_display_name(window_id) or window_id
        cwd = ""
        try:
            view = view_window(window_id)
            cwd = view.cwd if view else ""
        except Exception:  # noqa: BLE001  # cosmetic
            logger.debug("cwd lookup failed during naming dry run", window_id=window_id)
        ordered.append((chat_id, thread_id, window_id, display, cwd))
    used: dict[int, set[str]] = {}
    for chat_id, thread_id, window_id, display, cwd in ordered:
        chat_taken = used.setdefault(chat_id, set())
        proposal = _simple_name(display, cwd, chat_taken)
        chat_taken.add(proposal.lower())
        rows.append((chat_id, thread_id, window_id, display, proposal))
    return rows


async def apply_names() -> tuple[int, int]:
    """Apply simple names to every bound topic. Returns (renamed, total)."""
    if not _feature_on():
        return (0, 0)
    rows = await propose_names()
    renamed = 0
    skipped = 0
    spaced = False
    for chat_id, thread_id, window_id, _current, name in rows:
        if renames_flood_paused(chat_id):
            skipped += 1
            continue
        if spaced:
            await asyncio.sleep(ICON_EDIT_SPACING_SECONDS)
        spaced = True
        if await _rename_topic(chat_id, thread_id, name):
            update_stored_topic_name(chat_id, thread_id, name)
            await _align_window(window_id, name)
            _applied.add((chat_id, thread_id))
            renamed += 1
        else:
            skipped += 1
    return (renamed, len(rows), skipped)


async def names_command(update, context) -> None:
    """/names: dry run by default, /names apply executes."""
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
            'topic naming is off: add [topic-names] style = "ccbot" to toolbar.toml',
            **send_kwargs(thread),
        )
        return
    apply_requested = bool(context.args) and context.args[0].lower() == "apply"
    if not apply_requested:
        rows = await propose_names()
        lines = [f"{cur}  ->  {new}" for _c, _t, _w, cur, new in rows]
        text = "proposed names:\n" + "\n".join(lines) if lines else "no bound topics"
        text += "\n\n/names apply to execute"
        await rate_limit_send_message(
            client, update.effective_chat.id, text, **send_kwargs(thread)
        )
        return
    renamed, total, skipped = await apply_names()
    await rate_limit_send_message(
        client,
        update.effective_chat.id,
        f"names: {renamed} of {total} topics renamed"
        + (f", {skipped} skipped (flood cooldown)" if skipped else ""),
        **send_kwargs(thread),
    )


def handler() -> CommandHandler:
    return CommandHandler("names", names_command)
