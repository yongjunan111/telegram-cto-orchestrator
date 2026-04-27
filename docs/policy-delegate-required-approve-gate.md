# Policy: delegate_required Approve Gate (V1)

## Why this gate exists

Across the V2 session-archive rework cycle (rework-1 through rework-5 + docs), the
team observed that completion gates and acceptance coverage by themselves do
not enforce the role boundary the orchestrator depends on. CTO can mark a
handoff completed and approve it in the same session, even when the handoff
was supposed to be delegated. The existing delegate_required gate (V1)
enforces a child-ledger requirement at completion time, but does not enforce
who held the team-lead role or that an independent adversarial review actually
ran before approval.

This V1 approve gate adds role-enforcement evidence at approve time for
`delegate_required` handoffs only. Non-delegate handoffs (`direct`,
`delegate_optional`, missing) are unaffected — zero additional burden.

## What evidence is required

For a `delegate_required` handoff to pass approve, all of the following must hold:

1. `handoff.to` is not `cto` and `resolution.completed_by` is not `cto`. CTO is
   reviewer, not delegate owner or completer.
2. The assignee peer in `peer_registry.yaml` has `team-lead` in its
   `capabilities` list. `team-lead` is a *capability*, not a peer type.
3. `execution.child_handoffs` has at least one entry with `status: completed`
   (V1 ledger gate, preserved). This is worker evidence, not a plan artifact.
4. An adversarial review markdown artifact exists at
   `.orchestrator/runtime/adversarial-reviews/<handoff-id>/<UTC-iso-ts>.md`
   matching all five strict conditions:
   - frontmatter `handoff_id` equals the current handoff id
   - frontmatter `verdict` equals exactly `ship-as-is` (no other value passes)
   - frontmatter `review_target_completed_at` equals exactly
     `timestamps.completed_at` of the handoff (primary freshness check)
   - artifact filesystem `st_mtime` must be strictly greater than the handoff's
     `timestamps.completed_at` (UTC; rework-2 hardening — stale or equal mtime
     fails the gate; the helper always writes with a fresh mtime satisfying this)
   - frontmatter `reviewer` is not the assignee and not the completer
   - the file path is under
     `.orchestrator/runtime/adversarial-reviews/<handoff-id>/`

If any condition fails, the gate prints a structured `missing: ...` message
naming the violation and exits 1.

## Artifact format

Markdown with YAML frontmatter:

```text
---
findings_count: <non-negative integer>
handoff_id: <handoff-id>
review_target_completed_at: <UTC iso timestamp copied from handoff timestamps.completed_at>
reviewer: <reviewer-peer-id>
verdict: ship-as-is | needs-fix | reject
---

<free-form markdown review body>
```

Use the helper to produce this:

```bash
orchctl handoff add-adversarial-review <handoff-id> \
  --reviewer <peer-id> \
  --verdict ship-as-is \
  --findings-count 0 \
  --body-file /path/to/review-body.md
```

The helper validates that the handoff exists and has a `timestamps.completed_at`,
copies that value into the frontmatter, and writes atomically under
`.orchestrator/runtime/adversarial-reviews/<handoff-id>/<UTC-now-ts>.md`.

## Role boundaries (enforced by this gate)

- **CTO** — reviewer only. Cannot be `handoff.to` of a `delegate_required`
  handoff. Cannot be `resolution.completed_by`. CTO triggers adversarial review
  (writes the markdown artifact via the helper) but does not approve handoffs
  they are assigned to or completed by.
- **team-lead peer** — owns the delegate. Must have `team-lead` in
  `capabilities` in `peer_registry.yaml`. Decomposes into internal Agent
  tasklets, records each via `add-subtask`, completes the handoff with proper
  coverage.
- **worker / sub-agent** — completes the actual implementation. Recorded in
  `execution.child_handoffs` as the worker evidence.
- **adversarial reviewer** — independent peer (not the assignee, not the
  completer). Writes the review artifact via the helper. CTO is the typical
  reviewer for team-lead-owned handoffs.

## Why this must land before V3 idle archive automation

V3 idle session archive automation will run `orchctl session archive` against
sessions that meet idle/promotion criteria. If the underlying handoff is
`delegate_required`, V3 should NOT auto-archive without the role-enforcement
evidence having gated approval first. Otherwise CTO can quietly bypass the
team-lead role and have V3 archive sessions that were never independently
reviewed.

The V1 gate this policy describes is the prerequisite that makes V3
auto-archive safe for `delegate_required` handoffs.

## Known Limitations / Out-of-Scope Threats

The following attack vectors are explicitly out of scope for V1 and are not
defended against by this gate.

### Filesystem permissions tampering

If an attacker has write access to `.orchestrator/runtime/adversarial-reviews/`,
they can create or overwrite any artifact with arbitrary frontmatter. The gate
reads and validates file contents, but cannot detect content that was written by
a privileged attacker who bypassed the helper. Filesystem permissions on the
`.orchestrator/` tree are the ultimate authority; the gate is only as strong as
the OS-level access control enforcing who can write to that directory.

### `peer_registry.yaml` tampering

The gate validates that the artifact's `reviewer` field exists in
`peer_registry.yaml` and that the assignee has `team-lead` capability. If an
unauthorized actor modifies `peer_registry.yaml` directly — adding a fake peer
with `team-lead` capability, or inserting a `team-lead` capability onto an
existing peer — the gate is defeated. The integrity of `peer_registry.yaml`
itself is upstream of this gate and is enforced by repository access control and
git history, not by the gate logic.

### Reviewer-as-approver collusion

The gate enforces `reviewer != assignee` and `reviewer != completer`, but does
NOT enforce `reviewer != the peer invoking orchctl handoff approve` (the
`--by` argument). An adversarial reviewer who also holds `team-lead` capability
can write a `ship-as-is` artifact for a handoff they reviewed and then call
`handoff approve --by <themselves>` to approve it. The approve command's
`_enforce_review_authority` check independently limits who can call approve, but
that check does not cross-reference against the artifact's `reviewer` field.
Separating the reviewer identity from the approver identity via
capability-gating is deferred to a future gate version.

### TOCTOU on artifact content

Between the gate's read of the artifact file and the actual write of the
approved state to the handoff YAML, an attacker with filesystem write access
could mutate the artifact (e.g., change `verdict` from `needs-fix` back to
`ship-as-is` after a failed gate run, then retry). The gate does not hold a
file lock or re-verify the artifact after writing state. Defending against this
race is out of scope for V1; it requires either file locking across the gate
or a cryptographic commitment scheme.

## Publication-state invariant (rework-3)

The `add-adversarial-review` helper uses a temp-file + atomic-link write
pattern to guarantee all-or-nothing artifact creation from the approve gate's
perspective.

Temp file naming: `.<ts>.md.tmp.<pid>.<counter>` — leading dot, trailing
`.tmp.<pid>.<counter>` digits. This name does NOT end in `.md`, so the reader's
`endswith('.md')` filter skips it unconditionally even if the helper crashes
mid-write.

Publication states:

| State | On disk | On failure remainder | Reader skips? |
|---|---|---|---|
| S1 BEFORE | dirfd only, no temp/final | nothing | n/a |
| S2 DURING write | temp partial | temp partial; final absent | YES (no `.md` suffix) |
| S3 POST-write PRE-fsync | temp full unsynced | temp partial; final absent | YES |
| S4 POST-fsync PRE-publish | temp fsynced; final absent | temp only; final absent | YES |
| S5 POST-publish | final `.md` exists | dir fsync fail → final committed | n/a (success) |

On any write or fsync error the helper unlinks the temp via `dir_fd` and exits
1. The final `.md` is only created by `os.link(temp, final, follow_symlinks=False)`,
which is atomic on POSIX: it either succeeds fully or fails with EEXIST (collision
retry). An attacker cannot create a partial `.md` via this helper path.
