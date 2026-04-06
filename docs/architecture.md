# Architecture: telegram-cto-orchestrator

## Overview

The CTO orchestrator pattern describes a session that **triages, delegates, reviews, and reports** rather than implementing directly. It acts as the judgment layer above a set of worker peers — receiving requests, decomposing them, dispatching to capable peers, validating results, and surfacing conclusions to the user.

This document covers the core concepts, state model, authority model, transport layer design, and decision flow.

---

## Core Concepts

### Rooms

A room is a durable workspace for a single task or conversation thread. When the orchestrator dispatches work to a peer, it creates a handoff in the context of a room — the room provides the workspace, the handoff is the delegation unit.

Each room lives at `.orchestrator/rooms/<room-id>/` and contains exactly two files:

- **`state.yaml`** — The authoritative state for the room. Tracks goal, status, phase, constraints, and acceptance criteria. This file is the source of truth for room lifecycle; tools and automation read and write it. Room state includes operational memory fields (`request_summary`, `current_summary`, `open_questions`, `blocker_summary`) that hold broad CTO-level context. These fields are authoritative and manually maintained — they are not derived from other state. Room memory fields are updated explicitly via `orchctl room memory`. Each update records changes in the room log and updates `room.updated_at`. There is no automatic memory synchronization in v0 — all updates are manual. Room state stores authoritative broad context only. Handoff delegation summary is derived at render time by scanning handoff files — it is never stored in room state. `lifecycle.current_phase` is a manual field updated explicitly via `orchctl room memory --phase`; handoff transitions do not modify it.
- **`log.md`** — An append-only activity log. Each entry records a timestamp, actor, and action summary. Never edited retroactively. Human-readable summary of what happened and why.

Rooms are cheap to create and should be created liberally — one per distinct task or concern. They are archived, not deleted.

### Programs

A program is a named unit of work that may span multiple rooms. Programs are tracked in `.orchestrator/active_programs.yaml`.

A program has:
- A unique ID and human-readable name
- Status: `active`, `paused`, `completed`, or `blocked`
- Priority: `critical`, `high`, `medium`, or `low`
- A list of associated room IDs (derived convenience — the true room-to-program link lives in `room.state.yaml` via `room.program_id`)
- An optional owner (peer ID)

Programs provide the high-level view. Rooms provide the execution detail.

### Peer Registry

The peer registry (`.orchestrator/peer_registry.yaml`) lists known worker sessions and their capabilities. The orchestrator consults this when deciding where to dispatch work.

Each peer entry records:
- A stable ID and display name
- Type: `worker`, `reviewer`, or `specialist`
- Working directory (`cwd`)
- Capability tags (e.g., `python`, `fastapi`, `security`)
- Current status: `available`, `busy`, or `offline` (informational, not enforced by orchctl)
- Last seen timestamp

The registry is manually maintained. It is static metadata only. Peer discovery and heartbeat are out of scope.

### Handoffs

A handoff is a structured delegation unit — a YAML state object the orchestrator creates when assigning work to a peer. Handoffs are stored as individual YAML files at `.orchestrator/handoffs/<handoff-id>.yaml`.

Each handoff has its own lifecycle and tracks:
- Which room and program the work belongs to
- Who the work is delegated to (`to` field — this is authoritative for room assignment ownership)
- What the peer is expected to do (task description, scope)
- What constraints apply
- What constitutes completion (acceptance criteria)
- Where to report results
- Status: `open`, `claimed`, `completed`, `blocked`
- Timestamps for creation, claim, and completion

**Handoff state transitions:**

```
open → claimed   (orchctl handoff claim <id> --by <peer-id>)
claimed → blocked   (orchctl handoff block <id> --by <peer-id> --reason "...")
claimed → completed   (orchctl handoff complete <id> --by <peer-id> --summary "...")
```

All other transitions are invalid and will be rejected. Completed and blocked handoffs are terminal states (no further transitions).

Only the assigned peer (`handoff.to`) can transition a handoff. Reassignment is not supported in v0.

Handoffs are managed via `orchctl handoff create|list|show|claim|block|complete`.

The `orchctl handoff brief` command generates an on-demand execution brief by reading current handoff and room state. The brief is a derived worker instruction — it is not stored, not authoritative, and does not modify any state. It exists to give workers structured context without requiring them to parse raw YAML.

The `orchctl handoff room-memory` command generates suggested room memory updates from a terminal (blocked or completed) handoff. Like the execution brief, this is a read-only derived view — it does not modify room or handoff state. The orchestrator reviews the suggestion and applies it manually via `orchctl room memory` if appropriate.

Completed handoffs may include structured evidence in the `resolution` section: `files_changed`, `verification`, and `risks`. These fields capture what was done, what was checked, and what remains — providing review context without enforcing a pass/fail gate.

The `orchctl handoff review` command generates a read-only review packet from a completed handoff's evidence and room context. It highlights review signals (missing verification, outstanding risks, undefined criteria) without rendering a pass/fail verdict. Review signals are contract-aware: when task contracts define validation steps, invariants, non-goals, or failure examples, the review packet generates targeted prompts for the reviewer. The system never auto-determines contract satisfaction — it flags what needs manual verification.

Handoffs support structured task contracts via optional fields: `non_goals`, `invariants`, `failure_examples`, and `validation`. These encode what must not happen, what must be preserved, what constitutes failure, and what must be verified — embedding good instruction structure into the system rather than relying on one-off prompt quality. The execution brief surfaces these fields as worker-facing specifications.

When `task.validation` defines a contract, `handoff complete` accepts `--validation-cover` to explicitly map each validation step to its evidence. `handoff review` displays per-step coverage status. `handoff approve` enforces a hard gate: all validation steps must have explicit coverage before approval is allowed. This is a deterministic gate based on explicit mapping, not semantic inference.

Review outcomes (`approved` or `changes_requested`) are recorded in the handoff's `review` section via `orchctl handoff approve` or `orchctl handoff request-changes`. The handoff status remains `completed` — the review outcome is a separate concern. Each handoff can be reviewed once; re-review is not supported in v0.

Review authority is separated from execution authority. Only peers with `type: reviewer` in the peer registry can record review outcomes. The handoff assignee (`handoff.to`) and the peer who completed the handoff (`resolution.completed_by`) cannot review the same handoff — self-review is disallowed. This ensures review is an independent control point, not a rubber-stamp by the executor.

`completed` indicates the worker has submitted results; `approved` indicates those results passed review. Downstream propagation (room memory suggestions) is approval-gated: only approved completions are eligible for room context updates. Review state is derived at render time from `handoff.status` and `review.outcome` — it is never stored as a separate field.

When a review records `changes_requested`, the orchestrator creates a new rework handoff via `orchctl handoff rework` rather than reopening the original. This preserves the completed handoff's history and review record intact. The rework handoff inherits the original task contract, scope, and constraints, and includes the review feedback in its task description. Lineage is recorded via `handoff.rework_of`.

---

## Authority Model

This section documents which file is the source of truth for each concern. When information appears in multiple places, the authoritative source wins.

| Concern | Authoritative Source | Notes |
|---|---|---|
| Room lifecycle (status, phase, goal, constraints, acceptance criteria) | `rooms/<id>/state.yaml` | The single source of truth for a room's current state. Handoff delegation summary is derived at render time by `room show` — it is never stored in room state. |
| Delegation lifecycle (who is doing what, handoff status) | `handoffs/<id>.yaml` | Each handoff is one delegation unit with its own lifecycle. The `to` field is the authoritative record of "who is working on this room." |
| Program metadata (id, name, status, priority, owner) | `active_programs.yaml` | The `rooms` list within a program entry is a **derived convenience** — the true room-to-program link lives in `room.state.yaml` via `room.program_id`. |
| Peer identity and capabilities | `peer_registry.yaml` | Static metadata only. The `status` field is informational, not enforced by orchctl. |
| Activity history | `rooms/<id>/log.md` | Append-only. Never parsed for state by tools. Exists for human review. |

Room-to-handoff lookup is derived by scanning `handoffs/*.yaml` for matching `room_id` values. No reverse-link field is stored in room state. When filtering by room, unparseable handoff files are reported as warnings rather than inline rows, since their room membership cannot be determined.

**Rule:** If there is ever a conflict between a YAML state file and a Markdown file, the YAML file wins. Markdown files are never parsed for state by tools — they exist for human review only.

State transitions are recorded by updating `state.yaml` and appending a log entry to `log.md`. Both steps should happen together.

---

## State Model

```
YAML (authoritative)
  .orchestrator/active_programs.yaml
  .orchestrator/peer_registry.yaml
  .orchestrator/rooms/<id>/state.yaml
  .orchestrator/handoffs/<id>.yaml

Markdown (append-only, human-readable)
  .orchestrator/rooms/<id>/log.md
```

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
   - Create a handoff YAML for the target peer
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
- YAML schemas for programs, peers, rooms, and handoffs
- Append-only log format
- `orchctl` CLI with room, handoff, and log commands
- This architecture document

### Out of Scope for v0
- Telegram transport adapter implementation
- Peer discovery or heartbeat protocol
- MCP server implementation
- Plugin packaging
- Multi-orchestrator coordination
- Any runtime beyond a Claude Code session reading/writing these files

---

## Deferred Decisions

**schema_version**: Deferred until a breaking schema change. Current schemas are simple enough that additive changes are safe with PyYAML's permissive loading.

**Room memory vs. room contract**: Room memory (`request_summary`, `current_summary`, `open_questions`, `blocker_summary`) and room contract (`constraints`, `acceptance_criteria`) are distinct concerns. Memory tracks situational context — the evolving "what is happening" picture the orchestrator maintains as work progresses. Contract defines room-wide boundaries and definition of done — the "what must be true" specification that constrains all work done in this room. Both are authoritative fields in room state, managed via separate commands (`orchctl room memory` and `orchctl room contract`). Memory is updated frequently as context evolves; contract is set at room planning time and changes infrequently. Handoff briefs surface both: room memory provides worker situational context, room contract provides room-wide positive spec that task-level specs must satisfy.

---

## Related Documents

- `docs/worker-lifecycle.md` — execution-mode policy for ephemeral subagents, warm workers, persistent tmux workers, and archive rules
