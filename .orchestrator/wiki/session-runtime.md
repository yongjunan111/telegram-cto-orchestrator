# Session Runtime Reference

This file is the detailed reference for the runtime / session / dispatch / bootstrap layer.
It is not authoritative state. Authoritative state lives in `.orchestrator/runtime/sessions/*.yaml`.

## Layer Overview

### Static Peer Registry
- Location: `.orchestrator/peer_registry.yaml`
- Content: peer identity and capability metadata only (id, name, type, cwd, capabilities, last_seen).
- Never mixed with runtime/session status.

### Runtime Session State (authoritative, dynamic)
- Location: `.orchestrator/runtime/sessions/<session-id>.yaml`
- Writers: `orchctl session upsert`, `orchctl handoff dispatch` (side-effect)
- Schema:
  - `id` (slug)
  - `peer_id` (must match a registry peer)
  - `tmux_session` (deterministic name from peer + handoff)
  - `mode`: `ephemeral | warm | persistent`
  - `status`: `offline | idle | busy`
  - `room_id`, `handoff_id` (must reference existing room/handoff when set)
  - `cwd`, `branch`
  - `dirty` (bool)
  - `reuse_count` (non-negative int)
  - `heartbeat_at`, `lease_until`, `last_active_at` (ISO timestamps)
- Commands: `orchctl session list | show | upsert | checkpoint | bootstrap`
- Referential integrity: `session upsert` validates `peer_id`, `room_id`, `handoff_id` against existing authoritative state before writing.

### Dispatch Artifacts (derived, per handoff)
- Location: `.orchestrator/runtime/dispatches/<handoff-id>.md`
- Writer: `_write_dispatch_artifact` inside `handoff dispatch` (fresh and reuse)
- Content: rendered handoff brief + session metadata + "non-authoritative" notice
- Not displayed directly any more — superseded by bootstrap display on dispatch.

### Checkpoint Artifacts (derived, append-only)
- Location: `.orchestrator/runtime/checkpoints/<session-id>-<event>-<timestamp>.md`
- Writers: `orchctl session checkpoint`, EXIT trap, `orch_checkpoint` shell helper
- Event names: must match `[A-Za-z0-9_-]+`. Invalid events exit 1 with stderr error.
- Defense-in-depth: filename is also checked with `realpath` containment before write.
- Content: session + handoff + room summary snapshot at the moment of write.

### Bootstrap Artifacts (derived, one per session, overwritten)
- Location: `.orchestrator/runtime/bootstrap/<session-id>.md`
- Writers: `orchctl session bootstrap`, dispatch auto-read
- Content:
  - Session metadata
  - Handoff id/kind/status/review state (or `(none)` fallback)
  - Room lifecycle / memory / discovery (or `(none)` fallback)
  - Dispatch artifact pointer
  - Latest relevant checkpoint pointer + first-30-lines snippet
  - Wiki pointer (`wiki/current-state.md` if present)
- Overwritten each time `session bootstrap` runs (not append-only).

### Shell Hooks
- Template: `lib/session_hooks.sh.template`
- Installed copy: `.orchestrator/runtime/hooks/session_hooks.sh` (regenerated on every dispatch)
- Functions:
  - `orch_checkpoint [note]` → `orchctl session checkpoint "$ORCH_SESSION_ID" --event manual-checkpoint --note ...`
  - `orch_compact` → saves pre-compact checkpoint, prints reminder to run manual compact
  - `orch_bootstrap` → regenerates bootstrap artifact
- EXIT trap: idempotent via `ORCH_EXIT_TRAP_SET` guard; runs `session checkpoint --event shell-exit` on shell exit.
- Env vars injected at dispatch time: `ORCH_SESSION_ID`, `ORCH_HANDOFF_ID`, `ORCH_ROOM_ID`, `ORCHCTL_PYTHON`, `ORCHCTL_SCRIPT`

## Dispatch Decision Flow

1. `cmd_handoff_dispatch_plan` (read-only) or `cmd_handoff_dispatch` (executes)
2. Both call the shared `_compute_dispatch_decision()` helper.
3. Helper inputs: handoff, peer registry entry, session list for that peer, session parse errors, room blocker state, handoff status, review state, kind.
4. Helper output: `{outcome, reasons, chosen_session}` dict.
5. Outcomes:
   - **`cannot_allocate`**: no target peer, peer missing from registry, handoff completed/blocked, room blocker present
   - **`wait_for_existing_assignment`**: handoff already bound to a busy session
   - **`reuse_existing_session`**: at least one session passes eligibility (peer match, status=idle, dirty=false, room match or unbound, handoff_id null-or-same, lease valid)
   - **`fresh_session`**: default otherwise; parse errors force conservative fresh
6. Tie-breakers for checkpoint selection in bootstrap: `(mtime, filename)` tuple with `reverse=True`, deterministic.

## Dispatch Execution Flow

### Fresh session
1. Compute dispatch decision → `fresh_session`
2. Determine cwd from peer registry (fallback to current dir)
3. Check tmux name collision; check session id collision
4. `tmux new-session -d -s <tmux-name> -c <cwd>`
5. Write session state (with `status=busy`, `reuse_count=0`, lease, etc.)
   - If write fails → `tmux kill-session` rollback
6. Write dispatch artifact
7. Inject shell hooks (env vars + source template)
8. Run bootstrap subprocess + display artifact in tmux (if success)

### Reuse session
1. Compute dispatch decision → `reuse_existing_session` with a chosen session
2. **Preflight**: `_tmux_session_exists(tmux_name)` must return True. Empty or dead tmux → fail, no state change.
3. Read fresh session state, update (`status=busy`, `reuse_count++`, `handoff_id`, `lease`, timestamps)
4. Write dispatch artifact
5. Re-inject shell hooks (idempotent via ORCH_EXIT_TRAP_SET)
6. Run bootstrap subprocess + display artifact in tmux (if success)

### Failure modes
- Bootstrap subprocess failure → warning on stderr, tmux shows "Bootstrap artifact not available (generation failed)", dispatch returns success.
- Dead tmux reuse candidate → preflight fails, no state or artifact write, dispatch returns failure.
- Blocked room → `cannot_allocate`, no state or tmux change.
- Malformed session YAML → parse error surfaced, reuse is conservatively forced into fresh_session.

## What Is NOT Yet Implemented

- **Provider-specific `/compact` auto-detection.** `orch_compact` is manual. Intercepting Claude-specific or Codex-specific commands would couple the hook layer to a runtime, deliberately deferred.
- **Automatic session cleanup.** Dead tmux detection only happens at reuse preflight. There is no background sweeper.
- **Session heartbeat auto-update.** The field exists but nothing updates it during session activity.
- **Light mode.** Not yet explored.

## Invariants The Runtime Layer Must Preserve

- Never use tmux scan as a source of truth. Runtime session YAML is the source.
- Never write derived artifacts (dispatch / checkpoint / bootstrap) into authoritative YAML.
- Never display a stale bootstrap when generation fails.
- Never reuse a dirty, busy, wrong-room, or dead-tmux session.
- Never dispatch into a blocked room.
- Fresh dispatch must clean up its own tmux session if subsequent state write fails.
- Checkpoint and bootstrap filename components must be slug-safe.
- Bootstrap failure is non-fatal for dispatch but must never be hidden.
