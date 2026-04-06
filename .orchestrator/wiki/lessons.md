# Lessons

This file records repeated mistakes and the fixes they imply.

## Silent Skip Is Dangerous

- We hit this with malformed handoff scanning.
- If a derived summary silently ignores broken authoritative files, the UI lies.
- Rule: surface parse failure or incompleteness; do not quietly drop it.

## Rendered Contradictions Are Blockers

- A rendered view that says two different truths is a real bug, even if state write logic is correct.
- Example pattern: showing recorded review outcome while also saying “no decision has been performed”.

## Review Separation Must Reach Consumers

- Splitting `completed` from `approved` in stored state is not enough.
- Downstream consumers must respect the split or review becomes ceremonial.

## The Prompt Must Carry Boundaries

- “Do X” is not enough for workers.
- What repeatedly matters is:
  - what not to do
  - what must remain true
  - what counts as failure
  - what must be verified

## The Next Problem Moves Upstream

- After evidence/review/rework became stronger, the bottleneck moved earlier.
- We are now weaker in discovery/scoping than in execution/review.

## Use Explicit Mappings For Gates

- Validation coverage works because it is explicit.
- Acceptance coverage works because it is explicit.
- If we let the model guess semantic adequacy, the gate weakens.

## Keep The Wiki Compiled, Not Authoritative

- The wiki helps survive compression and session restart.
- But it must never replace code, YAML state, or git history as source of truth.
