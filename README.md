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
- `state.yaml` — authoritative state (status, goal, assignments, phase)
- `log.md` — append-only activity log (human-readable, never edited retroactively)

### Programs
Active work items tracked in `.orchestrator/active_programs.yaml`. A program is a named unit of work that spans one or more rooms and has an owner, priority, and status.

### Peer Registry
Known worker sessions listed in `.orchestrator/peer_registry.yaml`. The orchestrator uses this to decide where to dispatch work — which peer has the right capabilities and is available.

### Handoffs
Structured task delegation artifacts stored in `.orchestrator/handoffs/`. A handoff is a document passed from the orchestrator to a peer, specifying what to do, what constraints apply, and what constitutes completion.

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
.orchestrator/
  active_programs.yaml     # All active programs (source of truth)
  peer_registry.yaml       # Known peer sessions and their capabilities
  handoffs/                # Task delegation artifacts
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

## Status

**Skeleton / pre-alpha.** The structure and conventions are defined. Runtime tooling is not yet implemented.

### What Is Not Included Yet

- Telegram transport adapter (bot polling, message routing)
- Plugin packaging or marketplace integration
- Full MCP server implementation
- Automated room creation or program lifecycle tooling
- Multi-peer coordination protocol
- Any CI/CD or deployment configuration

These are out of scope for v0. This skeleton establishes the conventions that those components will build on.

---

## License

MIT. See [LICENSE](LICENSE).
