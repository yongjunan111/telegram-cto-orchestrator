# Current State

This wiki is compiled operating knowledge for the repo. It is not authoritative state.
Authoritative truth remains code, YAML state under `.orchestrator/`, and git history.

## What Exists Now

- `room` is the broad operational container.
- `room memory` stores situational context: request summary, current summary, open questions, blocker state, next action, manual phase.
- `room contract` stores room-level positive spec: constraints and acceptance criteria.
- `handoff` is the authoritative task handoff object with open/claimed/blocked/completed lifecycle.
- `handoff brief` is the worker-facing derived execution brief.
- `completion evidence` includes summary, files changed, verification, risks.
- `validation coverage` is explicit and approval-gated.
- `acceptance criteria coverage` is explicit for both room/task criteria and approval-gated.
- `review` is independent from execution state.
- `review authority` is separate from execution authority: reviewer-type peers only, no self-review by assignee or completer.
- `rework` is a new handoff, not a reopen of the original.
- `structured rework delta` carries `must_address` items from `request-changes` into the rework handoff and brief.

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

## Current Shape Of The System

- The repo now has a working minimum loop:
  - spec
  - execute
  - evidence
  - review
  - approval gate
  - structured rework
- The strongest parts are execution-after-contract, evidence capture, and approval gating.
- The weakest part is still pre-handoff discovery/scoping and the accumulation of planning knowledge.

## Wiki Layer Status

- `.orchestrator/wiki/` now exists as a compiled operating knowledge layer for this repo.
- It is meant to survive context compression and runtime/model changes.
- It should be read at session start and updated at workflow boundaries.
- It is support infrastructure for the protocol, not a new source of truth.

## Current Bottleneck

- Discovery/scoping conversations still live mostly in chat and operator memory.
- We can structure execution well after a handoff exists.
- We are weaker at deciding when something is ready for:
  - room creation
  - discovery handoff
  - implementation handoff

## Next Priority

- Add a discovery/scoping layer that turns user ↔ CTO clarification into persistent artifacts.
- Keep it conservative: compile planning knowledge into artifacts before inventing new lifecycle objects.

## How To Use This File

- Read this first at the start of a new session.
- Update it after shipped milestones or when the “current next step” changes.
