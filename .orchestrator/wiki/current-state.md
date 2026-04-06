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
- `30cf4da` room discovery planning artifacts
- `35e92cf` session wiki protocol and operating knowledge layer

## Current Shape Of The System

- The repo now has a working minimum loop:
  - spec
  - execute
  - evidence
  - review
  - approval gate
  - structured rework
- Room state has three distinct concern layers:
  - **memory** (`request_summary`, `current_summary`, `open_questions`, `blocker_summary`) — situational context
  - **contract** (`constraints`, `acceptance_criteria`) — normative spec
  - **discovery** (`problem_statement`, `confirmed_facts`, `assumptions`, `options_considered`, `decisions_made`, `dependencies`, `implementation_unknowns`, `chosen_direction`, `readiness_notes`) — pre-handoff planning artifacts
- The strongest parts are execution-after-contract, evidence capture, approval gating, and the discovery planning layer.
- The weakest part is bridging discovery artifacts into the handoff brief (discovery-aware brief) and pre-handoff readiness assessment.

## Wiki Layer Status

- `.orchestrator/wiki/` now exists as a compiled operating knowledge layer for this repo.
- It is meant to survive context compression and runtime/model changes.
- It should be read at session start and updated at workflow boundaries.
- It is support infrastructure for the protocol, not a new source of truth.

## Current Bottleneck

- Discovery artifacts now exist in room state, but the handoff brief does not yet read them.
- There is no formal readiness assessment before issuing an implementation handoff.
- We can structure execution well once a handoff exists; the gap is now the handoff creation decision.

## Next Priority

- Readiness packet: a pre-handoff readiness assessment that reads room discovery artifacts and surfaces whether the room is ready for an implementation handoff.
- Discovery-aware brief: update `handoff brief` to read the room's discovery section and surface relevant planning context to the worker.

## How To Use This File

- Read this first at the start of a new session.
- Update it after shipped milestones or when the “current next step” changes.
