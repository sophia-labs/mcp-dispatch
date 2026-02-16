#!/usr/bin/env python3
"""Sophia Dispatch — Local inter-agent messaging for Claude Code instances.

Each Claude Code window runs its own dispatch MCP server (stdio transport).
They share ~/.sophia/dispatch/ as a filesystem message relay.

Agent identity:
  Set SOPHIA_AGENT_ID=alpha|beta|gamma|delta in the shell before launching
  Claude Code to pin a stable identity. Without it, IDs are auto-claimed
  from the pool in startup order (fragile across restarts).

  Example:
    SOPHIA_AGENT_ID=beta claude     # This session is always Beta

Tools:
  dispatch(message, target)  — send to one agent or all
  inbox()                    — read + clear pending messages
  heartbeat()                — check for messages between work phases
  who()                      — list connected agents
"""

from __future__ import annotations

import atexit
import json
import os
import signal
import sys
import time
import uuid
from pathlib import Path
from typing import Optional

from mcp.server.fastmcp import FastMCP

# Optional: filesystem watcher for real-time stderr alerts
try:
    from watchdog.observers import Observer
    from watchdog.events import FileSystemEventHandler, FileCreatedEvent

    HAS_WATCHDOG = True
except ImportError:
    HAS_WATCHDOG = False

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

AGENT_IDS = ["alpha", "beta", "gamma", "delta"]
DISPATCH_DIR = Path(
    os.environ.get("DISPATCH_DIR", os.path.expanduser("~/.sophia/dispatch"))
)

# ---------------------------------------------------------------------------
# Agent ID management
# ---------------------------------------------------------------------------


def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def _setup_dirs() -> None:
    DISPATCH_DIR.mkdir(parents=True, exist_ok=True)
    (DISPATCH_DIR / ".presence").mkdir(exist_ok=True)
    for aid in AGENT_IDS:
        (DISPATCH_DIR / aid).mkdir(exist_ok=True)


def _claim_id() -> str:
    """Claim an agent ID.

    If SOPHIA_AGENT_ID is set, use that directly (allows deterministic
    identity across restarts). Otherwise auto-claim the first available
    slot from the pool.
    """
    presence_dir = DISPATCH_DIR / ".presence"

    # Explicit identity via env var — preferred for stable assignment
    explicit = os.environ.get("SOPHIA_AGENT_ID", "").strip().lower()
    if explicit:
        if explicit not in AGENT_IDS:
            raise ValueError(
                f"SOPHIA_AGENT_ID='{explicit}' is not a valid agent ID. "
                f"Valid IDs: {', '.join(AGENT_IDS)}"
            )
        # Check if slot is held by a *different* live process
        pf = presence_dir / f"{explicit}.json"
        if pf.exists():
            try:
                data = json.loads(pf.read_text())
                other_pid = data.get("pid", -1)
                if other_pid != os.getpid() and _pid_alive(other_pid):
                    print(
                        f"[dispatch] WARNING: {explicit} claimed by PID {other_pid}, taking over",
                        file=sys.stderr,
                    )
            except (json.JSONDecodeError, KeyError, OSError):
                pass
        pf.write_text(
            json.dumps(
                {
                    "agent_id": explicit,
                    "pid": os.getpid(),
                    "started": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                }
            )
        )
        return explicit

    # Auto-claim: first available slot
    for aid in AGENT_IDS:
        pf = presence_dir / f"{aid}.json"
        if pf.exists():
            try:
                data = json.loads(pf.read_text())
                if _pid_alive(data.get("pid", -1)):
                    continue  # Slot taken by a live process
            except (json.JSONDecodeError, KeyError, OSError):
                pass  # Stale or corrupt — reclaim

        # Claim this slot
        pf.write_text(
            json.dumps(
                {
                    "agent_id": aid,
                    "pid": os.getpid(),
                    "started": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                }
            )
        )
        return aid

    raise RuntimeError(
        f"All {len(AGENT_IDS)} agent slots are claimed by live processes. "
        "Stop an existing instance first."
    )


def _release_id(agent_id: str) -> None:
    pf = DISPATCH_DIR / ".presence" / f"{agent_id}.json"
    try:
        pf.unlink(missing_ok=True)
    except OSError:
        pass


# ---------------------------------------------------------------------------
# Message I/O
# ---------------------------------------------------------------------------


def _atomic_write(path: Path, data: dict) -> None:
    """Write JSON atomically via tmp + rename."""
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, indent=2))
    tmp.rename(path)


def _drain_inbox(agent_id: str) -> list[dict]:
    """Read and delete all messages from an agent's inbox."""
    inbox = DISPATCH_DIR / agent_id
    messages: list[dict] = []
    for f in sorted(inbox.glob("*.json")):
        try:
            messages.append(json.loads(f.read_text()))
            f.unlink()
        except (json.JSONDecodeError, OSError):
            pass
    return messages


def _send(
    from_id: str,
    to: str,
    content: str,
    priority: str = "normal",
    ref: str | None = None,
) -> dict:
    """Write a message to the target's inbox. Fan-out for 'all'."""
    msg = {
        "id": f"msg-{uuid.uuid4().hex[:8]}",
        "from": from_id,
        "to": to,
        "timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "priority": priority,
        "content": content,
        "ref": ref,
    }
    ts = str(int(time.time() * 1000))

    if to == "all":
        for aid in AGENT_IDS:
            if aid != from_id:
                _atomic_write(DISPATCH_DIR / aid / f"{ts}-{from_id}.json", msg)
    else:
        if to not in AGENT_IDS:
            raise ValueError(f"Unknown agent '{to}'. Valid targets: {', '.join(AGENT_IDS)}, all")
        _atomic_write(DISPATCH_DIR / to / f"{ts}-{from_id}.json", msg)

    return msg


# ---------------------------------------------------------------------------
# Filesystem watcher (Layer 2: stderr alerts for Eschaton)
# ---------------------------------------------------------------------------


def _start_watcher(agent_id: str) -> None:
    """Watch inbox for new files and print alerts to stderr."""
    if not HAS_WATCHDOG:
        print("[dispatch] watchdog not installed — no real-time alerts", file=sys.stderr)
        return

    class _Handler(FileSystemEventHandler):
        def on_created(self, event):
            if not isinstance(event, FileCreatedEvent):
                return
            if not event.src_path.endswith(".json"):
                return
            try:
                msg = json.loads(Path(event.src_path).read_text())
                sender = msg.get("from", "?")
                preview = msg.get("content", "")[:80]
                pri = msg.get("priority", "normal")
                marker = "!!!" if pri == "urgent" else ">>>"
                print(
                    f"\n[dispatch {marker}] Message from {sender}: {preview}",
                    file=sys.stderr,
                    flush=True,
                )
            except Exception:
                pass

    observer = Observer()
    observer.schedule(_Handler(), str(DISPATCH_DIR / agent_id), recursive=False)
    observer.daemon = True
    observer.start()


# ---------------------------------------------------------------------------
# MCP Server
# ---------------------------------------------------------------------------

_setup_dirs()
AGENT_ID = _claim_id()
print(f"[dispatch] I am {AGENT_ID} (PID {os.getpid()})", file=sys.stderr)

atexit.register(lambda: _release_id(AGENT_ID))
signal.signal(signal.SIGTERM, lambda *_: (_release_id(AGENT_ID), sys.exit(0)))

_start_watcher(AGENT_ID)

mcp = FastMCP(
    "dispatch",
    instructions=(
        f"This is the Sophia Dispatch server. You are agent '{AGENT_ID}'. "
        "Use dispatch() to send messages to other Sophia instances, "
        "heartbeat() to check for incoming messages between work phases, "
        "inbox() to explicitly read pending messages, and who() to see "
        "who's online. Messages from others are also included in every "
        "tool response (piggyback delivery)."
    ),
)


def _with_pending(result: dict) -> dict:
    """Attach any pending inbox messages to a tool response."""
    messages = _drain_inbox(AGENT_ID)
    if messages:
        result["_dispatches"] = messages
        result["_dispatch_count"] = len(messages)
    return result


@mcp.tool(
    name="dispatch",
    description=(
        "Send a message to another Sophia instance or all instances. "
        "Target can be 'all', 'alpha', 'beta', 'gamma', or 'delta'. "
        "Use priority='urgent' for time-sensitive messages. "
        "Returns confirmation plus any pending messages for you."
    ),
)
def dispatch_tool(
    message: str,
    target: str = "all",
    priority: str = "normal",
    ref: Optional[str] = None,
) -> dict:
    """Send a message to other Sophia instances."""
    sent = _send(AGENT_ID, target, message, priority, ref)
    return _with_pending(
        {
            "sent": True,
            "id": sent["id"],
            "from": AGENT_ID,
            "to": target,
            "priority": priority,
        }
    )


@mcp.tool(
    name="inbox",
    description=(
        "Read and clear all pending messages from other Sophia instances. "
        "Messages are deleted after reading."
    ),
)
def inbox_tool() -> dict:
    """Read all pending messages."""
    messages = _drain_inbox(AGENT_ID)
    return {
        "agent_id": AGENT_ID,
        "messages": messages,
        "count": len(messages),
    }


@mcp.tool(
    name="heartbeat",
    description=(
        "Check for pending messages without doing anything else. "
        "Call this between major work phases to see if other Sophias "
        "need your attention. Returns pending messages if any."
    ),
)
def heartbeat_tool() -> dict:
    """No-op that triggers piggyback delivery."""
    return _with_pending(
        {
            "agent_id": AGENT_ID,
            "status": "alive",
        }
    )


@mcp.tool(
    name="who",
    description="List all currently connected Sophia instances and their status.",
)
def who_tool() -> dict:
    """List connected agents via presence files."""
    presence_dir = DISPATCH_DIR / ".presence"
    agents: list[dict] = []
    for pf in sorted(presence_dir.glob("*.json")):
        try:
            data = json.loads(pf.read_text())
            pid = data.get("pid", -1)
            if _pid_alive(pid):
                agents.append(data)
            else:
                pf.unlink()  # Clean stale
        except (json.JSONDecodeError, OSError):
            pass

    return {
        "self": AGENT_ID,
        "agents": agents,
        "count": len(agents),
    }


if __name__ == "__main__":
    mcp.run()
