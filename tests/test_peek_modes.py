"""Smoke tests for the peek metadata mode and dispatch piggyback signals.

Audit P1 #11: peek(mode='metadata', since=...) returns lite previews; every
tool response includes unread_count + new_since_last so callers can decide
whether peek is even worth the round-trip.
"""

from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path

import pytest


@pytest.fixture
def isolated_dispatch(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """Set env so dispatch server.py starts up against a tmp dir."""
    config_dir = tmp_path / "dispatch"
    monkeypatch.setenv("MCP_DISPATCH_DIR", str(config_dir))
    monkeypatch.setenv("MCP_DISPATCH_AGENT_ID", "alpha")
    # Stop the watchdog from spawning threads we'd have to clean up
    monkeypatch.setenv("MCP_DISPATCH_NO_WATCH", "1")
    # Force a fresh import of server.py since module-level setup pins paths
    sys.modules.pop("server", None)
    repo_root = Path(__file__).resolve().parent.parent
    sys.path.insert(0, str(repo_root))
    import server as srv  # noqa: E402

    yield srv

    sys.modules.pop("server", None)
    sys.path.remove(str(repo_root))


def _deliver(srv, sender: str, body: str, target: str = "alpha") -> str:
    """Drop a message into target's inbox directly without invoking the tool."""
    inbox = srv.DISPATCH_DIR / target
    inbox.mkdir(parents=True, exist_ok=True)
    ts = str(int(time.time() * 1000))
    msg_id = f"msg-{ts[-8:]}"
    msg = {
        "id": msg_id,
        "from": sender,
        "to": target,
        "timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "priority": "normal",
        "content": body,
        "payload": None,
        "thread_id": None,
        "reply_to": None,
        "ttl": None,
        "must_read": False,
        "state": "pending",
    }
    path = inbox / f"{ts}-{sender}.json"
    path.write_text(json.dumps(msg))
    return msg_id


def test_with_pending_attaches_unread_count_and_new_since_last(isolated_dispatch) -> None:
    srv = isolated_dispatch
    # Prime the floor at module load
    srv._LAST_PIGGYBACK_FLOOR = 0.0

    _deliver(srv, "beta", "hi alpha")
    _deliver(srv, "gamma", "hi alpha 2")
    result = srv._with_pending({"sent": True})
    assert result["unread_count"] == 2
    assert result["new_since_last"] >= 1  # at least one new since last observation


def test_peek_metadata_mode_returns_lite_previews(isolated_dispatch) -> None:
    srv = isolated_dispatch
    _deliver(srv, "beta", "a" * 500)

    result = srv.peek_tool(mode="metadata")
    assert result["mode"] == "metadata"
    assert result["count"] == 1
    msg = result["messages"][0]
    assert set(msg.keys()) >= {"id", "from", "ts", "priority", "preview_120", "size_bytes", "state"}
    assert len(msg["preview_120"]) == 120
    assert msg["from"] == "beta"


def test_peek_metadata_does_not_mark_read(isolated_dispatch) -> None:
    """Metadata mode is for defensive polling — it shouldn't consume the unread signal."""
    srv = isolated_dispatch
    _deliver(srv, "beta", "hello")

    # Peek in metadata mode
    r1 = srv.peek_tool(mode="metadata")
    # Inbox file should still report pending state on the next read
    r2 = srv.peek_tool(mode="metadata")
    assert r1["messages"][0]["state"] == "pending"
    assert r2["messages"][0]["state"] == "pending"


def test_peek_full_mode_marks_read(isolated_dispatch) -> None:
    """Full mode is the canonical consumption path — it should mark read."""
    srv = isolated_dispatch
    _deliver(srv, "beta", "hello")

    r1 = srv.peek_tool(mode="full")
    assert r1["count"] == 1
    # Second peek (default = pending only) should return nothing
    r2 = srv.peek_tool(mode="full")
    assert r2["count"] == 0


def test_peek_since_filters_messages_by_timestamp(isolated_dispatch) -> None:
    srv = isolated_dispatch
    _deliver(srv, "beta", "old msg")
    # Sleep 1 second so a second timestamp differs
    time.sleep(1.1)
    later_ts = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    time.sleep(0.5)
    _deliver(srv, "gamma", "new msg")

    result = srv.peek_tool(mode="full", since=later_ts)
    # Only the post-later_ts message should come through
    contents = [m.get("content") for m in result["messages"]]
    assert "new msg" in contents
    # Note: the "old msg" was filtered out
    assert "old msg" not in contents


def test_peek_metadata_preview_120_truncates_correctly(isolated_dispatch) -> None:
    srv = isolated_dispatch
    short_body = "tiny"
    _deliver(srv, "beta", short_body)

    result = srv.peek_tool(mode="metadata")
    assert result["messages"][0]["preview_120"] == short_body


def test_with_pending_does_not_redeliver_old_message_as_new(isolated_dispatch) -> None:
    """Epsilon's #4 finding — _mark_read rewrites file mtime AFTER the
    new_since_last count snapshot, so on the next piggyback the rewritten
    file appears `> floor` and gets counted as fresh. Fix: snap the floor
    AFTER mark_read so the rewrites sit at the floor, not above it."""
    srv = isolated_dispatch
    srv._LAST_PIGGYBACK_FLOOR = 0.0

    _deliver(srv, "beta", "first message")
    first = srv._with_pending({"sent": True})
    assert first["new_since_last"] == 1
    assert first.get("_dispatch_count") == 1

    # No new messages delivered. The next piggyback should NOT see the
    # already-delivered message as fresh again, even though _mark_read
    # rewrote its file (bumping mtime).
    second = srv._with_pending({"sent": True})
    assert second["new_since_last"] == 0
    assert "_dispatch_count" not in second or second.get("_dispatch_count") is None


def test_with_pending_signals_genuinely_new_message_after_first_delivery(isolated_dispatch) -> None:
    """Sanity guard: the floor advance does not swallow genuine new traffic."""
    srv = isolated_dispatch
    srv._LAST_PIGGYBACK_FLOOR = 0.0

    _deliver(srv, "beta", "first")
    srv._with_pending({"sent": True})

    # Sleep to guarantee the new message has a distinctly higher mtime than
    # the rewrite floor.
    time.sleep(1.1)
    _deliver(srv, "gamma", "second")
    third = srv._with_pending({"sent": True})
    assert third["new_since_last"] == 1
