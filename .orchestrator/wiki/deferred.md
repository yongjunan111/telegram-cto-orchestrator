# Deferred

This file records decisions to intentionally postpone work.

## Still Advisory / Manual

- `constraints` are still advisory/manual; they are shown in brief/review but not hard-gated.
- Semantic sufficiency of validation or acceptance evidence is still manual review, not auto-verdict.

## Not Yet Built

- **Provider-specific `/compact` auto-detection.** `orch_compact` saves a pre-compact checkpoint manually. Intercepting `/compact` in Claude Code or `/compact` in Codex would tie the hook layer to a specific runtime and is deliberately deferred.
- **Light mode.** A lower-friction mode for quick tasks has not been explored yet. The current protocol is deliberately heavy.
- **Automatic session cleanup.** Dead tmux detection only runs at reuse preflight. There is no background sweeper that reconciles `runtime/sessions/*.yaml` against actual tmux state.
- **Session heartbeat auto-update.** `heartbeat_at` and `last_active_at` fields exist in session schema but nothing automatically updates them during session activity.
- **Re-review / review overwrite.** A reviewed handoff cannot be re-reviewed in v0.
- **Program CRUD / lineage.** Program layer is skeletal.
- **Peer discovery / introspection.** Peer registry is manually maintained.
- **Telegram adapter / bridge that binds Telegram to role-based peers** instead of a specific model runtime.
- **Provenance** for why room memory / room contract changed beyond the room log.
- **Scoring engine** for review signal weighting.

## Kept Conservative On Purpose

- No automatic reopening of completed handoffs.
- No review overwrite or re-review history yet.
- No scoring engine.
- No constraint auto-verdict.
- No hidden override for review authority.
- No aggressive session reuse — fresh-per-handoff is default.
- No provider-specific compact interception — hooks stay generic shell.
- No tmux scan as a source of truth — runtime/session YAML is authoritative.

## Legacy Compatibility We Keep Supporting

- Legacy `changes_requested` without `must_address` still reworks with a fallback notice.
- Older rooms with removed/derived fields are still read conservatively where possible.
- Handoffs without `kind` are treated as `implementation`.

## Remaining Operational Polish (Not Blockers)

- **Hook install / bootstrap success semantics.** Both are best-effort warnings. Surfacing them in dispatch result would improve operator visibility.
- **Bootstrap footer wording.** Footer currently lists checkpoint alongside authoritative state, blurring the derived/authoritative boundary.
- **Stale lock / stale session operational tooling.** Lock files left by crashed dispatches and dead-tmux session YAML both require operator `rm`. No sweeper or auto-recovery exists.

## What Not To Do Next

- Do not loosen dispatch reuse eligibility without a dedicated review.
- Do not turn the wiki into authoritative state.
- Do not add provider-specific hooks to the dispatch shell template.
- Do not use tmux scan as the source of truth for runtime state.
- Do not merge dispatch / checkpoint / bootstrap artifacts into one file.
- Do not add stale lock auto-recovery without careful dead-process detection design.
- Do not expand orchestrator surface before production use validates the current design.
