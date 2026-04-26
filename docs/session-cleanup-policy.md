# Session Cleanup Policy (V1)

This document is the operator-facing reference for `lib/session_cleanup.py`,
the read-only worker-session lifecycle report introduced by handoff
`orch-session-cleanup-policy-v1`. It is the companion policy to the
delegation gate (`orch-delegation-gate-schema`).

V1 is a **report-only** mechanism. It exists so an operator can decide what
to do with stale or completed worker sessions; it does not act on its own.

---

## V1 Invariants

These invariants are enforced in code (see `tests/test_session_cleanup_report.py`)
and MUST hold across any future refactor:

1. **Read-only / report-only.** No automatic tmux kill, no automatic YAML
   mutation, no automatic checkpoint generation. The module performs no
   subprocess spawn and never calls tmux.
2. **CTO review-pending or rework-pending sessions are never recommended for
   kill.** Sessions whose related handoff is in `pending_review` or
   `changes_requested` only ever receive `needs_cto_review` or
   `awaiting_review_evidence`. They never receive `leftover_after_complete`.
3. **Operator-led kill is the only sanctioned path in V1.** Even
   `leftover_after_complete` is a *signal* to the operator; it is not an
   instruction to auto-kill.
4. **The report is informational, not authoritative state.** Authoritative
   state remains in `.orchestrator/handoffs/*.yaml` and
   `.orchestrator/runtime/sessions/*.yaml`. The report is a derived view —
   re-running the report is always safe.

---

## Locked Recommendation Token Set

The cleanup report emits exactly one token per candidate from this closed
enum. Drift requires a coordinated handoff contract bump.

| Token | Meaning |
|---|---|
| `needs_worker_complete` | Worker session is busy on an open handoff and has been idle past the threshold. The worker should run `orchctl handoff complete`. |
| `needs_cto_review` | Worker filed a completion but no review outcome is recorded. CTO/reviewer needs to run `orchctl handoff review`. |
| `needs_session_checkpoint` | Session has been busy past the threshold without any checkpoint file. Operator should ask the worker to checkpoint. |
| `awaiting_review_evidence` | Review outcome is `changes_requested`. The worker (or a rework handoff) should produce evidence addressing the must-address items. |
| `leftover_after_complete` | Handoff is `completed` and `approved` but the worker session is still `busy`. Candidate for **operator-led** tmux kill. NEVER auto. |
| `parse_error` | Session YAML failed to parse OR the recorded `handoff_id` is not slug-safe. The binding cannot be trusted; investigate the file. |

The following tokens are explicitly **forbidden** and must never appear in
any rendered report:

```
auto_kill, safe_to_kill, archive_ready, can_archive, green_light,
ready_for_archive, auto_archive_eligible
```

The string-grep test `test_render_markdown_passes_forbidden_words_grep`
asserts these never appear.

---

## Recommendation Token Routing Rules

Routing is evaluated in this order. The first matching rule wins.

1. **`awaiting_review_evidence`** — review state is `changes_requested`.
   Always wins over kill-implying rules so a rework-pending session never
   gets a kill-implying token.
2. **`needs_cto_review`** — handoff status is `completed` and review state
   is `pending_review`.
3. **`leftover_after_complete`** — handoff status is `completed`, review
   state is `approved`, AND session status is `busy`. This signals an
   operator-led-kill candidate; the worker stayed running past approval.
4. **`needs_worker_complete`** — handoff status is `open`, session status
   is `busy`, idle minutes ≥ threshold.
5. **`needs_session_checkpoint`** — session status is `busy`, idle minutes
   ≥ threshold, and no checkpoint file exists for the session yet.
6. **`parse_error`** — session YAML fails to parse, or the recorded
   `handoff_id` ref is not slug-safe (path-traversal payloads, special
   characters, etc.). The handoff binding is dropped before any filesystem
   path is constructed from it.

If none of the above rules fire, the session is omitted from the report.
The report is a *focused punch list*, not a full session inventory.

---

## How to Read the Report

For each candidate, decide based on the recommendation token:

- **`needs_worker_complete`** — Open the worker tmux pane. Either prompt
  the worker to finish, or message the worker via the claude-peers MCP
  channel asking for a status report or completion run.
- **`needs_cto_review`** — Run `.venv/bin/python orchctl handoff review
  <handoff-id>` to inspect, then `handoff approve` or
  `handoff request-changes`.
- **`needs_session_checkpoint`** — In the worker pane, run
  `orchctl session checkpoint <session-id> --event manual --note "..."`.
  Do NOT kill the session; you would lose live work.
- **`awaiting_review_evidence`** — Either create a rework handoff
  (`orchctl handoff rework <source-id>`) or leave the session running and
  expect the worker to address must-address items.
- **`leftover_after_complete`** — Confirm the worker has nothing further
  to do (`tmux capture-pane -t <session>` or visit the pane), then run
  the operator-led `tmux kill-session` manually. Do NOT script this; V1
  refuses to automate it.
- **`parse_error`** — Open the session YAML and the linked handoff YAML;
  fix the underlying corruption (or remove the file if it is a leftover
  from a failed dispatch). Re-run the cleanup report to confirm the
  parse_error cleared.

---

## V1 Surface

The cleanup report is invocable from Python in V1:

```python
from lib.session_cleanup import build_cleanup_report, render_markdown

report = build_cleanup_report(
    rooms_filter=["orchestrator-hardening-gc-v1"],
    idle_minutes=60,
)
print(render_markdown(report))
```

A CLI subcommand (`orchctl session cleanup`) is **deferred to V1.5** to
avoid an `orchctl` edit conflict with the in-flight delegation-gate
handoff. Operators who want a markdown report today can call the function
directly from a Python REPL or paste the snippet above into a one-shot
script.

---

## What V1 Explicitly Does Not Do

- It does not call tmux. Live tmux state is irrelevant; YAML is truth.
- It does not look at provider-specific compaction, statusline, or any
  `git` operation.
- It does not delete, archive, or compress any session.
- It does not write any file (the caller may persist the markdown if
  desired, but the report function returns a dict).
- It does not auto-launch on a cron, hook, or background timer.

Each of these is intentional. Adding any of them is a V2+ scope decision
that requires a new handoff contract.

---

## Operating Rule: tmux Session vs. Task Boundary

tmux session is a responsibility boundary, not a task boundary. delegate_required
child handoffs default to team-lead-internal tasklets; external tmux dispatch
requires an explicit reason.

sub-handoff는 기본적으로 새 tmux 세션이 아니다. 새 tmux 세션은 책임 단위가
갈릴 때만 만든다.

Related troubleshooting record:

- `docs/issue-v2-session-archive-rework-churn.md` records why V2 session
  archive needed a third rework round: the earlier reviews fixed symptoms, but
  did not fully prove the report-write containment, concurrent filename
  reservation, and CLI help invariants.
