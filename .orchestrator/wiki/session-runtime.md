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
  - `tmux_target` (exact pane id `%N`, captured on fresh dispatch; required for reuse)
  - `mode`: `ephemeral | warm | persistent`
  - `status`: `offline | idle | busy`
  - `room_id`, `handoff_id` (must reference existing room/handoff when set)
  - `cwd`, `branch`
  - `dirty` (bool)
  - `reuse_count` (non-negative int)
  - `heartbeat_at`, `lease_until`, `last_active_at` (ISO timestamps)
- Commands: `orchctl session list | show | upsert | checkpoint | bootstrap`
- Referential integrity: `session upsert` validates `peer_id`, `room_id`, `handoff_id` against existing authoritative state before writing. `tmux_target` is validated against the `%[0-9]+` format at the CLI boundary.

### Safe-Write Helper

- Location: `storage.safe_write_text(base_dir, target_path, content)` in `lib/storage.py`
- Used by: all four derived artifact writers below.
- Guarantees:
  - `base_dir` itself must not be a symlink (checked both before and after `makedirs` for race defense).
  - Every intermediate directory from `target_parent` up to `base_dir` must not be a symlink (walked by `_check_parent_chain_no_symlinks`).
  - `target_parent` itself must not be a symlink.
  - `target_path`, if already existing, must not be a symlink.
  - `target_parent_real` must stay inside `realpath(base_dir)` (containment check).
  - Write goes to a hidden temp file in the same directory, then `os.replace` for atomic rename.
  - Tmp file is cleaned up on failure.
  - Helper raises `ValueError` or `OSError`. It does NOT call `sys.exit`.
- Design: callers decide command-level failure semantics (warn vs exit vs rollback). The helper is ordinary code.

### Dispatch Artifacts (derived, per handoff)
- Location: `.orchestrator/runtime/dispatches/<handoff-id>.md`
- Writer: `_write_dispatch_artifact` inside `handoff dispatch` (fresh and reuse), via `safe_write_text`.
- Content: rendered handoff brief + session metadata + "non-authoritative" notice
- Not displayed directly any more — superseded by bootstrap display on dispatch.

### Checkpoint Artifacts (derived, append-only)
- Location: `.orchestrator/runtime/checkpoints/<session-id>-<event>-<timestamp>.md`
- Writers: `orchctl session checkpoint`, EXIT trap, `orch_checkpoint` shell helper, via `safe_write_text`.
- Event names: must match `[A-Za-z0-9_-]+`. Invalid events exit 1 with stderr error.
- Session-id component of the filename is also validated against the same slug pattern.
- Content: session + handoff + room summary snapshot at the moment of write.

### Bootstrap Artifacts (derived, one per session, overwritten)
- Location: `.orchestrator/runtime/bootstrap/<session-id>.md`
- Writers: `orchctl session bootstrap`, dispatch auto-read, via `safe_write_text`.
- Content:
  - Session metadata
  - Handoff id/kind/status/review state (or `(none)` fallback)
  - Room lifecycle / memory / discovery (or `(none)` fallback)
  - Dispatch artifact pointer
  - Latest relevant checkpoint pointer + first-30-lines snippet
  - Wiki pointer (`wiki/current-state.md` if present)
- Overwritten each time `session bootstrap` runs (not append-only).
- Internal `room_id` and `handoff_id` read from session state are re-validated with `validate_slug` before being used to open related files; invalid values render as `(none)` fallback.

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
   - **`cannot_allocate`**: no target peer, peer missing from registry, handoff completed/blocked, room blocker present, OR any session parse error (fail-closed).
   - **`wait_for_existing_assignment`**: handoff already bound to a session whose tmux is confirmed live. Dead-tmux bindings are silently skipped.
   - **`reuse_existing_session`**: at least one session passes eligibility (peer match, status=idle, dirty=false, room match or unbound, handoff_id null-or-same, lease valid, AND tmux live). Dead-tmux sessions are ineligible.
   - **`fresh_session`**: default otherwise.
6. Tie-breakers for checkpoint selection in bootstrap: `(mtime, filename)` tuple with `reverse=True`, deterministic.
7. Stale-session warnings: dead-tmux sessions that would otherwise have been reuse candidates surface in `decision["reasons"]`.

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
- Dead tmux reuse candidate → preflight fails at execution time; already skipped at decision time. No state or artifact write. Dispatch returns failure only if dispatch chose it before a race.
- Dead tmux binding during decision → silently skipped for both wait and reuse; surfaced in reasons as warning.
- Blocked room → `cannot_allocate`, no state or tmux change.
- Malformed session YAML → `cannot_allocate`, no state or tmux change. Operator must fix or remove the malformed file.
- Tampered internal reference (e.g. `handoff.room_id = "../evil"`) → controlled error, no path ops, no state mutation.
- Symlinked runtime artifact base dir / parent / target → write refused, ordinary exception raised to caller.

## What Is NOT Yet Implemented (Remaining Polish)

- **Hook install / bootstrap success semantics.** Both are best-effort and their failures become stderr warnings. The dispatch result line still says "dispatched" even if hook injection or bootstrap generation failed.
- **Bootstrap footer wording.** The footer currently lists the latest checkpoint as source of truth alongside YAML state, blurring the derived/authoritative boundary.
- **Stale lock operational cleanup.** Lock files left by crashed dispatches require operator `rm`. No auto-recovery.
- **Provider-specific `/compact` auto-detection.** `orch_compact` is manual. Deliberately deferred.
- **Automatic session cleanup.** Dead tmux detection only runs at decision/reuse preflight. No background sweeper.
- **Session heartbeat auto-update.** The field exists but nothing updates it during session activity.
- **Light mode.** Not yet explored.

## Invariants The Runtime Layer Must Preserve

- Never use tmux scan as a source of truth. Runtime session YAML is the source.
- Never write derived artifacts (dispatch / checkpoint / bootstrap) into authoritative YAML.
- Never display a stale bootstrap when generation fails.
- Never reuse a dirty, busy, wrong-room, dead-tmux, or missing-tmux_target session.
- Never reuse a session without acquiring the per-session O_EXCL lock first.
- Never use a dead-tmux session as evidence of an existing assignment (but legacy sessions with live tmux still block duplicate dispatch).
- Never dispatch into a blocked room.
- Never fall back to `fresh_session` when session parse errors exist — fail closed as `cannot_allocate`.
- Never trust internal YAML references (`handoff.room_id`, `handoff.id`, `session.room_id`, `session.handoff_id`, `session.tmux_session`) without re-validating on the read path before any filename, subprocess, or path operation.
- Never interpolate untrusted strings directly into `tmux send-keys` commands — always `shlex.quote`.
- Never write a runtime artifact via raw `open(...)`; always go through `storage.safe_write_text`.
- Never follow a symlinked runtime base dir, parent chain, or target file.
- Fresh dispatch must clean up its own tmux session if pane target capture or subsequent state write fails.
- Hook injection, bootstrap display, and all tmux send-keys must use the exact pane target (`tmux_target`), never the session name.
- Lock path must refuse symlinked LOCKS_DIR, symlinked lock files, and paths outside the runtime root.
- Checkpoint and bootstrap filename components must be slug-safe.
- Bootstrap failure is non-fatal for dispatch but must never be hidden.
- Helper-level code must not `sys.exit`; it must raise ordinary exceptions so callers can preserve cleanup semantics.
