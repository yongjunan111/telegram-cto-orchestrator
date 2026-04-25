# V1 Remaining Work

This file isolates the post-v1 backlog so the team can compact the CTO session
and move into production use without re-reading the whole deferred list.

## Current Status

- `telegram-cto-orchestrator` is considered **v1 complete for production use**.
- Dispatch/runtime/session risk that blocked real usage has been addressed:
  stale bindings, parse-error fallback, internal reference validation,
  tmux quoting, safe-write, reuse race, and exact pane targeting.
- What remains is operational polish and deliberate deferred work, not a
  blocker for using the system on real repos.

## Immediate Operational Polish

These are the only remaining items worth revisiting soon if production use
exposes pain:

1. **Hook install / bootstrap success semantics**
   - Hook install and bootstrap display are still best-effort warnings.
   - Dispatch success does not yet cleanly distinguish full success vs degraded
     success.

2. **Bootstrap footer wording**
   - The footer still mentions checkpoint artifacts too close to authoritative
     state.
   - This is wording-only, but it should be corrected to preserve the
     authoritative/derived boundary.

3. **Stale lock / stale session operational tooling**
   - Crashed dispatches can leave lock files behind.
   - Dead tmux sessions can leave runtime session YAML behind.
   - Recovery is still manual (`rm` / operator reconciliation).

## Deliberately Deferred

These remain out of scope until production use creates a real need:

- Provider-specific `/compact` auto-detection
- Light mode
- Background sweeper / heartbeat automation
- Stale lock auto-recovery without strong dead-process detection
- Program CRUD / lineage expansion
- Constraint auto-verdict / scoring engine
- Peer auto-discovery / introspection

## What To Do Next

The next phase is **production use**, not more orchestrator implementation.

First real usage target:

- `/home/dydwn/projects/first_shovel`

Expected next workflow:

1. Open a new room/program context for `first_shovel`
2. Run repo discovery conservatively
3. Cut Day 1 into a bounded worker task
4. Dispatch one worker
5. Observe what breaks in real use before adding more orchestrator surface

## What Not To Do Next

- Do not reopen orchestrator implementation just because polish items exist.
- Do not expand the protocol surface before real usage justifies it.
- Do not treat the wiki as authoritative state.
- Do not loosen reuse or targeting guarantees without dedicated review.
