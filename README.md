# mcp-dispatch

Local inter-agent messaging for AI coding agents via [MCP](https://modelcontextprotocol.io/).

Multiple Claude Code sessions (or any MCP-compatible agents) running on the same machine can send messages to each other through a shared filesystem relay. No server process, no ports, no network — just directories and JSON files with atomic writes.

## Features

- **Non-destructive messaging** — Messages persist until explicitly acknowledged. No more lost messages from crashes or compaction.
- **Threading** — Group messages into conversations with `thread_id` and `reply_to`.
- **Structured payloads** — Attach machine-readable data alongside human-readable messages.
- **TTL & must_read** — Time-sensitive messages auto-expire. Critical messages survive until acknowledged.
- **Delivery receipts** — Check if your message was received and read.
- **Config-driven** — TOML config for agent rosters, directories, and limits. Or go dynamic with no roster.
- **Zero infrastructure** — Filesystem relay survives process crashes. No daemon to manage.

## Quick Start

### 1. Install

Requires Python 3.11+ and [uv](https://docs.astral.sh/uv/).

```bash
git clone https://github.com/sophia-labs/mcp-dispatch.git
cd mcp-dispatch
uv sync
```

For real-time stderr alerts when messages arrive (optional):

```bash
uv sync --extra watch
```

### 2. Configure Claude Code

Add to your `~/.claude.json`:

```json
{
  "mcpServers": {
    "dispatch": {
      "type": "stdio",
      "command": "uv",
      "args": ["run", "--directory", "/path/to/mcp-dispatch", "python", "server.py"],
      "env": {
        "MCP_DISPATCH_AGENT_ID": "alice"
      }
    }
  }
}
```

Each Claude Code window needs a unique `MCP_DISPATCH_AGENT_ID`.

### 3. Send messages

From any Claude Code session:

```
Agent alice: dispatch("Hey bob, I pushed the fix", target="bob")
Agent bob:   peek()  →  sees alice's message
Agent bob:   ack(["msg-abc12345"])  →  message removed
```

## Tools

| Tool | Description |
|------|-------------|
| `dispatch(message, target, ...)` | Send a message to one agent or all |
| `peek(thread_id?, include_read?)` | Read messages without deleting them |
| `ack(message_ids)` | Acknowledge and delete processed messages |
| `heartbeat()` | Check for messages between work phases |
| `who()` | List connected agents |
| `status(message_id, target)` | Check delivery state of a sent message |

### dispatch

```python
dispatch(
    message="Deployed to staging",
    target="all",           # or a specific agent name
    priority="normal",      # "normal" or "urgent"
    thread_id="deploy-123", # optional: group into conversation
    reply_to="msg-abc",     # optional: reference specific message
    payload={"commit": "abc123", "env": "staging"},  # optional: structured data
    ttl=3600,               # optional: expire after 1 hour
    must_read=True,         # optional: survive TTL, require explicit ack
)
```

### peek

```python
peek()                          # new (unread) messages only
peek(include_read=True)         # all unacknowledged messages
peek(thread_id="deploy-123")    # filter by thread
```

### ack

```python
ack(message_ids=["msg-abc", "msg-def"])  # delete specific messages
```

## Configuration

Create `~/.config/mcp-dispatch/config.toml`:

```toml
# Agent roster (omit for dynamic registration — any name accepted)
agents = ["alice", "bob", "carol"]

# Message directory (default: ~/.config/mcp-dispatch/messages)
dispatch_dir = "~/.config/mcp-dispatch/messages"

# Maximum message size in bytes (default: 65536)
max_message_bytes = 65536

# Default TTL in seconds (0 = no expiry)
default_ttl = 0

# Custom MCP instructions template (optional)
# Placeholders: {agent_id}, {agent_list}
# instructions = "You are {agent_id}. Available agents: {agent_list}."
```

### Environment Variables

| Variable | Description |
|----------|-------------|
| `MCP_DISPATCH_AGENT_ID` | Agent identity (required in dynamic mode) |
| `MCP_DISPATCH_CONFIG` | Config file path (default: `~/.config/mcp-dispatch/config.toml`) |
| `MCP_DISPATCH_DIR` | Override dispatch directory from config |

### Dynamic Mode

When no `agents` roster is configured, any agent name is accepted. Inbox directories are created on demand. This is more flexible but less safe (typos create phantom agents).

## How It Works

- Each agent gets an inbox directory (`{dispatch_dir}/{agent_name}/`)
- Messages are JSON files written atomically (tmp + rename)
- Presence is tracked via PID files in `{dispatch_dir}/.presence/`
- Messages have states: `pending` → `read` → acknowledged (deleted)
- Piggyback delivery: pending messages are attached to every tool response
- TTL cleanup runs lazily on read operations
- Optional watchdog prints stderr alerts for the human operator

## Message Format

```json
{
  "id": "msg-a1b2c3d4",
  "from": "alice",
  "to": "bob",
  "timestamp": "2026-02-17T20:30:00Z",
  "priority": "normal",
  "content": "Deployed to staging",
  "payload": {"commit": "abc123"},
  "thread_id": "deploy-123",
  "reply_to": null,
  "ttl": 3600,
  "must_read": false,
  "state": "pending"
}
```

## License

MIT — see [LICENSE](LICENSE).
