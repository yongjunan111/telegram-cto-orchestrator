# Architecture: telegram-cto-orchestrator

## Overview

The CTO orchestrator pattern describes a session that **triages, delegates, reviews, and reports** rather than implementing directly. It acts as the judgment layer above a set of worker peers — receiving requests, decomposing them, dispatching to capable peers, validating results, and surfacing conclusions to the user.

This document covers the core concepts, state model, transport layer design, and decision flow for v0.

---

## Core Concepts

### Rooms

A room is an isolated workspace for a single task or conversation thread. Rooms are the unit of delegation: when the orchestrator dispatches work to a peer, it does so in the context of a room.

Each room lives at `.orchestrator/rooms/<room-id>/` and contains exactly two files:

- **`state.yaml`** — The authoritative state for the room. Tracks goal, status, phase, assignments, constraints, and acceptance criteria. This file is the source of truth. Tools and automation read and write this file.
- **`log.md`** — An append-only activity log. Each entry records a timestamp, actor, and action summary. Never edited retroactively. Human-readable summary of what happened and why.

Rooms are cheap to create and should be created liberally — one per distinct task or concern. They are archived, not deleted.

### Programs

A program is a named unit of work that may span multiple rooms. Programs are tracked in `.orchestrator/active_programs.yaml`.

A program has:
- A unique ID and human-readable name
- Status: `active`, `paused`, `completed`, or `blocked`
- Priority: `critical`, `high`, `medium`, or `low`
- A list of associated room IDs
- An optional owner (peer ID)

Programs provide the high-level view. Rooms provide the execution detail.

### Peer Registry

The peer registry (`.orchestrator/peer_registry.yaml`) lists known worker sessions and their capabilities. The orchestrator consults this when deciding where to dispatch work.

Each peer entry records:
- A stable ID and display name
- Type: `worker`, `reviewer`, or `specialist`
- Working directory (`cwd`)
- Capability tags (e.g., `python`, `fastapi`, `security`)
- Current status: `available`, `busy`, or `offline`
- Last seen timestamp

The registry is manually maintained in v0. Peer discovery and heartbeat are out of scope.

### Handoffs

A handoff is a structured delegation artifact — a document the orchestrator creates when assigning work to a peer. Handoffs live in `.orchestrator/handoffs/`.

A handoff specifies:
- Which room and program the work belongs to
- What the peer is expected to do
- What constraints apply (scope, tools, time)
- What constitutes completion (acceptance criteria)
- Where to report results

In v0, handoffs are Markdown files created manually or by the orchestrator session. A structured schema and tooling for handoffs is a v1 concern.

---

## State Model

```
YAML (authoritative)
  └── .orchestrator/active_programs.yaml
  └── .orchestrator/peer_registry.yaml
  └── .orchestrator/rooms/<id>/state.yaml

Markdown (derived / append-only)
  └── .orchestrator/rooms/<id>/log.md
  └── .orchestrator/handoffs/<id>.md
```

**Rule:** If there is ever a conflict between a YAML file and a Markdown file, the YAML file wins. Markdown files are never parsed for state by tools — they exist for human review only.

State transitions are recorded by updating `state.yaml` and appending a log entry to `log.md`. Both steps should happen together.

---

## Transport Layer

The orchestrator's core logic (triage, dispatch, review, report) has no dependency on any messaging transport. Telegram, Slack, a CLI, or any other channel is an **optional adapter** that sits outside the core.

```
[Transport Adapter]         [Core Orchestrator Logic]
  Telegram bot        --->    Triage
  Slack bot           --->    Decide
  CLI input           --->    Dispatch
                      --->    Review
                      <---    Report
```

The adapter's only responsibilities are:
1. Receive a message from the user
2. Forward it to the orchestrator session
3. Relay the orchestrator's response back to the user

The adapter never makes decisions. It never reads room state or program state. It is a dumb pipe.

In v0, the Telegram adapter is not implemented. The orchestrator runs as a Claude Code session and the user interacts via the Telegram channel plugin or directly via the terminal.

---

## Decision Flow

Every request the orchestrator receives passes through this flow:

```
1. Triage
   - Identify: which project/domain does this belong to?
   - Classify: what kind of request is this?
   - Assess: urgency, risk, reversibility
   - Determine: answer directly, investigate first, or delegate?

2. Decide
   - If answerable directly: respond
   - If investigation needed: identify what evidence is required and how to get it
   - If delegation needed: identify the right peer and create a room + handoff

3. Dispatch
   - Create or update a room in state.yaml
   - Write a handoff document
   - Send the handoff to the target peer
   - Log the dispatch in log.md

4. Review
   - Receive peer's result
   - Distinguish: is this a claim or evidence?
   - If claim: request supporting evidence or verify independently
   - If evidence: evaluate against acceptance criteria

5. Report
   - Summarize conclusion, rationale, remaining risk, and next action
   - Keep it short: the user needs a decision, not a transcript
   - If uncertain: say so and specify what would resolve the uncertainty

6. Archive
   - Mark room as completed or archived in state.yaml
   - Append final summary to log.md
   - Update program status if all associated rooms are done
```

---

## Scope: v0

### In Scope
- Directory structure and file conventions
- YAML schemas for programs, peers, and rooms
- Append-only log format
- Handoff document convention
- This architecture document

### Out of Scope for v0
- Telegram transport adapter implementation
- Automated room creation tooling
- Peer discovery or heartbeat protocol
- MCP server implementation
- Plugin packaging
- Multi-orchestrator coordination
- Any runtime beyond a Claude Code session reading/writing these files

---

## Related Documents

- `docs/worker-lifecycle.md` — execution-mode policy for ephemeral subagents, warm workers, persistent tmux workers, and archive rules
