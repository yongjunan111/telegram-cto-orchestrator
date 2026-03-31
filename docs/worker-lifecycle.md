# Worker Lifecycle

## Purpose

The orchestrator should not treat every delegated task the same way. Some tasks are short-lived and should be handled by an ephemeral subagent. Others benefit from a persistent tmux-backed worker with deeper project context. This document defines the lifecycle rules for worker execution so the orchestrator can make that decision consistently.

---

## Execution Modes

### 1. Ephemeral subagent

Use an ephemeral subagent when the task is short, self-contained, and does not need to preserve deep context after completion.

Typical examples:
- one-shot scaffolding
- narrow research or fact gathering
- a single documentation task
- quick inspection of a repo or file tree

Default policy:
- create the subagent
- delegate one bounded task
- collect the result
- archive only a short summary, not the full working context

### 2. Warm worker

A warm worker is a task context that is likely to be reused soon but does not yet deserve a dedicated tmux session. This is a logical state, not necessarily a visible terminal session.

Promote work into a warm state when:
- the same room is reused 2 to 3 times, or
- follow-up work is likely within 30 to 60 minutes, or
- the task needs a little continuity but not a full long-running execution environment

Warm workers should still be cheap to discard.

### 3. Persistent tmux worker

Use a persistent tmux-backed worker when the task needs deep project context, repeated back-and-forth, or a long-running execution environment.

Typical signals:
- project-specific CLAUDE.md and memory are important
- implementation, testing, and review will span multiple turns
- the task needs a stable cwd, branch, or worktree
- the worker will likely be revisited after the current turn

A persistent worker should be represented in the peer registry and tied to one or more rooms.

---

## Promotion Rules

Do not promote workers based on reuse count alone. Promotion should consider three signals together:
- reuse count
- idle TTL
- room status

Recommended decision rule:
- start with an ephemeral subagent by default
- if the task is reused 2 to 3 times or likely to resume soon, treat it as warm
- if the task needs deep project context or stable runtime state, promote it to a persistent tmux worker

This avoids over-allocating terminal sessions while still preserving context where it matters.

---

## Archive Rules

Archive a worker context when all of the following are true:
- idle TTL has expired
- there is no open handoff requiring active ownership
- the room is completed, blocked, or explicitly archived

Recommended TTLs:
- ephemeral subagent: archive immediately after result collection
- warm worker: archive after about 45 minutes of inactivity
- persistent tmux worker: archive after 2 to 12 hours of inactivity, depending on the task

Archive should preserve:
- room ID
- program ID
- cwd
- branch or worktree
- last active time
- summary of what was done
- unresolved questions
- restart instructions

Markdown can hold the human-readable summary, but authoritative worker metadata should live in structured state.

---

## Coordination Rules

### CTO session responsibilities
- keep broad situational awareness
- decide whether a task should stay ephemeral or become persistent
- hand off work to the right worker
- collect results and report to the user

### Worker responsibilities
- own deep context for the assigned room or project
- produce evidence, not only claims
- update structured state and append concise logs
- report blockers early

### Communication style
- user-facing reports may remain in Korean
- peer-to-peer communication should optimize for compactness and precision
- English or a compact protocol format is preferred between sessions

---

## Future State Model

The following fields should eventually exist in authoritative worker state such as `peer_registry.yaml` or per-worker records:
- `id`
- `room_id`
- `program_id`
- `mode`
- `status`
- `cwd`
- `branch`
- `worktree`
- `reuse_count`
- `heartbeat_at`
- `lease_until`
- `last_active_at`
- `archive_summary`

This document defines the lifecycle policy. The exact schema can evolve in later versions of the toolkit.
