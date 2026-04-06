# Deferred

This file records decisions to intentionally postpone work.

## Still Advisory / Manual

- `constraints` are still advisory/manual; they are shown in brief/review but not hard-gated.
- Semantic sufficiency of validation or acceptance evidence is still manual review, not auto-verdict.

## Not Yet Built

- Discovery/scoping artifact layer before handoff.
- Readiness rules for:
  - room creation
  - discovery handoff
  - implementation handoff
- Program layer with real program CRUD / lineage.
- Peer runtime abstraction and launcher layer.
- Telegram adapter / bridge that binds Telegram to role-based peers instead of a specific model runtime.
- Provenance for why room memory / room contract changed.

## Kept Conservative On Purpose

- No automatic reopening of completed handoffs.
- No review overwrite or re-review history yet.
- No scoring engine.
- No constraint auto-verdict.
- No hidden override for review authority.

## Legacy Compatibility We Keep Supporting

- Legacy `changes_requested` without `must_address` still reworks with a fallback notice.
- Older rooms with removed/derived fields are still read conservatively where possible.

## What Not To Do Next

- Do not bolt on more lifecycle states before discovery/scoping is clarified.
- Do not make model/runtime choice the center of the design before the protocol is finished.
- Do not turn the wiki into authoritative state.
