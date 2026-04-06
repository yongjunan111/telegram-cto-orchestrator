# Patterns

This file captures operational patterns that keep recurring.

## Prompt Pattern For Worker Tasks

Every serious worker prompt should explicitly include:

- goal
- non-goals
- invariants
- failure examples
- validation or coverage expectations
- explicit “do not change” boundaries
- concrete verification cases

Reason: worker quality improves sharply when boundaries are explicit instead of implied.

## Review Pattern

When reviewing a change:

- start with findings if any
- prioritize blockers over summary
- check stored truth vs rendered output mismatches
- check silent skips / hidden partial failures
- check whether a new feature accidentally weakens an existing gate

## Separation Pattern

Keep these separate unless there is a very strong reason not to:

- memory vs contract
- execution state vs review state
- stored authoritative state vs derived rendered view
- free-form notes vs structured must-address items

## Handoff Readiness Pattern

Implementation handoff should exist when one peer can start meaningful work without another clarification round.

If that is not true yet, prefer:

- room memory updates
- room contract updates
- discovery/scoping work
- discovery handoff instead of implementation handoff

## Rework Pattern

- Never reopen the original handoff.
- Preserve source review history.
- Carry structured delta into the next brief.

## Session Bootstrap Pattern

At the start of a new session:

1. Read `current-state.md`
2. Read `decisions.md`
3. Read `deferred.md`
4. Read relevant code only after that

This reduces repeated rediscovery and keeps planning grounded.

See also: `protocol.md` for read points, write points, and page ownership rules.

## Wiki Maintenance Pattern

Treat wiki updates as part of normal work:

- decision made -> `decisions.md`
- defer intentionally -> `deferred.md`
- recurring workflow stabilized -> `patterns.md`
- mistake pattern discovered -> `lessons.md`
- milestone becomes commit-ready or ships -> `current-state.md`

The wiki should be updated at workflow boundaries, not “sometime later.”
