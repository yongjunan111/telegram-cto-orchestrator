# telegram-cto-orchestrator

A protocol/control-plane for AI work delegation. Rooms hold operational state, handoffs carry structured task contracts, reviews gate downstream propagation, dispatch allocates tmux sessions with auto-read bootstrap.

## Source of Truth

- **Authoritative state**: code, `.orchestrator/rooms/*.yaml`, `.orchestrator/handoffs/*.yaml`, `.orchestrator/runtime/sessions/*.yaml`, `.orchestrator/peer_registry.yaml`, git history
- **Derived artifacts** (non-authoritative): `.orchestrator/runtime/dispatches/`, `.orchestrator/runtime/checkpoints/`, `.orchestrator/runtime/bootstrap/`, `.orchestrator/wiki/`

Do not mix them. Wiki and runtime artifacts are compiled/derived views. They never override YAML state or code.

## Invariants

- Never write derived content into authoritative YAML
- Never use tmux scan as truth — runtime/session YAML is the source
- Fresh dispatch is default; reuse requires eligible clean idle session + live tmux + exact pane target
- Reuse dispatch must acquire per-session O_EXCL lock before any state mutation
- Bootstrap failure never aborts dispatch, but never displays stale bootstrap either
- Checkpoint/bootstrap filename components must be slug-safe
- Internal YAML references are re-validated on read paths before filename/subprocess use
- Runtime artifact writers refuse symlinked base dirs, symlinked parent chains, and symlinked target files
- Review authority check before any review state write
- Approval gate blocks when validation or acceptance coverage is incomplete
- Room blocker is a hard stop for dispatch allocation
- Parse errors in runtime session state → `cannot_allocate` (never fresh fallback)
- Stale/dead tmux bindings are never used for wait or reuse decisions
- wiki-suggest is suggestion-only — never writes to wiki files directly
- Fingerprint ownership: manual `wiki-suggest` command is read-only; only the auto hook after approve/rework writes `wiki_suggest.generated_hints` to handoff state
- Dedupe reads stored fingerprints from prior handoffs, never re-generates from current room state
- Wiki-suggest failure never affects the parent approve/rework operation (best-effort)

## Reference

- `.orchestrator/wiki/current-state.md` — shipped milestones, current shape, bottlenecks
- `.orchestrator/wiki/decisions.md` — design rationale for major choices
- `.orchestrator/wiki/deferred.md` — intentionally not-yet-built work
- `.orchestrator/wiki/session-runtime.md` — runtime/session/dispatch/bootstrap details
- `.orchestrator/wiki/patterns.md` — recurring operational patterns
- `.orchestrator/wiki/lessons.md` — mistakes and their implications
- `.orchestrator/wiki/protocol.md` — wiki read/write rules
- `docs/architecture.md` — full architecture spec
