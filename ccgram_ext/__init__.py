"""ccgram-ext: out-of-tree ccgram extensions (see ccgram docs/extension-seam.md).

Features: reaction-triggered actions; topic identity icons; simple topic names.

Loaded by ccgram's extension seam via the ``ccgram.extensions`` entry
point group. Presence in the environment + a ``[reactions]`` table in
``~/.ccgram/toolbar.toml`` = active; either missing = inert.
"""

from __future__ import annotations

import structlog
from telegram.ext import CommandHandler, MessageReactionHandler

from .icons import icons_command, on_topic_bound
from .naming import handler as names_handler
from .naming import on_topic_bound as on_topic_bound_name
from .reactions import handle_reaction_update, on_message_delivered

logger = structlog.get_logger()


def register(api) -> None:
    """Extension entry point: reactions, topic icons, simple topic names."""
    api.on("message.delivered", on_message_delivered)
    api.register_ptb_handler(
        MessageReactionHandler(handle_reaction_update), "message_reaction"
    )
    api.on("topic.bound", on_topic_bound)
    api.on("topic.bound", on_topic_bound_name)
    api.register_ptb_handler(names_handler(), "message")
    api.register_ptb_handler(CommandHandler("icons", icons_command), "message")
    logger.info("ccgram-ext: reactions + topic icons + names registered")
