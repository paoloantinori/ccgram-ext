"""ccgram-ext: out-of-tree ccgram extensions (see ccgram docs/extension-seam.md).

Loaded by ccgram's extension seam via the ``ccgram.extensions`` entry
point group. Presence in the environment + a ``[reactions]`` table in
``~/.ccgram/toolbar.toml`` = active; either missing = inert.
"""

from __future__ import annotations

import structlog

from telegram.ext import MessageReactionHandler

from .reactions import handle_reaction_update, on_message_delivered

logger = structlog.get_logger()


def register(api) -> None:
    """Extension entry point: claim the reaction surface."""
    api.on("message.delivered", on_message_delivered)
    api.register_ptb_handler(
        MessageReactionHandler(handle_reaction_update), "message_reaction"
    )
    logger.info("ccgram-ext: reaction-triggered actions registered")
