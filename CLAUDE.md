# telegram-cto-orchestrator

A protocol/control-plane for AI work delegation. Rooms hold operational state, handoffs carry structured task contracts, reviews gate downstream propagation, dispatch allocates tmux sessions with auto-read bootstrap.

## Source of Truth

- **Authoritative state**: code, `.orchestrator/rooms/*.yaml`, `.orchestrator/handoffs/*.yaml`, `.orchestrator/runtime/sessions/*.yaml`, `.orchestrator/peer_registry.yaml`, git history
- **Derived artifacts** (non-authoritative): `.orchestrator/runtime/dispatches/`, `.orchestrator/runtime/checkpoints/`, `.orchestrator/runtime/bootstrap/`, `.orchestrator/wiki/`

Do not mix them. Wiki and runtime artifacts are compiled/derived views. They never override YAML state or code.

## Working Milestones (Current Loop)

- spec → execute → evidence → review → approval gate → rework
- Room has 3-layer concerns: memory / contract / discovery
- Handoff has kind: implementation | discovery
- Review authority is separated from execution authority; self-review blocked
- Validation coverage + acceptance coverage are explicit and approval-gated
- `room readiness` recommends next action from room + handoff state
- `handoff brief` is discovery-aware
- Runtime session state tracked in `.orchestrator/runtime/sessions/`
- `handoff dispatch-plan` recommends fresh/reuse/wait/cannot_allocate
- `handoff dispatch` executes: creates fresh tmux session or reuses eligible idle clean session
- Dispatched tmux sessions get shell hooks (`orch_checkpoint`, `orch_compact`, `orch_bootstrap`, EXIT trap)
- `session checkpoint` writes derived snapshot; `session bootstrap` auto-runs on dispatch

## Next Work

1. **Security review** — sweep dispatch/checkpoint/bootstrap paths for path-traversal, tmux injection, subprocess arg handling beyond already-fixed cases
2. (Deferred) Provider-specific `/compact` auto-detection — currently manual via `orch_compact`
3. (Deferred) Light mode — not yet explored

## Invariants

- Never write derived content into authoritative YAML
- Never use tmux scan as truth — runtime/session YAML is the source
- Fresh dispatch is default; reuse requires eligible clean idle session + live tmux
- Bootstrap failure never aborts dispatch, but never displays stale bootstrap either
- Checkpoint/bootstrap filename components must be slug-safe
- Review authority check before any review state write
- Approval gate blocks when validation or acceptance coverage is incomplete
- Room blocker is a hard stop for dispatch allocation

## Reference

- `.orchestrator/wiki/current-state.md` — shipped milestones, current shape, bottlenecks
- `.orchestrator/wiki/decisions.md` — design rationale for major choices
- `.orchestrator/wiki/deferred.md` — intentionally not-yet-built work
- `.orchestrator/wiki/session-runtime.md` — runtime/session/dispatch/bootstrap details
- `.orchestrator/wiki/patterns.md` — recurring operational patterns
- `.orchestrator/wiki/lessons.md` — mistakes and their implications
- `.orchestrator/wiki/protocol.md` — wiki read/write rules
- `docs/architecture.md` — full architecture spec
