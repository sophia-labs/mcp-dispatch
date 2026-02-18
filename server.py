#!/usr/bin/env python3
"""MCP Dispatch — Local inter-agent messaging for Claude Code instances.

Each Claude Code window runs its own dispatch MCP server (stdio transport).
They share a filesystem directory as a message relay.

Agent identity:
  Set MCP_DISPATCH_AGENT_ID=alpha (or SOPHIA_AGENT_ID for backward compat)
  in the shell before launching Claude Code to pin a stable identity.
  Without it, IDs are auto-claimed from the configured pool in startup order.

Configuration:
  Config file: ~/.config/mcp-dispatch/config.toml
  Override path: MCP_DISPATCH_CONFIG=/path/to/config.toml

  See _DEFAULT_CONFIG for all available settings.

Tools:
  dispatch(message, target, ...)  — send to one agent or all
  peek(...)                       — non-destructive read of pending messages
  ack(message_ids)                — acknowledge and delete processed messages
  heartbeat()                     — check for messages between work phases
  who()                           — list connected agents
  status(message_id, target)      — check delivery receipt for a sent message
"""

from __future__ import annotations

import atexit
import json
import os
import signal
import sys
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from mcp.server.fastmcp import FastMCP

# tomllib is stdlib in 3.11+
try:
    import tomllib
except ImportError:
    import tomli as tomllib  # type: ignore[no-redef]

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

_DEFAULT_CONFIG = {
    "agents": [],  # empty = dynamic registration (any name accepted)
    "dispatch_dir": "~/.config/mcp-dispatch/messages",
    "max_message_bytes": 65536,
    "default_ttl": 0,  # 0 = no default expiry
    "instructions": "",  # empty = use built-in template
}


def _load_config() -> dict:
    """Load configuration from TOML file with env var overrides.

    Resolution order (highest priority first):
    1. Environment variables
    2. Config file
    3. Built-in defaults
    """
    config = dict(_DEFAULT_CONFIG)

    # Find config file
    config_path = os.environ.get(
        "MCP_DISPATCH_CONFIG",
        os.path.expanduser("~/.config/mcp-dispatch/config.toml"),
    )

    if os.path.exists(config_path):
        try:
            with open(config_path, "rb") as f:
                file_config = tomllib.load(f)
            # Flatten: support both top-level keys and [dispatch] section
            if "dispatch" in file_config and isinstance(file_config["dispatch"], dict):
                file_config = {**file_config, **file_config.pop("dispatch")}
            for key in _DEFAULT_CONFIG:
                if key in file_config:
                    config[key] = file_config[key]
            print(f"[dispatch] Config loaded: {config_path}", file=sys.stderr)
        except Exception as e:
            print(f"[dispatch] Config error ({config_path}): {e}", file=sys.stderr)
    else:
        print(f"[dispatch] No config file found, using defaults", file=sys.stderr)

    # Env var overrides
    if os.environ.get("MCP_DISPATCH_DIR"):
        config["dispatch_dir"] = os.environ["MCP_DISPATCH_DIR"]
    elif os.environ.get("DISPATCH_DIR"):
        config["dispatch_dir"] = os.environ["DISPATCH_DIR"]  # legacy fallback

    # Expand ~ in dispatch_dir
    config["dispatch_dir"] = os.path.expanduser(config["dispatch_dir"])

    return config


CONFIG = _load_config()
AGENT_IDS: list[str] = CONFIG["agents"]
DISPATCH_DIR = Path(CONFIG["dispatch_dir"])
MAX_MESSAGE_BYTES = int(CONFIG["max_message_bytes"])
DEFAULT_TTL = int(CONFIG["default_ttl"])
DYNAMIC_MODE = len(AGENT_IDS) == 0  # no roster = accept any agent name


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

    If MCP_DISPATCH_AGENT_ID (or SOPHIA_AGENT_ID) is set, use that directly.
    Otherwise auto-claim the first available slot from the configured pool.
    In dynamic mode, the env var is required.
    """
    presence_dir = DISPATCH_DIR / ".presence"

    # Explicit identity via env var
    explicit = (
        os.environ.get("MCP_DISPATCH_AGENT_ID", "").strip().lower()
        or os.environ.get("SOPHIA_AGENT_ID", "").strip().lower()
    )

    if explicit:
        if AGENT_IDS and explicit not in AGENT_IDS:
            raise ValueError(
                f"Agent ID '{explicit}' is not in the configured roster. "
                f"Valid IDs: {', '.join(AGENT_IDS)}"
            )
        # In dynamic mode, create inbox dir on demand
        (DISPATCH_DIR / explicit).mkdir(exist_ok=True)

        # Check if slot is held by a different live process
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

    if DYNAMIC_MODE:
        raise RuntimeError(
            "No agent ID specified. In dynamic mode (no agents roster), "
            "set MCP_DISPATCH_AGENT_ID in your environment."
        )

    # Auto-claim: first available slot from roster
    for aid in AGENT_IDS:
        pf = presence_dir / f"{aid}.json"
        if pf.exists():
            try:
                data = json.loads(pf.read_text())
                if _pid_alive(data.get("pid", -1)):
                    continue
            except (json.JSONDecodeError, KeyError, OSError):
                pass

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


def _parse_timestamp(ts: str) -> float:
    """Parse ISO 8601 timestamp to epoch seconds."""
    try:
        dt = datetime.strptime(ts, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
        return dt.timestamp()
    except (ValueError, TypeError):
        return 0.0


def _is_expired(msg: dict) -> bool:
    """Check if a message has expired based on TTL."""
    ttl = msg.get("ttl")
    if not ttl or ttl <= 0:
        return False
    if msg.get("must_read", False):
        return False
    sent_at = _parse_timestamp(msg.get("timestamp", ""))
    if sent_at <= 0:
        return False
    return time.time() > sent_at + ttl


def _cleanup_expired(agent_id: str) -> int:
    """Remove expired messages from an agent's inbox. Returns count removed."""
    inbox = DISPATCH_DIR / agent_id
    removed = 0
    for f in inbox.glob("*.json"):
        try:
            msg = json.loads(f.read_text())
            if _is_expired(msg):
                f.unlink()
                removed += 1
        except (json.JSONDecodeError, OSError):
            pass
    return removed


def _read_inbox(agent_id: str, *, state_filter: str | None = None, thread_id: str | None = None) -> list[dict]:
    """Read messages from inbox with optional filtering. Non-destructive."""
    inbox = DISPATCH_DIR / agent_id
    messages: list[dict] = []
    for f in sorted(inbox.glob("*.json")):
        try:
            msg = json.loads(f.read_text())
        except (json.JSONDecodeError, OSError):
            continue

        if state_filter and msg.get("state", "pending") != state_filter:
            continue
        if thread_id and msg.get("thread_id") != thread_id:
            continue

        msg["_file"] = str(f)  # internal: track file path for state updates
        messages.append(msg)

    return messages


def _mark_read(messages: list[dict]) -> None:
    """Transition messages from pending → read. Atomic write in place."""
    for msg in messages:
        if msg.get("state", "pending") != "pending":
            continue
        filepath = msg.pop("_file", None)
        if not filepath:
            continue
        msg["state"] = "read"
        msg["read_at"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        _atomic_write(Path(filepath), {k: v for k, v in msg.items() if k != "_file"})


def _send(
    from_id: str,
    to: str,
    content: str,
    priority: str = "normal",
    thread_id: str | None = None,
    reply_to: str | None = None,
    payload: dict | None = None,
    ttl: int | None = None,
    must_read: bool = False,
) -> dict:
    """Write a message to the target's inbox. Fan-out for 'all'."""
    msg = {
        "id": f"msg-{uuid.uuid4().hex[:8]}",
        "from": from_id,
        "to": to,
        "timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "priority": priority,
        "content": content,
        "payload": payload,
        "thread_id": thread_id,
        "reply_to": reply_to,
        "ttl": ttl if ttl else (DEFAULT_TTL if DEFAULT_TTL > 0 else None),
        "must_read": must_read,
        "state": "pending",
    }

    # Enforce size limit
    msg_bytes = len(json.dumps(msg).encode("utf-8"))
    if msg_bytes > MAX_MESSAGE_BYTES:
        raise ValueError(
            f"Message too large ({msg_bytes} bytes). "
            f"Maximum: {MAX_MESSAGE_BYTES} bytes."
        )

    ts = str(int(time.time() * 1000))

    def _validate_target(target: str) -> None:
        if not DYNAMIC_MODE and target not in AGENT_IDS:
            valid = ", ".join(AGENT_IDS) + ", all"
            raise ValueError(f"Unknown agent '{target}'. Valid targets: {valid}")
        # In dynamic mode, create inbox on demand
        (DISPATCH_DIR / target).mkdir(exist_ok=True)

    if to == "all":
        # Broadcast: fan-out to all known agents (or all with inboxes in dynamic mode)
        targets = [aid for aid in _discover_agents() if aid != from_id]
        for target in targets:
            _atomic_write(DISPATCH_DIR / target / f"{ts}-{from_id}.json", dict(msg))
    else:
        _validate_target(to)
        _atomic_write(DISPATCH_DIR / to / f"{ts}-{from_id}.json", msg)

    return msg


def _discover_agents() -> list[str]:
    """List all known agents. From roster if configured, else from inbox dirs."""
    if AGENT_IDS:
        return list(AGENT_IDS)
    # Dynamic mode: find all directories that aren't .presence
    return [
        d.name
        for d in sorted(DISPATCH_DIR.iterdir())
        if d.is_dir() and not d.name.startswith(".")
    ]


# ---------------------------------------------------------------------------
# Piggyback delivery (non-destructive)
# ---------------------------------------------------------------------------


def _with_pending(result: dict) -> dict:
    """Attach NEW (pending) messages to a tool response, marking them read."""
    _cleanup_expired(AGENT_ID)
    messages = _read_inbox(AGENT_ID, state_filter="pending")
    if messages:
        # Strip internal _file before exposing, but keep for _mark_read
        _mark_read(messages)
        # Clean internal fields for response
        clean = [{k: v for k, v in m.items() if not k.startswith("_")} for m in messages]
        result["_dispatches"] = clean
        result["_dispatch_count"] = len(clean)
    return result


# ---------------------------------------------------------------------------
# Filesystem watcher (stderr alerts for the human operator)
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

# Build instructions from template
_default_instructions = (
    "This is the MCP Dispatch server. You are agent '{agent_id}'. "
    "Use dispatch() to send messages to other agents, "
    "peek() to read incoming messages (non-destructive), "
    "ack() to acknowledge processed messages, "
    "heartbeat() to check for messages between work phases, "
    "who() to see who's online, and status() to check delivery receipts. "
    "Messages from others are also included in every tool response (piggyback delivery). "
    "Available targets: {agent_list}."
)
_instructions_template = CONFIG["instructions"] or _default_instructions
_agent_list = ", ".join(AGENT_IDS) if AGENT_IDS else "(dynamic — any name)"
_instructions = _instructions_template.format(
    agent_id=AGENT_ID,
    agent_list=_agent_list,
)

mcp = FastMCP("dispatch", instructions=_instructions)


# ---------------------------------------------------------------------------
# Tools
# ---------------------------------------------------------------------------


@mcp.tool(
    name="dispatch",
    description=(
        "Send a message to another agent or all agents. "
        "Use priority='urgent' for time-sensitive messages. "
        "Optional: thread_id groups messages into conversations, "
        "reply_to references a specific message, "
        "payload carries structured data (dict), "
        "ttl sets expiry in seconds, "
        "must_read=true prevents auto-expiry. "
        "Returns confirmation plus any pending messages for you."
    ),
)
def dispatch_tool(
    message: str,
    target: str = "all",
    priority: str = "normal",
    thread_id: Optional[str] = None,
    reply_to: Optional[str] = None,
    payload: Optional[dict] = None,
    ttl: Optional[int] = None,
    must_read: bool = False,
) -> dict:
    """Send a message to other agents."""
    sent = _send(
        AGENT_ID, target, message, priority,
        thread_id=thread_id,
        reply_to=reply_to,
        payload=payload,
        ttl=ttl,
        must_read=must_read,
    )
    return _with_pending(
        {
            "sent": True,
            "id": sent["id"],
            "from": AGENT_ID,
            "to": target,
            "priority": priority,
            "thread_id": sent.get("thread_id"),
        }
    )


@mcp.tool(
    name="peek",
    description=(
        "Read incoming messages without deleting them. "
        "By default returns only NEW (unread) messages. "
        "Set include_read=true to see ALL unacknowledged messages. "
        "Filter by thread_id to see a specific conversation. "
        "Use ack() to acknowledge messages when you're done with them."
    ),
)
def peek_tool(
    thread_id: Optional[str] = None,
    include_read: bool = False,
) -> dict:
    """Non-destructive read of inbox messages."""
    _cleanup_expired(AGENT_ID)

    if include_read:
        messages = _read_inbox(AGENT_ID, thread_id=thread_id)
    else:
        messages = _read_inbox(AGENT_ID, state_filter="pending", thread_id=thread_id)

    # Mark pending → read
    _mark_read(messages)

    # Clean internal fields
    clean = [{k: v for k, v in m.items() if not k.startswith("_")} for m in messages]
    return {
        "agent_id": AGENT_ID,
        "messages": clean,
        "count": len(clean),
    }


@mcp.tool(
    name="ack",
    description=(
        "Acknowledge and delete messages by their IDs. "
        "This is the only way to permanently remove messages from your inbox "
        "(besides TTL expiry). Pass a list of message IDs to acknowledge."
    ),
)
def ack_tool(
    message_ids: list[str],
) -> dict:
    """Acknowledge and delete messages."""
    inbox = DISPATCH_DIR / AGENT_ID
    acked = 0
    not_found = []

    for msg_id in message_ids:
        found = False
        for f in inbox.glob("*.json"):
            try:
                msg = json.loads(f.read_text())
                if msg.get("id") == msg_id:
                    f.unlink()
                    acked += 1
                    found = True
                    break
            except (json.JSONDecodeError, OSError):
                continue
        if not found:
            not_found.append(msg_id)

    result = {
        "agent_id": AGENT_ID,
        "acked": acked,
        "not_found": not_found if not_found else None,
    }
    return _with_pending(result)


@mcp.tool(
    name="heartbeat",
    description=(
        "Check for pending messages without doing anything else. "
        "Call this between major work phases to see if other agents "
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
    description="List all currently connected agents and their status.",
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


@mcp.tool(
    name="status",
    description=(
        "Check the delivery state of a message you sent. "
        "Pass the message_id and the target agent's name. "
        "Returns the message state: pending (unread), read, or acked_or_expired."
    ),
)
def status_tool(
    message_id: str,
    target: str,
) -> dict:
    """Check receipt state of a sent message."""
    target_inbox = DISPATCH_DIR / target
    if not target_inbox.is_dir():
        return {"message_id": message_id, "target": target, "state": "unknown_target"}

    for f in target_inbox.glob("*.json"):
        try:
            msg = json.loads(f.read_text())
            if msg.get("id") == message_id:
                return {
                    "message_id": message_id,
                    "target": target,
                    "state": msg.get("state", "pending"),
                    "read_at": msg.get("read_at"),
                }
        except (json.JSONDecodeError, OSError):
            continue

    return {"message_id": message_id, "target": target, "state": "acked_or_expired"}


if __name__ == "__main__":
    mcp.run()
