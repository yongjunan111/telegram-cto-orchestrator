# telegram-cto-orchestrator

A minimal toolkit skeleton for running a CTO-style orchestrator session over Telegram (or any messaging transport).

---

## What This Is

This repo provides the structural scaffolding for an orchestrator that **triages, delegates, reviews, and reports** — rather than implementing things directly. It is designed to be used as the home directory for a long-running Claude Code session that coordinates work across multiple peer sessions and projects.

The design is transport-agnostic. Telegram is one adapter. The core state and logic have no dependency on it.

---

## Key Concepts

### Rooms
Isolated workspaces for tasks or conversations. Each room lives in `.orchestrator/rooms/<room-id>/` and contains:
- `state.yaml` — authoritative state (status, goal, phase) plus derived assignment views
- `log.md` — append-only activity log (human-readable, never edited retroactively)

### Programs
Active work items tracked in `.orchestrator/active_programs.yaml`. A program is a named unit of work that spans one or more rooms and has an owner, priority, and status. The `rooms` list within a program entry is a derived convenience — the true room-to-program link lives in each room's `state.yaml` via `program_id`.

### Peer Registry
Known worker sessions listed in `.orchestrator/peer_registry.yaml`. The orchestrator consults this as a static capability directory when selecting peers for dispatch. Availability status is informational only, not enforced at runtime.

### Handoffs
Structured delegation units stored as individual YAML files at `.orchestrator/handoffs/<handoff-id>.yaml`. A handoff is a YAML state object the orchestrator creates when assigning work to a peer. Each handoff tracks what to do, what constraints apply, who the work is delegated to, and what constitutes completion. The handoff's `to` field is the authoritative record of room assignment ownership.

---

## Design Principles

| Principle | Rationale |
|---|---|
| Policy and state are separated | Policy lives in prompts/config; state lives in YAML. Mixing them makes both harder to change. |
| YAML is authoritative state | Human-readable, diff-friendly, easy to parse. Markdown is derived from it, not the other way around. |
| Markdown is for logs and views only | Append-only logs and human summaries. Never the source of truth for program or room status. |
| Transport-agnostic core | Telegram, Slack, or CLI — the orchestrator's decision logic never imports a transport adapter. |
| Skeleton quality over feature breadth | A clean, well-documented skeleton is more useful than a half-implemented feature set. |

---

## Directory Structure

```
orchctl                    # CLI entry point (Python)
pyproject.toml             # Project metadata and dependencies
.orchestrator/
  active_programs.yaml     # Active programs (rooms list is derived; room.program_id is authoritative)
  peer_registry.yaml       # Known peer sessions and their capabilities
  handoffs/                # YAML state objects for task delegation
  rooms/
    TEMPLATE/              # Copy this to create a new room
      state.yaml           # Room state (authoritative)
      log.md               # Append-only activity log
docs/
  architecture.md          # Architecture and design decisions
  worker-lifecycle.md      # Execution-mode and archive policy
README.md
LICENSE
```

---

## Getting Started

Requires Python 3.10+ and [uv](https://docs.astral.sh/uv/).

```bash
git clone <repo-url> && cd telegram-cto-orchestrator

# Install dependencies (if uv is not on PATH, prefix with its full path)
uv sync                    # or: ~/.local/bin/uv sync
```

## Usage

Run `orchctl` through the project venv:

```bash
# Create a room
.venv/bin/python orchctl room create api-refactor \
  --name "API Refactor" --goal "Migrate REST endpoints to v2"

# List rooms
.venv/bin/python orchctl room list

# Show room details
.venv/bin/python orchctl room show api-refactor

# Append to room log
.venv/bin/python orchctl log append api-refactor \
  --actor orchestrator --message "Dispatched to backend-worker"

# Create a handoff
.venv/bin/python orchctl handoff create fix-auth-bug \
  --room api-refactor --to backend-worker \
  --task "Fix the auth middleware token validation" \
  --priority high --scope "auth module only"

# List handoffs
.venv/bin/python orchctl handoff list

# List handoffs filtered by room
.venv/bin/python orchctl handoff list --room api-refactor

# Show handoff details
.venv/bin/python orchctl handoff show fix-auth-bug

# Generate a worker execution brief (on-demand, not stored)
.venv/bin/python orchctl handoff brief fix-auth-bug
```

> **Tip:** If your shell activates the venv (`source .venv/bin/activate`), you can use `./orchctl` directly.

### Supported Commands

| Command | Description |
|---|---|
| `room create <id> --name ... --goal ...` | Create a new room from TEMPLATE |
| `room list` | List all rooms with status and phase |
| `room show <id>` | Display full room state |
| `log append <id> --actor ... --message ...` | Append entry to room log |
| `handoff create <id> --room ... --to ... --task ...` | Create a task handoff to a peer |
| `handoff list [--room <room-id>]` | List handoffs, optionally filtered by room |
| `handoff show <id>` | Display full handoff details |
| `handoff claim <id> --by <peer-id>` | Claim an open handoff (open → claimed) |
| `handoff block <id> --by <peer-id> --reason "..."` | Block a claimed handoff (claimed → blocked) |
| `handoff complete <id> --by <peer-id> --summary "..."` | Complete a claimed handoff (claimed → completed) |
| `handoff brief <id>` | Generate a derived execution brief for workers |

---

## Status

**MVP / pre-alpha.** Core room management CLI (`orchctl`) is functional. State conventions and directory structure are established.

### What Is Not Included Yet

- Telegram transport adapter (bot polling, message routing)
- Plugin packaging or marketplace integration
- Full MCP server implementation
- Program and peer management commands
- Multi-peer coordination protocol
- Any CI/CD or deployment configuration

These are out of scope for v0.

---

## License

MIT. See [LICENSE](LICENSE).
