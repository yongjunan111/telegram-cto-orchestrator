# Wiki Protocol

This directory is the compiled operating knowledge layer for the repo.
It is not authoritative state.

Authoritative truth remains:

- code in the repo
- YAML state under `.orchestrator/`
- git history

The wiki exists to preserve current state, design rationale, deferred work, recurring patterns, and lessons across session compression, thread restart, and runtime/model changes.

## Purpose

Use this wiki to answer:

- what exists now
- why key decisions were made
- what was intentionally deferred
- what patterns we now treat as standard
- what mistakes we should not repeat

Do not use this wiki as a substitute for checking code or YAML when exact truth matters.

## Read Points

Read the wiki at these points:

1. **Session start**
   - Read `current-state.md`
   - Read `decisions.md`
   - Read `deferred.md`

2. **Before delegating work**
   - Read `patterns.md`
   - Read any relevant decision entries tied to the area being changed

3. **Before changing the roadmap**
   - Re-read `current-state.md`
   - Re-read `deferred.md`
   - Re-read `lessons.md`

## Write Points

Update the wiki as part of normal workflow, not as a separate ritual.

1. **When a design decision is made**
   - Update `decisions.md` immediately

2. **When work is intentionally deferred**
   - Update `deferred.md` immediately

3. **When a recurring pattern becomes explicit**
   - Update `patterns.md`

4. **When an avoidable mistake or repeated failure pattern is discovered**
   - Update `lessons.md` immediately

5. **When a work unit becomes commit-ready or is shipped**
   - Update `current-state.md`

## Verification Point

At session start, verify that the wiki is not obviously stale relative to:

- recent commits
- current code paths
- current YAML/state semantics

If the wiki and code diverge, fix the wiki early before delegating more work.

## Page Ownership

### `current-state.md`

Update when:

- a new capability becomes commit-ready
- a roadmap priority changes
- the current bottleneck changes

### `decisions.md`

Update when:

- we make a non-obvious design choice
- we choose one boundary or authority model over another
- we pick a conservative gate or fallback policy

### `deferred.md`

Update when:

- we explicitly decide not to build something yet
- we choose advisory/manual treatment instead of a hard gate
- we accept a v0 limitation on purpose

### `patterns.md`

Update when:

- a workflow becomes repeatable enough to standardize
- we settle on a prompt/review/bootstrap pattern
- we clarify readiness rules

### `lessons.md`

Update when:

- we find a repeated failure pattern
- we discover a bug class we want to avoid structurally
- a review catches a category of mistake worth remembering

## Non-Goals

- Do not copy full chat transcripts into the wiki.
- Do not duplicate raw code listings.
- Do not invent facts not grounded in code, YAML, or accepted design decisions.
- Do not let the wiki become a second source of truth.

## Model-Agnostic Rule

This protocol is intentionally model-agnostic.

- Claude sessions may read and update it.
- Codex sessions may read and update it.
- Future orchestrator/worker runtimes may read and update it.

The protocol belongs to the repo, not to a single model runtime.
