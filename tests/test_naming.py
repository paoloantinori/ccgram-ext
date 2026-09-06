"""Behavioral tests for the simple topic naming module."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

import ccgram_ext.naming as N
from ccgram_ext.config import reset_cache_for_testing


@pytest.fixture(autouse=True)
def _isolate(monkeypatch, tmp_path):
    N.reset_for_testing()
    reset_cache_for_testing()
    monkeypatch.setenv("CCGRAM_TOOLBAR_CONFIG", str(tmp_path / "toolbar.toml"))
    yield
    N.reset_for_testing()
    reset_cache_for_testing()


def _cfg(tmp_path, body=""):
    p = tmp_path / "toolbar.toml"
    p.write_text(body)
    reset_cache_for_testing()


class TestSimpleName:
    def test_cwd_basename_wins(self):
        assert (
            N._simple_name("Claude ▸ x ▸ y ▸ p1", "/home/u/planner", set()) == "planner"
        )

    def test_fallback_tab_segment_without_pane(self):
        assert N._simple_name("Claude ▸ planner ▸ zai ▸ p1", "", set()) == "zai"

    def test_fallback_single_segment(self):
        assert N._simple_name("planner", "", set()) == "planner"

    def test_collision_counter(self):
        assert N._simple_name("x", "/a/ccgram", {"ccgram"}) == "ccgram 2"
        assert N._simple_name("x", "/a/ccgram", {"ccgram", "ccgram 2"}) == "ccgram 3"

    def test_last_resort(self):
        assert N._simple_name("", "", set()) == "session"


class TestListener:
    async def test_off_without_config(self, tmp_path, monkeypatch):
        _cfg(tmp_path, "")

        async def fail_client():
            raise AssertionError("no API calls")

        monkeypatch.setattr(N, "_shared_client", fail_client)
        await N.on_topic_bound(
            chat_id=1, thread_id=2, window_id="w", window_name="x ▸ y", cwd="/a/b"
        )

    async def test_names_once_and_aligns(self, tmp_path, monkeypatch):
        _cfg(tmp_path, '[topic-names]\nstyle = "ccbot"\n')
        edits, stored, renames = [], [], []

        class Bot:
            async def edit_forum_topic(self, **kw):
                edits.append(kw)

        monkeypatch.setattr(N, "_shared_client", lambda: Bot())
        monkeypatch.setattr(
            N, "update_stored_topic_name", lambda c, t, n: stored.append((c, t, n))
        )

        async def rename_window(wid, name):
            renames.append((wid, name))
            return True

        monkeypatch.setattr(
            N, "tmux_manager", SimpleNamespace(rename_window=rename_window)
        )
        router = SimpleNamespace(
            iter_thread_bindings_with_chat=lambda: iter([(1, 1, 5, "other")]),
            get_display_name=lambda w: "Claude ▸ other ▸ tab ▸ p2",
        )
        monkeypatch.setattr(N, "thread_router", router)

        await N.on_topic_bound(
            chat_id=1,
            thread_id=2,
            window_id="w1",
            window_name="Claude ▸ x",
            cwd="/proj/ccgram",
        )
        # second bind event for the same topic: one-shot, no re-edit
        await N.on_topic_bound(
            chat_id=1,
            thread_id=2,
            window_id="w1",
            window_name="Claude ▸ x",
            cwd="/proj/ccgram",
        )
        assert [e["name"] for e in edits] == ["ccgram"]
        assert stored == [(1, 2, "ccgram")]
        assert renames == [("w1", "ccgram")]

    async def test_failed_rename_does_not_latch(self, tmp_path, monkeypatch):
        _cfg(tmp_path, '[topic-names]\nstyle = "ccbot"\n')

        class BadBot:
            async def edit_forum_topic(self, **kw):
                raise RuntimeError("nope")

        monkeypatch.setattr(N, "_shared_client", lambda: BadBot())
        router = SimpleNamespace(
            iter_thread_bindings_with_chat=lambda: iter([]),
            get_display_name=lambda w: "",
        )
        monkeypatch.setattr(N, "thread_router", router)
        await N.on_topic_bound(
            chat_id=1, thread_id=2, window_id="w", window_name="x", cwd="/a/b"
        )
        assert (1, 2) not in N._applied


class TestPropose:
    async def test_dry_run_rows_with_disambiguation(self, tmp_path, monkeypatch):
        _cfg(tmp_path, '[topic-names]\nstyle = "ccbot"\n')
        router = SimpleNamespace(
            iter_thread_bindings_with_chat=lambda: iter(
                [(1, 10, 100, "wA"), (1, 10, 101, "wB"), (1, 10, 102, None)]
            ),
            get_display_name=lambda w: {
                "wA": "Claude ▸ planner ▸ zai ▸ p1",
                "wB": "x ▸ planner",
            }[w],
        )
        monkeypatch.setattr(N, "thread_router", router)

        def view(wid):
            return SimpleNamespace(
                cwd={"wA": "/repo/planner", "wB": "/other/planner"}[wid]
            )

        monkeypatch.setattr(N, "view_window", view)
        monkeypatch.setattr(N, "renames_flood_paused", lambda c: False)
        rows = await N.propose_names()
        proposals = [r[4] for r in rows]
        assert proposals == ["planner", "planner 2"]


class TestReviewFixes:
    def test_degenerate_names(self):
        assert N._simple_name("▸", "", set()) == "session"
        assert N._simple_name("▸ p1", "", set()) == "session"
        assert N._simple_name("   ", "", set()) == "session"

    def test_names_in_use_predicts_future_names(self, monkeypatch):
        router = SimpleNamespace(
            iter_thread_bindings_with_chat=lambda: iter(
                [(1, 10, 100, "wA"), (1, 10, 101, "wB")]
            ),
            get_display_name=lambda w: {
                "wA": "Claude ▸ x ▸ zai ▸ p1",
                "wB": "ccgram ▸ 2",
            }[w],
        )
        monkeypatch.setattr(N, "thread_router", router)
        monkeypatch.setattr(
            N,
            "view_window",
            lambda wid: SimpleNamespace(cwd={"wA": "/repo/zai", "wB": ""}[wid]),
        )
        # wA predicts "zai" (its cwd), wB predicts "2"? no: fallback last
        # segment "2"... the chain "ccgram ▸ 2" has no pane token, so the
        # tab segment is "2" itself: the prediction follows the rule.
        assert N._names_in_use(10, exclude_thread=101) == {"zai"}

    async def test_rename_retry_after_pauses(self, monkeypatch):
        from telegram.error import RetryAfter

        paused = []

        class FloodBot:
            async def edit_forum_topic(self, **kw):
                raise RetryAfter(20)

        monkeypatch.setattr(N, "_shared_client", lambda: FloodBot())
        monkeypatch.setattr(N, "pause_renames_for_flood", lambda c: paused.append(c))
        ok = await N._rename_topic(1, 2, "x")
        assert ok is False and paused == [1]

    async def test_apply_reports_skipped_and_aligns(self, tmp_path, monkeypatch):
        _cfg(tmp_path, '[topic-names]\nstyle = "ccbot"\n')
        edits, aligned = [], []

        class OkBot:
            async def edit_forum_topic(self, **kw):
                edits.append(kw)

        monkeypatch.setattr(N, "_shared_client", lambda: OkBot())

        async def align(wid, name):
            aligned.append((wid, name))

        monkeypatch.setattr(N, "_align_window", align)
        monkeypatch.setattr(
            N,
            "renames_flood_paused",
            lambda chat: chat == 99,
        )

        async def fast_sleep(_s):
            return None

        monkeypatch.setattr(N.asyncio, "sleep", fast_sleep)
        # two chats: 10 gets two same-basename topics, 99 is flood-paused
        router = SimpleNamespace(
            iter_thread_bindings_with_chat=lambda: iter(
                [(1, 10, 100, "wA"), (1, 10, 101, "wB"), (1, 99, 102, "wC")]
            ),
            get_display_name=lambda w: {
                "wA": "a ▸ planner ▸ t ▸ p1",
                "wB": "b ▸ planner",
                "wC": "c",
            }[w],
        )
        monkeypatch.setattr(N, "thread_router", router)
        monkeypatch.setattr(
            N,
            "view_window",
            lambda wid: SimpleNamespace(
                cwd={"wA": "/r/planner", "wB": "/o/planner", "wC": "/z/other"}[wid]
            ),
        )
        monkeypatch.setattr(N, "update_stored_topic_name", lambda c, t, n: None)
        renamed, total, skipped = await N.apply_names()
        assert (renamed, total, skipped) == (2, 3, 1)
        assert [e["name"] for e in edits] == ["planner", "planner 2"]
        assert aligned == [("wA", "planner"), ("wB", "planner 2")]
