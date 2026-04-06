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
- Reason: memory answers “what is happening”; contract answers “what must be true”.

## Room State Has Three Distinct Concern Layers

- Room state separates memory, contract, and discovery into distinct sections, each with its own command.
- Memory (`room memory`): evolving situational context — what is happening right now.
- Contract (`room contract`): normative spec — what must be true for the room to be done.
- Discovery (`room discovery`): pre-handoff planning artifacts — what we know, what we assume, what we decided, and whether we are ready.
- Reason: mixing situational context, normative spec, and planning artifacts into a single section made each concern harder to maintain and surface to workers.

## Discovery Artifacts Are Stored, But Brief Integration Is Still Missing

- Room discovery artifacts exist and are writable via `orchctl room discovery`.
- The handoff brief does not yet read the discovery section; it surfaces memory and contract only.
- A formal readiness assessment before implementation handoff does not yet exist.
- Reason: discovery was the first layer to build; readiness/brief integration is the next gap, not discovery storage itself.
