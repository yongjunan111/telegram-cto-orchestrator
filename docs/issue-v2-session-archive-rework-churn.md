# Issue: V2 Session Archive Rework Churn

Date captured: 2026-04-26 KST

This note records why the V2 session archive work required a third rework
round after two apparently successful review cycles. It is not authoritative
state. Code, handoff YAML, room YAML, runtime session YAML, and git history
remain authoritative.

## Summary

The V2 archive design direction was sound, and the broad end-to-end flow was
working. The rework churn came from a narrower problem: the evidence gathered
for rework-1 and rework-2 proved the happy path and the exact reported symptom,
but did not prove the full safety invariant behind the symptom.

In short:

- rework-1 fixed the major V2 pipeline gaps, but left report-write containment
  and help wording edge cases.
- rework-2 fixed the exact reported containment hole and the option-level help
  text, but the review surface was still too narrow.
- the post-approval review found that the underlying write-safety contract was
  still incomplete, so rework-3 is required before commit.

## Timeline

### Initial V2 Implementation

The first V2 implementation added:

- `orchctl session archive <session-id> --from-report <absolute-path>`
- validation and revalidation
- archive bundle writing
- session archive marker stamping
- focused tests for validate, bundle, and CLI behavior

The first review found five issues:

1. real `gc-audit` / idle-snapshot outputs could not actually drive archive;
2. bundle output was mostly a null shell;
3. same-second archive bundle filenames could collide;
4. parent/child ledger state drifted;
5. validate-to-stamp drift was not rechecked.

### Rework 1

Rework-1 fixed the major pipeline shape:

- added `orchctl session archive-report <session-id>`;
- produced V2-contract YAML with `session_id`, `audit_verdict`, snapshots,
  and git state;
- made archive bundles meaningful instead of mostly null;
- added collision suffixing for sequential same-second archive bundle writes;
- added stamp-time CAS rehashing before writing the session archive marker;
- cleaned up the stale external-child-handoff model by recording internal
  tasklets instead of creating more orphan child YAML files.

Reported verification:

```text
uv run pytest tests/ -q -> 341 passed
```

Why that was not enough:

- the producer's report write path still used a too-deep `safe_write_text`
  trust boundary;
- CLI help still told operators to pass old V1 report types;
- the review focused on broad V2 flow correctness, not adversarial report-write
  behavior.

### Rework 2

Rework-2 was intentionally tiny:

- change archive-report writing so `safe_write_text` uses the reports root
  instead of the session-specific report directory;
- add a regression test where `<reports-root>/<session-id>` is a symlink before
  the producer creates the session directory;
- update `--from-report` option help to say it consumes V2 archive-report YAML.

Reported verification:

```text
uv run pytest tests/test_session_archive_report.py tests/test_session_archive_cli.py -q -> 29 passed
uv run pytest tests/ -q -> 342 passed
```

Why that was still not enough:

- the test proved the exact `<reports-root>/<session-id>` symlink case, but did
  not check symlinks above `reports-root`, such as `.orchestrator/runtime`;
- the file-name collision test proved sequential suffixing, but not concurrent
  reservation;
- the help check verified `orchctl session archive --help`, but not the parent
  `orchctl session --help` summary.

## Root Causes

### 1. Symptom Fix Instead Of Invariant Fix

The original finding was about `safe_write_text` being called with a base
directory that was too deep. Rework-2 moved the base from the session-specific
directory to the reports root. That fixed the concrete example, but not the
full invariant.

The invariant should be:

```text
archive-report must not write through any symlinked parent from the repo-owned
orchestrator tree down to the final report path.
```

Rework-2 only proved:

```text
archive-report refuses a symlink at <reports-root>/<session-id>.
```

That is a smaller claim. The gap matters because `safe_write_text` only checks
the parent chain beneath the `base_dir` the caller provides. If the caller
chooses a deep `base_dir`, anything above it becomes trusted implicitly.

### 2. Sequential Collision Tests Were Treated As Concurrency Safety

The producer has a helper that chooses `<timestamp>.yaml`, then `<timestamp>-1.yaml`
if the first file already exists. That works for sequential calls.

It does not prove concurrency safety:

```text
process A checks: <timestamp>.yaml does not exist
process B checks: <timestamp>.yaml does not exist
process A writes <timestamp>.yaml
process B writes <timestamp>.yaml with os.replace
```

The second write can overwrite the first report. That means process A's stdout
can point to a file whose contents no longer match process A's own snapshots
and git state.

The missing invariant is:

```text
the chosen report filename must be reserved atomically, or a concurrent writer
must retry with a different suffix without overwriting an existing report.
```

### 3. Help Verification Checked Only One Surface

Rework-2 updated the `--from-report` option help. That made this command look
correct:

```text
orchctl session archive --help
```

But this parent command still exposed the old wording:

```text
orchctl session --help
```

So the operator-facing contract was only partly fixed. The review evidence said
"help string verified", but it verified one help surface, not every surface
where the stale claim appeared.

### 4. Green Tests Were Accepted Without Asking What They Did Not Prove

Both reworks reported full-suite success. That was useful, but full-suite green
only proves the suite's current questions.

The missed questions were:

- What happens if a trusted parent above `reports-root` is a symlink?
- What happens when two producers run in the same second?
- Does every CLI help surface say the same contract?

This is the core process lesson: for safety-sensitive filesystem work, the
review must inspect the shape of the invariant, not just the evidence count.

### 5. Approval Happened Before A Second Adversarial Review

CTO approval for rework-2 was reasonable based on the task as written, but the
task was too narrow. The later Codex + review-agent pass found issues because
it asked a broader question:

```text
Does this fully close the write-safety and operator-contract risk?
```

The answer was no. That is why rework-3 is not churn for its own sake; it is the
cost of turning a symptom patch into an invariant patch.

## Rework-3 Scope

Rework-3 should stay narrow. It should not add V3 automation, idle triggers,
cleanup hooks, cron, `--force`, `--best-effort`, or direct wiki writes.

Required fixes:

1. **Report write containment**
   - Either make `safe_write_text` check from a higher trust root, or add an
     explicit parent-chain check from the repo-owned orchestrator root down to
     `session-archive-reports`.
   - Add a regression test where a parent above `reports-root` is a symlink
     before the producer runs.
   - The producer must fail before writing outside the repo-owned tree.

2. **Atomic report filename reservation**
   - Replace check-then-write filename selection with an atomic reservation or
     no-overwrite write path.
   - Concurrent same-session same-second producers must produce distinct report
     paths or one must retry safely.
   - No producer should overwrite another producer's report.

3. **Complete CLI help wording**
   - Update the archive subcommand summary shown by `orchctl session --help`.
   - Test or manually verify both:
     - `orchctl session --help`
     - `orchctl session archive --help`

## Acceptance Bar For Closing Rework-3

The closing evidence should include:

- targeted archive-report and archive CLI tests;
- full test suite;
- a symlink-parent test above `reports_root`;
- a concurrent or atomic-reservation test for report filename collision;
- help output verification for both parent and child CLI help surfaces;
- proof that no new external child handoff YAML was created if this is handled
  by the existing team-lead session.

## Operating Lesson

For future filesystem and archive features:

- treat `base_dir` as a trust-boundary decision, not a convenience argument;
- test the full parent chain, not just the final directory;
- do not call a filename collision fix complete unless it is safe under
  concurrent writers;
- verify every CLI help surface that repeats the contract;
- run one adversarial review before CTO approval, not after it.

The phrase to remember:

```text
Do not approve the symptom. Approve the invariant.
```

## Why Rework-4 Happened

Rework-3 closed three more symptoms — containment scope from `/`, atomic
`O_EXCL` reservation, and the parent help surface. After CTO approval, Codex
was asked the broader question once more, and two latent issues were
confirmed.

The first was a containment-scope over-correction. Rework-3's
`_check_no_symlink_in_chain` walked from filesystem root down to the target
path and rejected any symlink along the way. That defended against the
specific report-write hole, but rejected legitimate workspaces. On macOS,
`/var` is itself a symlink to `/private/var`, so a repo rooted under
`/var/folders/...` (the system temp tree, the path most CI and tmp_path-style
fixtures resolve to) would fail the check before any orchestrator work
happened. The chain check was strictly stronger than the contract required:
the contract is "do not write outside the repo's own `.orchestrator` subtree",
not "every ancestor of the repo must be a real directory".

The second was a TOCTOU window in the placeholder-then-replace pattern. The
sequence was:

```text
1. _check_no_symlink_in_chain(session_dir)        # precheck
2. os.makedirs(session_dir, exist_ok=True)        # create dirs
3. os.open(target, O_CREAT|O_EXCL|O_WRONLY)       # 0-byte placeholder
4. safe_write_text(...)                           # tempfile + os.replace
```

Codex confirmed concretely that an attacker with write access to the
orchestrator tree could swap a parent directory to a symlink between step 1
and step 2. `os.makedirs` would then follow the symlink, and the 0-byte
placeholder created at step 3 would land on the symlink target — outside the
repo. Even though `safe_write_text` had its own containment check at step 4,
the escape had already happened.

The fix had to defeat the path-string race, not paper over it. The dirfd +
`O_NOFOLLOW` content-write primitive does that:

- open each component of the chain with `os.open(part, O_DIRECTORY|O_NOFOLLOW,
  dir_fd=parent_fd)`, holding fds at every level;
- write content directly with `os.open(name, O_CREAT|O_EXCL|O_WRONLY|O_NOFOLLOW,
  dir_fd=session_fd)` — no placeholder, no later rename;
- once an fd is held, the kernel resolves subsequent `openat` calls relative
  to the inode it already owns, so a path swap on disk after fd acquisition
  cannot redirect the write.

`O_NOFOLLOW` is the syscall-level guarantee. There is no userspace check that
can be raced, because the kernel rejects symlinked components atomically as
part of the `openat` call itself. That is what closed Findings 1 and 2.

## Why Rework-5 Happened

Rework-4's direction was correct: dirfd plus `O_NOFOLLOW`, content write
through the fd, no placeholder. The remaining hole was at the entry point.

The dirfd chain started with:

```python
fd = os.open(orchestrator_dir, os.O_DIRECTORY | os.O_CLOEXEC)
```

`.orchestrator` was treated as the trust boundary, opened plainly without
`O_NOFOLLOW`. The reasoning was that operators may legitimately place the
orchestrator tree behind a symlink (mounting, deploy layouts, etc.), so the
boundary itself must allow following.

Codex demonstrated that this reasoning was incomplete. If `.orchestrator` is
itself a symlink to a path outside the repo, the very first `os.open` follows
the symlink and the entire dirfd chain ends up rooted outside. Every
subsequent `_open_or_create_dir_nofollow` call on `runtime`, on
`session-archive-reports`, on `<sid>` — each of which correctly used
`O_NOFOLLOW` against its own component — operated relative to the wrong
inode. The contract is "writes go inside the repo's own `.orchestrator`
subtree". A symlinked `.orchestrator` violates the contract directly,
regardless of how each step below it behaves.

The fix split the entry into two steps:

```python
repo_fd = os.open(repo_root, os.O_DIRECTORY | os.O_CLOEXEC)
# Step A: NO O_NOFOLLOW on repo_root — above-repo symlinks (e.g., macOS /var
# -> /private/var, /home/x where /home is a symlink) must continue to succeed.
fd = _open_or_create_dir_nofollow(repo_fd, ".orchestrator")
# Step B: O_NOFOLLOW on .orchestrator — symlinked .orchestrator now fails
# atomically with ELOOP. The trust boundary is still honored above repo_root,
# but .orchestrator itself must be a real directory inside the repo.
```

This is a small change in code and a meaningful change in invariant. Above
repo_root remains operator-controlled territory (out of scope by design).
`.orchestrator` is now treated as part of the protected subtree, not as the
boundary itself, because the boundary is whatever the kernel can verify
without following a symlink.

The lesson layered on top of rework-4's lesson:

```text
A trust boundary that follows symlinks is not a trust boundary.
```

Pick the boundary where you can use `O_NOFOLLOW`, and let the path above it
be operator-controlled.

---

*This narrative is a derived view of the rework history. The authoritative
state remains code (lib/session_archive_report.py, lib/storage.py),
handoff YAML (.orchestrator/handoffs/), room YAML
(.orchestrator/rooms/), runtime session YAML (.orchestrator/runtime/sessions/),
and git history. This file is not consulted by any code path; it exists so
future readers can understand why five rework rounds were necessary, not
what the code currently looks like.*
