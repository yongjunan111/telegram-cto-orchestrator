# Decisions

This file records why major design choices were made.
It is not the source of truth; it is the design rationale layer.

## Stored Broad State vs Derived Delegation Summary

- Room broad state is authoritative and stored.
- Handoff/delegation summary is derived at render time.
- Reason: storing derived delegation placeholders caused stale and misleading room views.
- Consequence: `room show` computes handoff summary from handoff files and surfaces parse errors instead of silently hiding them.

## Completed Is Not Approved

- `handoff.status=completed` means the worker submitted results.
- `review.outcome=approved|changes_requested` is a separate review axis.
- Reason: execution truth and review truth are different facts.
- Consequence: downstream propagation is approval-gated; completion alone is not accepted truth.

## Explicit Coverage Over Semantic Guessing

- Validation coverage is explicit.
- Acceptance criteria coverage is explicit.
- Reason: the system must not pretend it knows semantic sufficiency when it does not.
- Consequence: approval gates only on deterministic explicit mappings, not on model inference.

## Review Authority Is Separate From Execution Authority

- Only reviewer-type peers can record review outcomes.
- Assignee and completer cannot review the same handoff.
- Reason: review must be an independent control point, not a rubber stamp.

## Rework Is A New Handoff

- `changes_requested` does not reopen the original handoff.
- Rework creates a new handoff with lineage via `rework_of`.
- Reason: preserve completion evidence and review history intact.

## Rework Delta Must Be Structured

- `request-changes` requires `must_address` items for new reviews.
- Rework handoffs carry those items in a dedicated `rework` section.
- Reason: free-form review notes alone are too weak to act as the next execution contract.

## Positive Spec Is Separate From Memory

- `room memory` stores evolving context.
- `room contract` stores room-level positive spec.
- `handoff task` stores task-level positive and negative spec.
- Reason: memory answers "what is happening"; contract answers "what must be true".

## Room State Has Three Distinct Concern Layers

- Room state separates memory, contract, and discovery into distinct sections, each with its own command.
- Memory (`room memory`): evolving situational context.
- Contract (`room contract`): normative spec.
- Discovery (`room discovery`): pre-handoff planning artifacts.
- Reason: mixing situational context, normative spec, and planning artifacts into a single section made each concern harder to maintain and surface to workers.

## Static Peer Registry vs Dynamic Runtime State

- `peer_registry.yaml` holds static capability metadata only (id, type, cwd, capabilities).
- `.orchestrator/runtime/sessions/*.yaml` holds dynamic session state (mode, status, room, handoff, dirty, lease, heartbeat).
- Reason: mixing static identity with dynamic status made the registry both stale and unsafe to edit.
- Consequence: `session upsert` is the only way to change runtime state; peer registry is edited separately and rarely.

## Dispatch-Plan And Dispatch Share One Decision Helper

- `handoff dispatch-plan` (read-only recommendation) and `handoff dispatch` (actual execution) both route through `_compute_dispatch_decision()`.
- Reason: if the two paths diverge, the recommendation and execution can disagree, which is the worst possible UX.
- Consequence: any new dispatch rule must be added to the shared helper, not to either caller.

## Fresh Dispatch Is Default; Reuse Is Strictly Gated

- Default outcome is `fresh_session`.
- Reuse requires: peer match, status=idle, dirty=false, room match (or unbound), handoff_id null-or-same, lease valid, AND live tmux preflight at execution time.
- Reason: stale session reuse pollutes context; fresh-per-handoff is conservative and debuggable.
- Consequence: the reuse eligibility rules cannot be loosened without a separate design turn.

## Dispatch Rollback On Partial Failure

- Fresh dispatch: if session state write fails after tmux create, the tmux session is killed to prevent leftover processes.
- Reuse dispatch: tmux existence is checked BEFORE any state write (preflight); failure leaves state untouched.
- Reason: partial dispatches are the worst kind of runtime leak.

## Room Blocker Is A Hard Stop For Dispatch

- If room lifecycle has a blocker_summary or blocked_by, dispatch returns `cannot_allocate` immediately.
- Reason: dispatching into a known-blocked room produces work that cannot be used.
- Consequence: the blocker check lives inside `_compute_dispatch_decision` so both plan and execute paths share it.

## Checkpoint, Dispatch, and Bootstrap Are Three Separate Artifacts

- **Dispatch artifact** (`runtime/dispatches/<handoff-id>.md`): current handoff brief + session metadata at dispatch time.
- **Checkpoint artifact** (`runtime/checkpoints/<session-id>-<event>-<ts>.md`): point-in-time session boundary snapshot.
- **Bootstrap artifact** (`runtime/bootstrap/<session-id>.md`): next-session startup packet combining state + latest checkpoint.
- Reason: merging them into one file loses the semantic distinction between "what was dispatched", "what happened at session boundaries", and "what a new session should read to resume".
- Consequence: each artifact has its own writer, its own lifecycle, and is never treated as authoritative state.

## Bootstrap Never Displays Stale Artifacts

- `_run_bootstrap_and_display` tracks subprocess success explicitly. It will never `cat` a stale bootstrap file when generation fails.
- Reason: displaying a stale file looks like success and silently misleads the operator.
- Consequence: on failure the tmux session shows an explicit failure message; dispatch itself still succeeds.

## Deterministic Checkpoint Selection

- `_find_latest_checkpoint` sorts by `(mtime, filename)` tuple, not mtime alone.
- Reason: mtime ties are common (the filesystem can create two files in the same second) and `os.listdir` order is not guaranteed. A claim of determinism must actually hold in code.
- Consequence: tie-breaking is by lexicographic filename with `reverse=True`.

## Shell Hooks Are Generic, Not Provider-Specific

- Injected hooks define `orch_checkpoint`, `orch_compact`, `orch_bootstrap`, and an EXIT trap — all plain bash.
- Reason: intercepting Claude-specific `/compact` or Codex-specific commands couples the dispatch layer to a single model runtime.
- Consequence: `/compact` auto-detection is deliberately deferred. Users call `orch_compact` manually before compacting.
