"""Reaction configuration: the [reactions] table from ccgram's toolbar.toml.

The extension owns its config surface: it reads the SAME file the core
toolbar reads (``$CCGRAM_TOOLBAR_CONFIG`` or ``~/.ccgram/toolbar.toml``)
but only the ``[reactions]``, ``[reactions.speak]`` and ``[actions]``
sections it needs. No core config code is imported.
"""

from __future__ import annotations

import structlog
import os
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

logger = structlog.get_logger()


@dataclass(frozen=True)
class ExtConfig:
    reaction_map: dict[str, str] = field(default_factory=dict)
    reaction_speak: dict[str, str] = field(default_factory=dict)


def _toolbar_path() -> Path:
    env = os.environ.get("CCGRAM_TOOLBAR_CONFIG", "")
    if env:
        return Path(env)
    return Path.home() / ".ccgram" / "toolbar.toml"


def _parse(raw: dict) -> ExtConfig:
    section = raw.get("reactions")
    if not isinstance(section, dict):
        return ExtConfig()
    section = dict(section)  # do not mutate the parsed document
    reaction_map: dict[str, str] = {}
    speak: dict[str, str] = {}

    speak_raw = section.pop("speak", None)
    if isinstance(speak_raw, dict):
        for k, v in speak_raw.items():
            if isinstance(v, str | int | float) and not isinstance(v, bool):
                speak[str(k)] = str(v)
            else:
                logger.warning("reactions.speak: skipping non-scalar %r", k)

    actions = raw.get("actions")
    actions = actions if isinstance(actions, dict) else {}
    for emoji, name in section.items():
        if not isinstance(emoji, str) or not isinstance(name, str):
            logger.warning("reactions: skipping malformed entry %r", emoji)
            continue
        if name in ("screenshot", "speak"):
            reaction_map[emoji] = name
            continue
        action = actions.get(name)
        action_type = action.get("type") if isinstance(action, dict) else None
        if action_type not in ("key", "text"):
            # builtin-typed actions need a CallbackQuery; they cannot run
            # from a reaction. Rejected at load, not mid-reaction.
            logger.warning(
                "reactions: %r maps to %r which is missing or not a"
                " key/text action (ignored)",
                emoji,
                name,
            )
            continue
        reaction_map[emoji] = name
    return ExtConfig(reaction_map=reaction_map, reaction_speak=speak)


_cache: tuple[Path, float, ExtConfig] | None = None


def load_config() -> ExtConfig:
    """Parse (and mtime-cache) the [reactions] table; empty = feature off."""
    global _cache
    path = _toolbar_path()
    try:
        mtime = path.stat().st_mtime
    except OSError:
        return ExtConfig()
    if _cache and _cache[0] == path and _cache[1] == mtime:
        return _cache[2]
    try:
        with open(path, "rb") as f:
            cfg = _parse(tomllib.load(f))
    except (tomllib.TOMLDecodeError, OSError) as exc:
        logger.warning("reactions config unreadable (%s); feature off", exc)
        cfg = ExtConfig()
    _cache = (path, mtime, cfg)
    return cfg


def reset_cache_for_testing() -> None:
    global _cache
    _cache = None
