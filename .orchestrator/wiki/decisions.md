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

## Stale/Dead Tmux Bindings Are Skipped, Not Trusted

- Dispatch decision no longer uses a session's `handoff_id` binding as evidence of an active assignment if the session's tmux is dead or missing.
- Reason: after a reboot, tmux crash, or shell exit, session YAML can lie — the runtime layer must be skeptical of stored bindings unless the tmux is actually live.
- Consequence: `_compute_dispatch_decision` filters out dead-tmux sessions before both the wait-for-existing-assignment check and reuse eligibility. Dead bindings surface as warnings in the decision reasons.

## Parse Errors Fail Closed, Not Open

- If any session YAML under `runtime/sessions/` fails to parse, dispatch returns `cannot_allocate`, not `fresh_session`.
- Reason: parse errors mean the runtime state is untrustworthy. Silently defaulting to fresh allocation risks double-dispatching live work, masking corruption, and undermining the conservative principle that drives the rest of the dispatch design.
- Consequence: operator sees the malformed file list and must fix or remove it before dispatch resumes. No silent recovery.

## Internal YAML References Are Re-validated On Read

- `_load_handoff_with_room` validates `handoff.room_id` with `validate_slug` before constructing any path.
- `_execute_fresh_dispatch` and `_execute_reuse_dispatch` both validate `handoff.id` with `is_slug_safe` before any tmux creation or state mutation.
- `_execute_reuse_dispatch` also re-validates the chosen session's `room_id`, `handoff_id`, and `tmux_session` before touching state.
- `cmd_session_bootstrap` re-validates session state's `room_id` and `handoff_id` before opening related files; invalid values render as `(none)` fallback.
- Reason: even slug-validated CLI input cannot protect against tampered or corrupted YAML written between invocations. Read paths must not trust stored references.
- Consequence: a tampered internal reference produces a controlled error, not a path-traversal or injection.

## Tmux Shell Commands Are `shlex.quote`-Safe

- `_inject_session_hooks` and `_run_bootstrap_and_display` use `shlex.quote` for every interpolated value that goes to `tmux send-keys`.
- Reason: single-quote f-strings do not escape inner single quotes; a tampered `handoff_id` or `room_id` could break out of the quote and inject shell commands.
- Consequence: path, env-var, and file-path interpolation is quote-safe regardless of upstream validation state. Defense in depth.

## Derived Artifact Writers Use `safe_write_text`

- Bootstrap, dispatch artifact, checkpoint, and session hook-file writers all route through `storage.safe_write_text(base_dir, target_path, content)`.
- The helper enforces: base-dir symlink refusal (including post-`makedirs` race re-check), intermediate parent-chain symlink refusal via `_check_parent_chain_no_symlinks`, target-file symlink refusal, containment under realpath(base_dir), atomic temp-file + `os.replace` rename, tmp cleanup on failure.
- Reason: derived artifacts are not authoritative but they are still file writes under the operator's repo. A pre-existing symlink at a runtime artifact path could silently clobber files outside the runtime tree. The helper makes that class of attack impossible without needing to trust individual writers.
- Consequence: the four writers no longer use raw `open(...)`. Helpers raise ordinary exceptions (never `sys.exit`); command-level semantics live in callers.

## Reuse Race Closed With Lock + CAS Revalidation

- Reuse dispatch acquires a per-session `O_EXCL` lock file (`.orchestrator/runtime/locks/session-<id>.lock`) BEFORE revalidation or state mutation.
- Under the lock, CAS-style revalidation re-reads authoritative session state from disk and re-checks every eligibility condition against the decision snapshot's expectations.
- Reason: without the lock, two parallel `orchctl handoff dispatch` could both pass revalidation and both write `status=busy`, double-claiming the same idle session. Atomic YAML rename prevents corruption but not double reuse. The lock serializes the critical section.
- Lock path safety: LOCKS_DIR symlink refusal, runtime-root containment, lock_path symlink refusal, payload write failure cleanup.
- Stale lock recovery is operator-manual: if a dispatch crashes with the lock held, the operator must `rm` the lock file. No auto-recovery — matches the project's "malformed state = operator intervention" pattern.

## Exact Pane Targeting Is Mandatory For Reuse

- Fresh dispatch captures the pane id (`%N`) immediately after `tmux new-session` and stores it as `tmux_target` in authoritative session state.
- Reuse dispatch requires `tmux_target` to be present, safe, matching the snapshot, and alive. Missing or unsafe target → fail-closed as ineligible.
- All `send-keys` (hooks, bootstrap, command injection) use the exact pane target, not the session name.
- Reason: if the user splits panes or creates windows in a reused tmux session, session-name-level `send-keys` hits whichever pane happens to be selected — silently mis-routing hooks and commands to the wrong shell.
- Consequence: capture failure in fresh dispatch rolls back the tmux session (kill + no state/artifact/hook side effects). Legacy sessions without `tmux_target` are never reusable.

## Legacy Sessions Block Duplicate Dispatch But Cannot Be Reused

- `_evaluate_session_eligibility` requires `tmux_target` → legacy sessions are ineligible for reuse.
- `_compute_dispatch_decision` wait-for-existing-assignment loops still check `_tmux_session_exists(tmux_name)` → a legacy session bound to the same handoff with live tmux still blocks duplicate dispatch.
- Reason: if legacy sessions were silently treated as stale, removing them from duplicate detection would open the door to dispatching the same handoff twice. Reuse eligibility is strict; duplicate blocking is conservative.

## Helper `sys.exit` Is A Caller Hazard

- Helpers invoked from inside dispatch execution (`_write_dispatch_artifact`, `safe_write_text`, etc.) must not call `sys.exit`.
- Reason: if a helper exits mid-dispatch, the process dies before cleanup — tmux session stays live, session YAML stays in the wrong state, rollback never runs.
- Consequence: helpers raise exceptions; callers decide whether to fail the command, warn, or roll back. Only top-level command functions are allowed to `sys.exit`.

## Wiki Suggestions Are Suggest-Only

- `wiki-suggest` extracts structured hints from completed handoff cycles but never writes to wiki files.
- The orchestrator (CTO session) reads suggestions and decides what to accumulate in the wiki.
- Reason: deterministic code cannot produce the quality of synthesis that an LLM curator can. Auto-generated text quickly degrades wiki quality into "auto-generated dump."
- Consequence: wiki files remain hand-curated by the orchestrator session; `wiki-suggest` only surfaces candidates.

## Fingerprint Storage For Accurate Dedupe

- Wiki-suggest stores normalized hint fingerprints in handoff state (`wiki_suggest.generated_hints`) after generating suggestions.
- Dedupe reads these stored fingerprints from prior handoffs rather than re-generating hints from current room state.
- Reason: re-generating from current room state incorrectly projects current decisions/discovery onto past cycles, causing new hints to be false-positively deduped. Stored fingerprints capture what was actually suggested at cycle time.
- Consequence: legacy handoffs without stored fingerprints get no dedupe applied (safe default).

## Manual Wiki-Suggest Is Read-Only

- `orchctl handoff wiki-suggest <id>` computes and displays suggestions but does not write fingerprints to handoff state.
- Only the auto hook (`_try_wiki_suggest_auto`) writes fingerprints, and only at approve/rework time.
- Reason: manual re-runs use current room state, which may have changed since the original cycle. Writing fingerprints at re-run time would overwrite the original cycle-time record, corrupting future dedupe.
- Consequence: fingerprint ownership is split — auto hook = write, manual command = read-only. This is enforced by code structure, not by a runtime check.

## Rework Lineage Walks The Actual Chain

- When `rework_of` is set, `detect_continuity` walks the `rework_of` chain backwards to collect ancestors, rather than scanning all room handoffs.
- Reason: scanning the full room includes unrelated handoffs, inflating cycle count and producing false pattern suggestions.
- Consequence: `prior_handoffs` in rework_lineage mode contains only actual chain ancestors (oldest first). Broken chains fail-soft with a partial result; no room-wide fallback.
