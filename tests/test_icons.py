"""Behavioral tests for the topic identity icons extension."""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from telegram.error import RetryAfter, TelegramError

import ccgram_ext.icons as I
from ccgram_ext.config import load_config, reset_cache_for_testing


@pytest.fixture(autouse=True)
def _isolate(monkeypatch, tmp_path):
    I.reset_for_testing()
    reset_cache_for_testing()
    monkeypatch.setenv("CCGRAM_TOOLBAR_CONFIG", str(tmp_path / "toolbar.toml"))
    yield
    I.reset_for_testing()
    reset_cache_for_testing()


def _write_cfg(tmp_path, body=""):
    p = tmp_path / "toolbar.toml"
    p.write_text(body)
    reset_cache_for_testing()
    return p


def _fake_ids(monkeypatch, ids):
    async def fake_fetch():
        return ids

    monkeypatch.setattr(I, "fetch_allowed_icon_ids", fake_fetch)


class _OkBot:
    def __init__(self):
        self.edits = []

    async def edit_forum_topic(self, **kw):
        self.edits.append(kw)
        return True


class _FloodBot:
    async def edit_forum_topic(self, **kw):
        raise RetryAfter(30)


class _EchoBot:
    async def edit_forum_topic(self, **kw):
        raise TelegramError("Bad Request: TOPIC_NOT_MODIFIED")


class _NoCallBot:
    async def edit_forum_topic(self, **kw):  # pragma: no cover
        raise AssertionError("no API calls expected")


class TestConfigParse:
    def test_explicit_map_and_heuristics_flag(self, tmp_path):
        _write_cfg(
            tmp_path,
            '[topic-icons]\nheuristics = true\n"ccgram" = "💻"\n"planner" = "🔭"\n',
        )
        cfg = load_config()
        assert cfg.topic_icons == {"ccgram": "💻", "planner": "🔭"}
        assert cfg.topic_icon_heuristics is True

    def test_no_section_means_off(self, tmp_path):
        _write_cfg(tmp_path, '[reactions]\n"👎" = "screenshot"\n')
        cfg = load_config()
        assert cfg.topic_icons == {} and cfg.topic_icon_heuristics is False


_FORUM_SET = frozenset(
    ["☕️", "☠️", "♂️", "⚡️", "⚽️", "⛅️", "✅", "✈️", "✍️", "❓", "❗️", "❤️", "⭐️", "🍓", "🍔", "🍕", "🍣", "🍹", "🍽", "🎂", "🎃", "🎄", "🎉", "🎓", "🎖", "🎙", "🎟", "🎤", "🎨", "🎩", "🎬", "🎭", "🎮", "🎵", "🎶", "🏀", "🏁", "🏆", "🏔", "🏕", "🏖", "🏛", "🏠", "🏴", "🐈", "🐟", "👀", "👑", "👜", "👠", "👦", "👧", "👨", "👩", "👮", "👶", "💃", "💄", "💅", "💉", "💊", "💎", "💘", "💡", "💬", "💰", "💱", "💸", "💻", "💼", "📁", "📆", "📈", "📉", "📚", "📝", "📣", "📰", "📱", "📺", "🔎", "🔝", "🔞", "🔥", "🔬", "🔭", "🔮", "🕺", "🖨", "🗣", "🗳", "🚂", "🚗", "🛃", "🛍", "🛒", "🛥", "🤖", "🤡", "🤰", "🦄", "🦠", "🦮", "🧠", "🧪", "🧮", "🧳", "🧼", "🩺", "🪖", "🪙", "🪩", "🪪", "🫦"]
)


class TestPoolMembership:
    def test_hash_pool_and_keyword_rules_all_in_forum_set(self):
        pool_missing = [e for e in I._HASH_POOL if e not in _FORUM_SET]
        rule_missing = [e for e, _ in I._KEYWORD_RULES if e not in _FORUM_SET]
        assert not pool_missing + rule_missing


class TestResolution:
    def test_explicit_by_segment_and_basename(self, tmp_path):
        _write_cfg(tmp_path, '[topic-icons]\n"ccbot-fork" = "🤖"\n"ccgram" = "💻"\n')
        assert I.resolve_icon_emoji("zai ▸ ccgram ▸ main") == "💻"
        assert I.resolve_icon_emoji("whatever", "/home/x/ccbot-fork") == "🤖"

    def test_heuristics_token_not_substring(self, tmp_path):
        _write_cfg(tmp_path, "[topic-icons]\nheuristics = true\n")
        assert I.resolve_icon_emoji("robotics lab") == "🧪"
        assert I.resolve_icon_emoji("voice notes") == "🎙"

    def test_heuristics_off_no_hash_fallback(self, tmp_path):
        _write_cfg(tmp_path, '[topic-icons]\n"named" = "📁"\n')
        assert I.resolve_icon_emoji("unnamed project") is None

    def test_hash_fallback_stable(self, tmp_path):
        _write_cfg(tmp_path, "[topic-icons]\nheuristics = true\n")
        a = I.resolve_icon_emoji("qqqq unmappable zzz")
        assert a == I.resolve_icon_emoji("qqqq unmappable zzz")
        assert a in I._HASH_POOL


class TestApply:
    async def test_feature_off_inert(self, tmp_path, monkeypatch):
        _write_cfg(tmp_path, "")
        monkeypatch.setattr(I, "_bot", _NoCallBot())
        _fake_ids(monkeypatch, {"💻": "cid"})
        assert await I.apply_topic_icon(1, 2, "w", "ccgram", cwd="/x/ccgram") is False

    async def test_applies_and_latches_once(self, tmp_path, monkeypatch):
        _write_cfg(tmp_path, '[topic-icons]\n"ccgram" = "💻"\n')
        bot = _OkBot()
        monkeypatch.setattr(I, "_bot", bot)
        _fake_ids(monkeypatch, {"💻": "cid1"})
        assert await I.apply_topic_icon(1, 2, "w", "ccgram") is True
        assert await I.apply_topic_icon(1, 2, "w", "ccgram") is False
        assert len(bot.edits) == 1
        assert bot.edits[0]["icon_custom_emoji_id"] == "cid1"

    async def test_emoji_not_in_set_skips_and_latches(self, tmp_path, monkeypatch):
        _write_cfg(tmp_path, '[topic-icons]\n"weird" = "🫠"\n')
        monkeypatch.setattr(I, "_bot", _NoCallBot())
        _fake_ids(monkeypatch, {})
        assert await I.apply_topic_icon(1, 2, "w", "weird") is False
        assert "w" in I._applied

    async def test_retry_after_unlatches_and_pauses(self, tmp_path, monkeypatch):
        _write_cfg(tmp_path, '[topic-icons]\n"ccgram" = "💻"\n')
        paused = []
        monkeypatch.setattr(I, "_bot", _FloodBot())
        _fake_ids(monkeypatch, {"💻": "cid1"})
        monkeypatch.setattr(
            I, "pause_renames_for_flood", lambda chat: paused.append(chat)
        )
        assert await I.apply_topic_icon(7, 2, "w", "ccgram") is False
        assert "w" not in I._applied and paused == [7]

    async def test_not_modified_counts_as_applied(self, tmp_path, monkeypatch):
        _write_cfg(tmp_path, '[topic-icons]\n"ccgram" = "💻"\n')
        monkeypatch.setattr(I, "_bot", _EchoBot())
        _fake_ids(monkeypatch, {"💻": "cid1"})
        assert await I.apply_topic_icon(1, 2, "w", "ccgram") is True


class TestTopicBoundListener:
    async def test_guards(self, tmp_path):
        _write_cfg(tmp_path, '[topic-icons]\n"ccgram" = "💻"\n')
        assert (
            await I.on_topic_bound(
                chat_id=None,
                thread_id=5,
                window_id="w",
                window_name="ccgram",
                cwd="",
            )
            is None
        )
        _write_cfg(tmp_path, "")
        assert (
            await I.on_topic_bound(
                chat_id=1,
                thread_id=5,
                window_id="w",
                window_name="ccgram",
                cwd="",
            )
            is None
        )


class TestPass:
    async def test_pass_applies_paced_and_counts(self, tmp_path, monkeypatch):
        _write_cfg(tmp_path, '[topic-icons]\n"a" = "💻"\n"b" = "📁"\n')
        bot = _OkBot()
        monkeypatch.setattr(I, "_bot", bot)
        _fake_ids(monkeypatch, {"💻": "c1", "📁": "c2"})

        async def fast_sleep(_s):
            return None

        monkeypatch.setattr(I.asyncio, "sleep", fast_sleep)
        monkeypatch.setattr(
            I,
            "thread_router",
            SimpleNamespace(
                iter_thread_bindings_with_chat=lambda: iter(
                    [(1, 10, 100, "wA"), (1, 10, 101, "wB"), (1, 10, 102, None)]
                ),
                get_display_name=lambda w: {"wA": "a", "wB": "b"}.get(w, w),
            ),
        )
        monkeypatch.setattr(I, "renames_flood_paused", lambda chat: False)
        monkeypatch.setattr(I, "_cwd_for_window", lambda w: "")
        applied, considered = await I.apply_icons_for_bound_topics()
        assert (applied, considered) == (2, 3)
        assert len(bot.edits) == 2
