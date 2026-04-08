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
- `handoff dispatch-plan` / `handoff dispatch` share one decision helper
- Dispatched tmux sessions get shell hooks + auto-read bootstrap
- Dispatch hardened against stale tmux bindings, parse-error fresh-fallback, tampered internal refs, and shell injection via send-keys
- Runtime artifact writers (dispatch/checkpoint/bootstrap/hook) use safe-write helper: containment + symlink-refuse + atomic rename

## Next Work

1. Reuse race guard — CAS/file lock so two parallel dispatches cannot claim the same idle session
2. Exact pane/window targeting for tmux send-keys (currently session-only)
3. Hook install / bootstrap success semantics — surface these in dispatch result instead of silent warnings
4. Bootstrap footer wording — clarify authority boundary (checkpoint is derived, not source of truth)
5. (Deferred) Provider-specific `/compact` auto-detection
6. (Deferred) Light mode

## Invariants

- Never write derived content into authoritative YAML
- Never use tmux scan as truth — runtime/session YAML is the source
- Fresh dispatch is default; reuse requires eligible clean idle session + live tmux
- Bootstrap failure never aborts dispatch, but never displays stale bootstrap either
- Checkpoint/bootstrap filename components must be slug-safe
- Internal YAML references are re-validated on read paths before filename/subprocess use
- Runtime artifact writers refuse symlinked base dirs, symlinked parent chains, and symlinked target files
- Review authority check before any review state write
- Approval gate blocks when validation or acceptance coverage is incomplete
- Room blocker is a hard stop for dispatch allocation
- Parse errors in runtime session state → `cannot_allocate` (never fresh fallback)
- Stale/dead tmux bindings are never used for wait or reuse decisions

## Reference

- `.orchestrator/wiki/current-state.md` — shipped milestones, current shape, bottlenecks
- `.orchestrator/wiki/decisions.md` — design rationale for major choices
- `.orchestrator/wiki/deferred.md` — intentionally not-yet-built work
- `.orchestrator/wiki/session-runtime.md` — runtime/session/dispatch/bootstrap details
- `.orchestrator/wiki/patterns.md` — recurring operational patterns
- `.orchestrator/wiki/lessons.md` — mistakes and their implications
- `.orchestrator/wiki/protocol.md` — wiki read/write rules
- `docs/architecture.md` — full architecture spec
