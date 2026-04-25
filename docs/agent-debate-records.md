# Agent Debate Records

This file records notable design debates between orchestrator agents.

It is not authoritative state. It is a human-readable memory of how the agents
argued, changed their minds, and reached design decisions. Authoritative state
still lives in code, `.orchestrator/*.yaml`, and git history.

---

## Publication Stance

This convention is currently an internal operating rule.

The policy may become public later, because it explains how agent debates are
recorded, how records differ from ADRs, and who may append. For now, keep both
the policy and the debate content internal until the user explicitly approves
publication.

Default stance:

```text
Internal-first, publish only after explicit review.
```

That means:

- the governance rules are internal by default;
- debate records are internal by default;
- a future public version may be created as a redacted summary;
- nothing should be published just because it is marked `public-ok`;
- these records are still not authoritative state.

### Visibility Labels

Every debate entry should carry one of these labels for future publication
review:

```text
Visibility: public-ok
Visibility: redacted-public-summary
Visibility: internal-only
```

Use `public-ok` when the record contains only architecture reasoning, command
shape, implementation boundaries, or process decisions. This label means
"eligible for future public release," not "publish immediately."

Use `redacted-public-summary` when the public repo should retain the shape of
the decision, but the detailed discussion includes private paths, customer data,
security details, unreleased strategy, credentials, or personal information.

Use `internal-only` when even a redacted summary would reveal sensitive
operational, security, business, or personal context.

---

## Governance Policy

### Roles

- User: can instruct any agent to append or edit any record. This is the
  absolute override.
- CTO: default scribe, not curator. The CTO appends by default, claims numbering
  at write time, and recommends detail-file versus index-only form. The CTO has
  no deletion authority over records written by other agents. Disagreement must
  be captured as a new dissent entry.
- Codex and other external agents: may append when the user directly asks, or
  when all recording criteria are met and the CTO has been unavailable for more
  than 24 hours. Both paths require CTO post-hoc acknowledgment.
- Workers and subagents: do not append directly. They submit a
  `debate_candidate` summary in handoff or review output. If a
  `changes_requested` plus rework cycle validates the worker's position, the
  index entry becomes automatically eligible and cannot be vetoed by the CTO.

### Recording Criteria

All four conditions must hold:

1. Two or more agents expressed opposing technical positions with reasoning.
2. One position changed, or the plan changed, because of the debate.
3. The outcome is concrete: plan delta, invariant lock, scope narrowing, or ADR
   bullet.
4. Future agents would likely repeat the same argument without this record.

If any condition fails, it is chat, not a debate record.

### Append Rules

- Numbering is claimed at write time.
- Non-CTO append requires CTO post-hoc acknowledgment.
- Acknowledgment is visibility, not approval. The record persists even if the
  CTO disagrees.
- CTO responses are limited to silent acceptance, a new dissent entry, or an
  edit request to the user.

### Detail-File Triggers

Create a separate detailed file when at least two are true:

- there were two or more objection/counter rounds;
- an architecture boundary changed;
- the result affects an ADR or implementation scope;
- the index entry would exceed three lines.

Use an index-only entry for short, narrow disagreements that can be summarized
in three bullets or fewer.

### Body Conventions

- Keep each party to at most three bullets per round.
- Include a required `Resolution` field: who prevailed, why, and what changed.
- Freeze records after CTO acknowledgment. Add errata as new entries instead of
  editing the body.
- Link to the ADR when a final architectural decision exists.
- If a later ADR reverses the decision, add a `Superseded-by: ADR-NNN` pointer
  without rewriting the original body.

### Non-Goals

- Not a transcript dump.
- Not a record of simple agreement.
- Not self-praise.
- Not an ADR substitute.
- Not a commit or PR description substitute.
- Not authoritative state.

---

## Why Keep This

The orchestrator is not only a command runner. It is also a working style:

- one agent proposes a design;
- another agent reviews or attacks it;
- disagreement exposes hidden assumptions;
- the final plan becomes smaller, safer, and easier to implement.

Keeping these debates gives future agents a way to understand not just what was
decided, but why the decision survived pressure.

---

## Debate 001: Room-Level GC Audit

Date: 2026-04-23 KST

Visibility: public-ok

Record type: detailed file plus index entry

Participants:

- Codex
- CTO Claude session
- User as product/architecture owner

Detailed note:

- [GC Audit Design Debate Notes](gc-audit-design-debate.md)

### Starting Question

The original idea was Idle-Triggered Context Garbage Collection:

```text
If an LLM session has been idle for long enough, can the orchestrator archive
completed work and clear live context automatically?
```

The debate quickly narrowed the first safe version:

```text
Do not implement automatic cleanup first.
Implement a read-only room-level promotion audit first.
```

### Main Disagreement

The sharpest disagreement was whether dead tmux state should make a session
`at-risk`.

Claude initially argued:

```text
If the tmux pane is dead, the bound session looks operationally suspicious.
That should be at-risk.
```

Codex pushed back:

```text
Tmux liveness is not truth.
V1 should judge semantic promotion from YAML and git.
Tmux state should be reported as runtime observation only.
```

Final resolution:

```text
stale_tmux is not an at-risk reason.
It belongs in runtime_observation, not audit_verdict.
```

### Final Shape

The debate produced a three-tier model:

```text
Tier 1: authoritative signals
  room/handoff/review/session YAML + peer registry + git
  -> affects audit_verdict

Tier 2: runtime observations
  tmux/process/pane liveness
  -> report-only, no verdict effect

Tier 3: future archive mechanics
  live revalidation + tmux kill/session upsert
  -> V2 only
```

Final verdict:

```text
APPROVE WITH CHANGES
```

Next step:

```text
Write ADR first, then implement orchctl room gc-audit <room-id> V1 skeleton.
```

---

## Format For Future Debates

Use this shape for future entries:

```text
## Debate NNN: <Title>

Date:
Participants:
Detailed note:

### Starting Question
...

### Main Disagreement
...

### Final Shape
...

### Final Verdict
...
```

The best entries should preserve the moment where an agent changed its mind.
That is usually where the real architecture lives.
