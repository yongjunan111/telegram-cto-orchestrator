---
name: handoff-brief-writer
description: Draft paste-ready handoff task instructions for this orchestrator repo. Use when creating CTO-to-team-lead briefs, team-lead subtask prompts, rework prompts, or adversarial review requests with scope, non-goals, invariants, acceptance criteria, evidence, and completion protocol.
argument-hint: "[brief type or task intent]"
---

# Handoff Brief Writer

Use this skill when the user wants a handoff task brief, team-lead instruction, worker/subtask prompt, rework prompt, or adversarial review prompt.

The output should be a paste-ready instruction for Claude Code. Keep it small enough to send into a tmux/Claude pane without dragging old chat context along.

## Authority Boundary

This skill drafts briefs only. It is not authoritative for:

- risk classification
- handoff approval
- handoff completion
- orchctl gate enforcement
- mutation of official orchestrator state

Official state lives in code + git + `.orchestrator` YAML and is managed by `orchctl` and code-enforced gates. The skill produces text. The text is reviewed by a human and dispatched through the proper channel. Skill output never substitutes for `orchctl` commands or for code-level checks.

## First Rule

Do not edit official orchestrator state by default. The official state files are:

- `.orchestrator/rooms/*/state.yaml`
- `.orchestrator/handoffs/*.yaml`
- `.orchestrator/runtime/sessions/*.yaml`
- `.orchestrator/peer_registry.yaml`
- `.orchestrator/active_programs.yaml`, if present

Docs, wiki, generated briefs, chat messages, and runtime evidence artifacts (e.g., adversarial-review markdown bodies) are not the source of truth unless an official YAML record commits or references them.

By default, only draft the instruction text. Mutate official state only when the user explicitly asks for it.

## Workflow

1. Pick the brief type:
   - `cto_to_team_lead`
   - `team_lead_to_subagent`
   - `rework`
   - `adversarial_review`
2. Gather the minimum needed:
   - desired outcome
   - target peer or role
   - existing handoff id, if any
   - files/modules in scope
   - must-preserve rules
   - must-not-do rules
   - acceptance criteria
   - verification commands
3. If enough is known, do not ask. Make reasonable assumptions and label them.
4. Output one paste-ready brief with locked scope, evidence requirements, and completion protocol.

Ask at most one clarifying question when the missing answer would change who owns the work or what files are safe to touch.

## Operating Rules

- Separate `must_preserve` from `must_not_do`.
- Make acceptance criteria checkable.
- Treat invariants as stronger than examples. Examples prove coverage for one case; invariants define the whole rule that must stay true.
- Include explicit non-goals.
- If this is `delegate_required`, remind CTO not to code directly.
- Delegate only when ALL of the following hold: separable concerns AND disjoint file ownership (or explicit serialization with a single owner for shared files) AND independent verification per delegated unit. If shared files are unavoidable, prefer a single owner or serialized work over parallel sub-agents.
- If a task can be done in under 5 focused minutes, do not make it `delegate_required`; draft it as a direct/non-delegate handoff instead.
- Chat reports do not close work. Official closure requires `orchctl handoff complete`.
- Wiki is memory, not authority. Code, git, and `.orchestrator` YAML are the real state.
- For rework, include a second-order risk map: how each fix could create a new bypass, stale state, collision, partial write, symlink escape, role-boundary leak, or docs/code mismatch.
- When a boundary is enforced in both reader and writer paths, require symmetry. The writer refusing bad state is not enough if the reader still accepts it.
- Do not treat skill output as automatically correct. The generated brief must be reviewed once before sending.

## delegate_required Rules

For `execution.mode == delegate_required`, include these rules:

- CTO must not implement directly.
- A non-CTO team-lead owns the handoff.
- Team-lead must not implement directly. The team-lead decomposes, delegates, reviews child/tasklet results, and completes the parent with evidence.
- Team-lead must record completed child evidence.
- Child evidence must have non-empty `owned_files` and non-empty evidence text.
- Approve requires an adversarial review artifact with `verdict: ship-as-is`.
- The adversarial reviewer must be registered and must not be the assignee or completer.
- Adversarial review must happen before approve. Do not approve first and backfill evidence later.

## Brief Self-Check

Before giving the brief to the user, check:

- Does it name the invariant, not just one example?
- Does it say what is out of scope?
- Does it include how to verify the work?
- If it is a rework, does it list second-order risks from the proposed fix?
- If it touches a read/write boundary, does it ask both sides to enforce the same rule?
- If it is `delegate_required`, does it preserve the team-lead role and require adversarial review before approve?
- Are there any unresolved placeholders like `<...>` left in the final brief? If yes, fill them in or remove them, unless the user explicitly asked for a reusable template.

Templates below intentionally contain `<...>` fill-in slots. The placeholder self-check applies to final generated briefs sent to the user, not to the reusable template definitions in this skill file.

## Completion Protocol

Use this block in implementation and rework briefs:

```text
Official completion protocol:
1. Claim the handoff with the assigned peer id.
2. Do the work inside the locked scope.
3. Run the listed verification.
4. Complete the handoff with summary, files touched, commands/tests run, evidence, risks, and unresolved items.
5. Notify CTO that the handoff is ready for review.

Chat-only "done" messages are not official closure.
```

For exact flags, run `orchctl handoff complete --help`; include coverage where relevant with `--validation-cover`, `--task-criterion-cover`, and `--room-criterion-cover`.

## CTO To Team-Lead Template

```text
You are the team-lead for this handoff. Apply the selected mode strictly. Decompose only when the mode and scope call for it, and keep the role boundary intact.

## Handoff Brief

**handoff_id:** <id or "new handoff">
**mode:** <direct | delegate_optional | delegate_required>
**kind:** <implementation | discovery>
**brief_type:** <cto_to_team_lead | team_lead_to_subagent | rework | adversarial_review>
**goal:** <one clear outcome>
**target_peer_id:** <peer id; if mode is delegate_required, this peer must exist in peer_registry.yaml and carry team-lead capability>

`kind` must match the orchctl handoff kind (only `implementation` or `discovery` are accepted by `orchctl handoff create --kind`). `brief_type` is skill/workflow metadata, not an orchctl `--kind` value.

**locked_scope:**
- <files/modules/behavior in scope>

**out_of_scope:**
- <things not to touch>

**must_preserve:**
- <invariant>
- <existing behavior that must stay true>

**invariant_vs_examples:**
- Invariant: <the rule that must hold for all cases>
- Examples that must pass: <specific cases proving the invariant>

**must_not_do:**
- <forbidden shortcut or risky behavior>
- <specific failure mode to avoid>

**acceptance_criteria:**
1. <verifiable condition>
2. <verifiable condition>

**verification:**
- <targeted command/check>
- <full or broader command/check, if needed>

**mode_specific_rules:**
Apply the rules matching the chosen `mode`:

- `direct`: implement directly. No child/tasklet evidence is required by mode alone. Adversarial review is not required by `direct` mode alone, but a deterministic high-risk trigger or explicit CTO requirement can still require review. Use when the task is small, focused, and does not warrant role separation.
- `delegate_optional`: decompose only when useful. Direct execution by the team-lead is allowed for small focused work. Child evidence is recorded only when delegation actually happens.
- `delegate_required`: CTO must not implement directly. Team-lead must not implement directly. Team-lead must delegate to child/tasklet/sub-agent work and record completed child evidence (non-empty `owned_files` list of non-empty strings + non-empty evidence text). Adversarial review artifact (`verdict: ship-as-is`) is required before approve. If the task is too small to justify child/tasklet delegation, stop and ask CTO to reclassify it as `direct` or `delegate_optional` instead.

**required_evidence:**
- child/tasklet ledger if delegated
- files touched and why
- commands/tests run with pass/fail result
- acceptance criteria coverage
- risks and unresolved items

Official completion protocol:
1. Claim the handoff with your peer id.
2. Do the work inside the locked scope.
3. Complete the handoff with summary/evidence.
4. Notify CTO with completion evidence and any review recommendation.
5. If adversarial review is required by code gate (`delegate_required`) or by current operating policy (`rework`, high-risk work, or explicit CTO gate), do not ask for approve until review evidence exists.

Do not approve your own work.
```

## Team-Lead To Sub-Agent Template

```text
You are a sub-agent executing one delegated sub-task. Stay inside this subtask. Do not edit files outside owned_files unless you stop and ask.

## Sub-Handoff

**title:** <one-line subtask>
**why:** <why this subtask exists inside the parent handoff>

**task:**
<specific imperative task>

**owned_files:**
- <file this sub-agent owns>

**out_of_scope:**
- <files/modules/behaviors not to touch>

**must_preserve:**
- <parent invariant>

**must_not_do:**
- <forbidden action>

**acceptance_criteria:**
1. <verifiable condition>

**verification:**
- <command/check>

**report_back_format:**
## Completion Report
- Summary:
- Files touched:
- Commands/tests run:
- Criterion coverage:
- Known failures:
- Risks:
- Unresolved:
```

### Team-Lead Note (after Sub-Agent Reports)

A chat/report from a sub-agent is NOT official child evidence. After the team-lead reviews and accepts a sub-agent's result, the team-lead must record official child evidence on the parent handoff via `orchctl handoff add-subtask`:

```bash
orchctl handoff add-subtask <parent-handoff-id> \
  --id <subtask-id> \
  --model-target <model-or-agent-id> \
  --owned-file <file> \
  --status completed \
  --evidence "<specific evidence: tests/checks/results>" \
  --parent-criterion "<TA/V/RA mapping>"
```

If multiple owned files apply, repeat `--owned-file <file>` for each. Empty strings or whitespace entries in `owned_files` will fail the delegate_required gate. Evidence text must be non-empty and concrete (test counts, commit refs, command outputs, etc.).

## Rework Template

```text
This is a rework, not a new feature. Fix only the listed findings and close the second-order risks created by the fix.

## Rework Brief

**target_handoff:** <handoff id>
**rework_reason:** <why the previous attempt failed>

**findings_to_close:**
1. <finding with concrete failure mode>
2. <finding with concrete failure mode>

**second_order_risk_map:**
- If fixing <A> by <approach>, check it does not create <B>.
- If adding validation, check read path and write path enforce the same boundary.
- If adding atomic writes, check partial failure, collision, symlink, stale timestamp, and retry behavior.
- If changing help/docs, check every user-facing command description matches the real contract.
- If adding one example test, name the broader invariant that example is proving.
- If changing approval or completion gates, check role bypass, stale evidence, forged evidence, and backfilled evidence paths.

Note: this skill drafts the risk map for human review. Code-level rework gates and schema enforcement (when implemented) are the official authority. The skill itself is not the gate; it only helps surface what to ask.

**locked_scope:**
- <files/modules allowed>

**must_not_do:**
- No unrelated refactors.
- No new automation/hook/cron unless explicitly requested.
- No staging, commit, or push unless CTO asks.

**verification:**
- Add or update regression tests for every finding.
- Run targeted tests first.
- Run broader tests if the change touches shared behavior.

**completion_evidence:**
- finding-by-finding fix summary
- second-order risks checked
- reader/writer symmetry checked, if applicable
- invariant coverage, not just example coverage
- tests run
- remaining risk, if any
```

## Adversarial Review Template

```text
You are the adversarial reviewer. Do not summarize the implementation first. Try to break the approval claim.

## Review Target

**handoff_id:** <handoff id>
**completed_at:** <timestamp from handoff>
**claimed outcome:** <what the team says is done>

**review_focus:**
- Are the locked invariants actually closed?
- Are examples being mistaken for invariants?
- Are there bypass paths around the gate?
- Did the fix create a new read/write mismatch?
- Are stale, partial, symlinked, collision, and wrong-role artifacts rejected where relevant?
- Was adversarial review produced before approve, not backfilled after approval?
- Do docs/help say the same thing the code enforces?

**output_format:**
## Review Findings
- Finding 1: <priority> <file/function if known> <failure mode> <why it matters>

If no issues:
Return `verdict: ship-as-is`, `findings_count: 0`, and a short note listing the invariants checked.

Do not approve. Only produce review evidence.
```

### Persisting the Review (CTO step)

The reviewer's output is NOT official until persisted through the official helper. Do not hand-write files under `.orchestrator/runtime/adversarial-reviews/`. Do not mutate handoff YAML directly. Do not approve.

CTO writes the artifact via:

```bash
orchctl handoff add-adversarial-review <handoff-id> \
  --reviewer <peer-id> \
  --verdict <ship-as-is | needs-fix | reject> \
  --findings-count <actual non-negative finding count> \
  --body-file <review-body.md>
```

Use `ship-as-is` only when the adversarial review found no blocking findings and `findings_count` is `0`. If findings exist, persist the real verdict (`needs-fix` or `reject`) and the actual count; do not convert review output into approval evidence by hard-coding pass values.

For approval to succeed, the current code-enforced gate requires (mention these in the brief when relevant):

- `handoff_id` in frontmatter matches the handoff being approved
- `verdict` is exactly `ship-as-is` (anything else blocks approve)
- `review_target_completed_at` matches the handoff's `timestamps.completed_at`
- artifact filesystem mtime is strictly greater than the handoff's `timestamps.completed_at`; stale or equal mtime fails the gate
- `reviewer` is a registered peer in `peer_registry.yaml` and is neither the assignee nor the completer
- artifact is produced before `orchctl handoff approve`, not backfilled after approval

## Final Output Style

Output the brief first. Put assumptions at the bottom. Do not bury the task in commentary.
