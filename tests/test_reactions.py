"""Behavioral tests for the reaction-triggered actions extension."""

from __future__ import annotations

import time
from types import SimpleNamespace

import pytest

import ccgram_ext.reactions as R
from ccgram_ext.config import _parse, reset_cache_for_testing


def _reaction(emoji, *, chat=1, msg=10, user=7, old=()):
    nr = SimpleNamespace(new_reaction=[SimpleNamespace(emoji=emoji)])
    orr = [SimpleNamespace(emoji=e) for e in old]
    upd = SimpleNamespace(
        message_reaction=SimpleNamespace(
            chat=SimpleNamespace(id=chat),
            message_id=msg,
            user=SimpleNamespace(id=user),
            new_reaction=nr.new_reaction,
            old_reaction=orr,
        )
    )
    return upd


@pytest.fixture(autouse=True)
def _isolate(monkeypatch, tmp_path):
    R._tracked.clear()
    reset_cache_for_testing()
    monkeypatch.setattr(
        R.app_config, "is_user_allowed", lambda uid: True, raising=False
    )
    monkeypatch.setenv("CCGRAM_TOOLBAR_CONFIG", str(tmp_path / "toolbar.toml"))
    yield
    reset_cache_for_testing()


def _write_cfg(tmp_path, body=""):
    p = tmp_path / "toolbar.toml"
    p.write_text(body)
    return p


class TestConfigParse:
    def test_builtin_names_and_keytext_actions_map(self):
        cfg = _parse(
            {
                "reactions": {"👎": "screenshot", "🙈": "speak", "🔥": "esc"},
                "actions": {"esc": {"type": "key", "payload": "Escape"}},
            }
        )
        assert cfg.reaction_map == {
            "👎": "screenshot",
            "🙈": "speak",
            "🔥": "esc",
        }

    def test_builtin_typed_and_missing_actions_rejected(self):
        cfg = _parse(
            {
                "reactions": {"🔥": "shot"},
                "actions": {"shot": {"type": "builtin"}},
            }
        )
        assert cfg.reaction_map == {}

    def test_speak_scalars_kept_nonscalar_skipped(self):
        cfg = _parse(
            {
                "reactions": {
                    "speak": {
                        "url": "http://lan:3900/v1",
                        "timeout": 240,
                        "bad": {"nested": True},
                    }
                }
            }
        )
        assert cfg.reaction_speak == {"url": "http://lan:3900/v1", "timeout": "240"}

    def test_no_table_means_off(self, tmp_path):
        _write_cfg(tmp_path, "[actions.esc]\ntype='key'\npayload='Escape'\n")
        from ccgram_ext.config import load_config

        assert load_config().reaction_map == {}


class TestTracking:
    def test_delivered_event_records_and_bounded(self):
        for i in range(600):
            R.on_message_delivered(
                chat_id=1,
                message_id=i,
                window_id="w%d" % (i % 3),
                text="t%d" % i,
                thread_id=None,
            )
        assert len(R._tracked) == 500
        assert R._lookup(1, 599) is not None
        assert R._lookup(1, 0) is None

    def test_ttl_expiry(self, monkeypatch):
        R.on_message_delivered(
            chat_id=1, message_id=9, window_id="w", text="x", thread_id=42
        )
        entry = R._tracked[(1, 9)]
        aged = R._TrackedEntry(
            entry.window_id, time.monotonic() - 4000, entry.text, entry.thread_id
        )
        R._tracked[(1, 9)] = aged
        assert R._lookup(1, 9) is None


class TestResolve:
    def test_delta_only_emoji_fire(self):
        upd = _reaction("👎", old=("🙈",))
        cfg_map = {"👎": "screenshot", "🙈": "speak"}
        emoji, action = R._resolve(cfg_map, upd.message_reaction)
        assert (emoji, action) == ("👎", "screenshot")

    def test_unmapped_emoji_no_action(self):
        upd = _reaction("❤")
        assert R._resolve({"👎": "screenshot"}, upd.message_reaction)[1] is None


class TestHandlerGuard:
    async def test_inert_without_tracked_message(self):
        R.on_message_delivered(
            chat_id=1, message_id=10, window_id="w", text="x", thread_id=None
        )
        # config WITHOUT a [reactions] table: handler must return early
        assert await R.handle_reaction_update(_reaction("👎"), None) is None

    async def test_untracked_message_ignored(self, tmp_path):
        _write_cfg(tmp_path, '[reactions]\n"👎" = "screenshot"\n')
        upd = _reaction("👎", msg=999)
        assert await R.handle_reaction_update(upd, None) is None
