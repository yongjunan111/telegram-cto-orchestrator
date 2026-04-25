# GC Audit Design Debate Notes

Recorded: 2026-04-23 KST

This note records the design debate between Codex and the CTO Claude session about
Idle-Triggered Context Garbage Collection and the proposed `room gc-audit` command.

This file is design rationale, not authoritative state. Authoritative state remains
code, `.orchestrator/*.yaml`, and git history.

---

## 1. Starting Point

The original idea was "Idle-Triggered Context Garbage Collection":

- long-running LLM sessions waste tokens and slowly pollute live context;
- after `last_active_time` stays idle for about 2 hours, the orchestrator could wake a worker;
- that worker could analyze the room/session state, archive completed work, and clear live context.

The first major correction was that this should not start as automatic cleanup.

The agreed V1 should be:

```text
orchctl room gc-audit <room-id>
```

V1 is a read-only safety audit. It does not archive sessions, kill tmux panes,
compact provider context, mutate wiki files, or change room/session YAML.

The point of V1 is to answer a narrower question:

```text
Does this room look like its live session context has already been promoted into durable state?
```

---

## 2. Main Design Tension

The biggest disagreement was about `stale_tmux`.

Claude's earlier position:

```text
If a session is bound to an approved handoff, but its tmux pane is dead,
the session should be at-risk with reason stale_tmux.
```

Codex pushed back:

```text
Tmux liveness is a runtime observation, not the source of truth.
V1 is a semantic promotion audit.
It should judge promotion using room/handoff/review/session YAML plus git,
not live tmux state.
```

After debate, Claude accepted the Codex position.

Final resolution:

```text
stale_tmux is not an at-risk reason in V1.
tmux liveness is recorded only as runtime_observation.
```

---

## 3. Final Signal Model

The agreed model has three tiers.

### Tier 1: Authoritative Signals

These are allowed to affect `audit_verdict`.

- room YAML
- handoff YAML
- review outcome
- session YAML
- peer registry `cwd`
- git HEAD, branch, dirty state, upstream, ahead/behind
- git layout reason codes

Only Tier 1 signals can move a session between:

```text
promoted | at-risk | unbound | parse-error
```

### Tier 2: Runtime Observations

These are reported for operator awareness, but they do not affect
`audit_verdict`.

- tmux target alive/dead/unknown
- process or pane observation
- observation timestamp
- observation method

The report should make this explicit:

```text
Runtime observations are not authoritative.
audit_verdict is computed from Tier 1 signals only.
```

### Tier 3: Future V2 Mechanics

These happen only in a future command:

```text
orchctl session archive <session-id> --from-report <report-path>
```

V2 must revalidate live YAML hashes, git state, and runtime state before doing
anything destructive or state-changing. V1 does not perform these actions.

---

## 4. Debate Log

### Entry 1: Should stale tmux make a session at-risk?

Claude objection:

```text
Dead tmux plus approved handoff looks operationally suspicious.
If the report says promoted, the operator may think the session is still alive.
V2 also cannot reliably kill a pane that is already gone.
```

Codex counter:

```text
That is an operator visibility issue, not a semantic promotion issue.
V1 should answer whether work was promoted, not whether the runtime shell is alive.
Show tmux state in runtime_observation, but do not let it block promoted.
```

Resolution:

```text
Codex position accepted.
stale_tmux is removed from at-risk reason codes.
tmux state is reported as runtime_observation only.
```

### Entry 2: Does dead tmux imply dirty or lost work?

Claude objection:

```text
If the worker died, there may be unsaved or unpromoted work.
```

Codex counter:

```text
If worktree state exists on disk, git status catches it as dirty_git.
If the handoff was not approved, pending_review or changes_requested_pending catches it.
The tmux death itself adds no new semantic evidence.
```

Resolution:

```text
dirty_git and review state cover this risk.
Dead tmux remains observed-only.
```

### Entry 3: Does dead tmux make session YAML stale?

Claude objection:

```text
If the pane died, the session YAML might be stale.
```

Codex counter:

```text
The orchestrator already treats session YAML as runtime truth.
Using tmux scan to overrule YAML violates the existing rule that tmux is not truth.
```

Resolution:

```text
Session YAML remains authoritative for V1.
Tmux liveness does not override it.
```

### Entry 4: How should unbound sessions affect the room verdict?

Shared position:

```text
unbound should not force the room to some-at-risk.
```

Resolution:

```text
unbound is neutral for room-level coherent calculation.
However, unbound sessions must be surfaced prominently as cleanup-not-eligible under V1.
```

### Entry 5: How should pending review be classified?

Shared position:

```text
A completed handoff without an approving review is not promoted.
```

Resolution:

```text
pending_review is an at-risk reason for bound sessions.
```

### Entry 6: Timestamp collision policy

Claude earlier option:

```text
Microsecond timestamp collision could fail closed.
```

Codex preference:

```text
Use microsecond timestamp plus monotonic suffix.
Failing closed on filename collision is too unfriendly for operators.
```

Resolution:

```text
Use microsecond timestamp plus suffixes like -001, -002 on collision.
```

---

## 5. Final Plan Delta

Compared with the previous plan, these changes are now locked:

- remove `stale_tmux` from at-risk reason codes;
- add a `runtime_observation` section to the report;
- record tmux liveness as observed-only;
- make `audit_verdict` the field name, never `status`;
- keep `pending_review` as at-risk;
- keep `unbound` neutral for room-level coherent, but mark it cleanup-not-eligible under V1;
- use microsecond timestamp plus monotonic suffix for report filenames;
- keep V1 report free of wiki suggestions;
- require V2 to recompute live state instead of trusting the report blindly.

---

## 6. Final Verdict

Final joint verdict:

```text
APPROVE WITH CHANGES
```

Implementation should not start from automatic idle cleanup.

The next step should be:

```text
1. Write the ADR for Room-Level Promotion Audit.
2. Then implement the V1 skeleton for orchctl room gc-audit <room-id>.
3. Keep session archive and idle trigger as future V2/V3 work.
```

The key architectural line:

```text
V1 judges semantic promotion.
V2 handles runtime cleanup.
Idle automation comes later.
```
