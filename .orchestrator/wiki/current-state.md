# Current State

This wiki is compiled operating knowledge for the repo. It is not authoritative state.
Authoritative truth remains code, YAML state under `.orchestrator/`, and git history.

## What Exists Now

- `room` is the broad operational container with three concern layers: memory, contract, discovery.
- `room memory` stores situational context: request summary, current summary, open questions, blocker state, next action, manual phase.
- `room contract` stores room-level positive spec: constraints and acceptance criteria.
- `room discovery` stores pre-handoff planning artifacts: problem statement, confirmed facts, assumptions, options, decisions, dependencies, implementation unknowns, chosen direction, readiness notes.
- `room readiness` is a derived read-only recommendation that reads room + related handoff state and suggests next action.
- `handoff` is the authoritative task handoff object with open/claimed/blocked/completed lifecycle.
- `handoff.kind` is an optional type signal: `implementation` (default) or `discovery`.
- `handoff brief` is the worker-facing derived execution brief. It reads room memory, discovery, and contract.
- `completion evidence` includes summary, files changed, verification, risks.
- `validation coverage` is explicit and approval-gated.
- `acceptance criteria coverage` is explicit for both room/task criteria and approval-gated.
- `review` is independent from execution state.
- `review authority` is separate from execution authority: reviewer-type peers only, no self-review by assignee or completer.
- `rework` is a new handoff, not a reopen of the original.
- `structured rework delta` carries `must_address` items from `request-changes` into the rework handoff and brief.
- `peer_registry` is static capability metadata only.
- `runtime session state` is authoritative and lives under `.orchestrator/runtime/sessions/`. Updated via `orchctl session upsert` or as a side-effect of `handoff dispatch`.
- `handoff dispatch-plan` is a read-only recommendation that reads handoff + room + peer registry + runtime session state and emits one of `fresh_session | reuse_existing_session | wait_for_existing_assignment | cannot_allocate` with reasons.
- `handoff dispatch` executes the recommendation: creates a fresh tmux session, or reuses an eligible idle clean session (after live-tmux preflight). Writes a derived dispatch artifact to `.orchestrator/runtime/dispatches/<handoff-id>.md`.
- `session checkpoint` writes a derived snapshot artifact under `.orchestrator/runtime/checkpoints/<session-id>-<event>-<timestamp>.md`. Event names are slug-safe only.
- `session bootstrap` writes a derived startup packet under `.orchestrator/runtime/bootstrap/<session-id>.md`. Combines session/room/handoff state with the latest relevant checkpoint (deterministic selection).
- Dispatched tmux sessions get injected shell hooks (`orch_checkpoint`, `orch_compact`, `orch_bootstrap`, EXIT trap). Hook file is regenerated from `lib/session_hooks.sh.template` on every dispatch.
- Fresh and reuse dispatch both auto-run `session bootstrap` and display the artifact in the tmux session.

## Shipped Milestones

- `05bb6aa` derived worker execution brief
- `39a5b70` room memory workflow primitives
- `3eb17e7` completion evidence capture
- `bc48015` completion review packet
- `71b546a` review outcomes for completed handoffs
- `b2b5857` structured task contracts
- `10f75d6` contract-aware review
- `66e4a38` rework handoff creation
- `face403` validation coverage gate
- `a7c8923` room truth model separation
- `3f23384` approval-gated downstream propagation
- `441fc7d` positive spec authoring path
- `3a69050` review authority enforcement
- `caf9cc7` structured rework deltas
- `2b1b529` acceptance criteria coverage gate
- `30cf4da` room discovery planning artifacts
- `35e92cf` session wiki protocol and operating knowledge layer
- `a850e17` room readiness assessment packet
- `a9d5fde` discovery context in handoff briefs
- `4b27fa4` minimal handoff kind specialization
- `9dce6ad` authoritative runtime session state
- `e878241` conservative handoff dispatch planning
- `cd38b6e` tmux-backed handoff dispatch execution
- `bcb0f78` session checkpoint hooks for dispatched sessions
- `d25b51d` session bootstrap auto-read for dispatched sessions
- `40fcb8b` session memory and runtime references synced
- `1730584` harden dispatch against stale state and unsafe refs
- `2c1e676` harden runtime artifact writes against symlink escapes
- `c2e52f1` harden reuse dispatch locking and revalidation
- `6f03979` require exact tmux pane targets for dispatch reuse

## Current Shape Of The System

- The protocol loop is now end-to-end:
  - spec (room memory + contract + discovery)
  - execute (handoff with contract + dispatch to tmux session)
  - evidence (completion with explicit coverage)
  - review (contract-aware, authority-separated, gated)
  - rework (new handoff with structured delta)
- The runtime layer exists as a separate authoritative concern:
  - static peer registry (capability directory)
  - dynamic session state (mode, status, room, handoff, dirty, lease)
  - derived dispatch artifacts (handoff brief snapshot)
  - derived checkpoint artifacts (session boundary snapshots)
  - derived bootstrap artifacts (next-session startup packets)
- Shell hook layer injects `orch_checkpoint`, `orch_compact`, `orch_bootstrap`, and an EXIT trap into every dispatched session.
- Bootstrap auto-read displays the derived startup packet on dispatch, covering both fresh and reuse paths.
- Dispatch decision is hardened: stale/dead tmux bindings are skipped for wait and reuse decisions; session parse errors fail closed as `cannot_allocate` (no fresh fallback); internal YAML references are re-validated on read before filename or subprocess use; tmux injected shell commands are `shlex.quote`-safe.
- Runtime derived artifact writers route through `storage.safe_write_text` with containment + symlink-refuse + atomic rename.
- Reuse dispatch is hardened with per-session `O_EXCL` lock + CAS revalidation: two parallel dispatches cannot double-claim the same idle session. Lock path safety enforces symlink refusal, runtime-root containment, and payload write failure cleanup.
- Exact tmux pane targeting: fresh dispatch captures pane id (`%N`) immediately after `tmux new-session` and stores it as `tmux_target` in authoritative session state. Reuse dispatch revalidates the target (drift, liveness). Hooks, bootstrap display, and all `send-keys` use the exact pane target, not the session name. Legacy sessions without `tmux_target` are ineligible for reuse but still block duplicate dispatch via `wait_for_existing_assignment`.
- Session `upsert --tmux-target` validates the pane id format at the CLI boundary before writing authoritative state.

## Status

**v1 development phase complete.** The dispatch/runtime/session layer is production-ready for first real-world use. Remaining items are operational polish, not blockers.

Remaining polish:
1. Hook install / bootstrap success semantics — surface in dispatch result instead of silent warnings.
2. Bootstrap footer wording — clarify authority boundary (checkpoint is derived).
3. Stale lock / stale session operational cleanup tooling (currently operator-manual).
4. (Deferred) Provider-specific `/compact` auto-detection.
5. (Deferred) Light mode / automatic session heartbeat / sweeper.

## Next Phase

The next phase is production use on real work repos, not further orchestrator development. Issues discovered during production use will feed back into the hardening backlog.

## Wiki Layer Status

- `.orchestrator/wiki/` is the compiled operating knowledge layer for this repo.
- It exists to survive context compression and runtime/model changes.
- Read this file (`current-state.md`) first at session start; then `decisions.md`, then `deferred.md`.
- `session-runtime.md` holds the detailed reference for the runtime/session/dispatch/bootstrap layer.
- The wiki is support infrastructure, not a source of truth.

## How To Use This File

- Read this first at the start of a new session.
- Update it after shipped milestones or when the current next step changes.
