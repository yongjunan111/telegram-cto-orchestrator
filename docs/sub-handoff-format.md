# Sub-Handoff Format Specification

This document defines the structured format a team lead worker uses when delegating sub-tasks to sub-agents via Claude Code's Agent tool.

---

## When to Delegate (Fan-out Rules)

**Default is direct execution.** Sub-agent delegation is the exception, not the rule.

**Delegate ONLY when ALL of these hold:**
- The task involves 2 or more distinct concerns
- Those concerns are disjoint — no shared file edits between sub-tasks
- Each sub-task can be verified independently

**Do NOT delegate when:**
- The task is a single focused change
- Sub-tasks would need to coordinate on shared files
- There are sequential dependencies between steps (each step must see the prior step's output)

**Granularity check:**
- Too big: touches 3+ unrelated modules, has 5+ acceptance criteria, mixes code changes with infra changes
- Too small: single function rename, trivial formatting, anything faster to do directly than to write the sub-handoff
- Rule of thumb: if you can complete it in under 5 minutes of focused work, do it directly — do not create a sub-handoff

---

## Field Reference

All fields are required unless marked optional.

| Field | Purpose |
|---|---|
| `title` | One-line name for the sub-task |
| `why` | Why this sub-task exists in context of the parent handoff (1-2 sentences) |
| `task` | What to do — imperative, specific, unambiguous |
| `scope` | Files, directories, or modules in play |
| `out_of_scope` | What NOT to touch or change |
| `owned_files` | (optional) Files this sub-agent exclusively owns — other sub-agents must not edit these |
| `constraints` | Technical or process constraints that apply |
| `must_preserve` | Invariants and non-goals inherited from the parent handoff that must not be violated |
| `must_not_do` | Failure examples and things explicitly forbidden |
| `acceptance_criteria` | Numbered list of verifiable conditions defining "done" |
| `covers_parent_criteria` | (optional) Which parent indices this sub-task covers, using prefixes: `TA` (task acceptance), `RA` (room acceptance), `V` (validation) |
| `verification` | How the sub-agent should verify its own work before reporting back |
| `deliverables` | Concrete outputs expected |
| `escalate_if` | Conditions to stop and ask the team lead for guidance rather than proceeding |
| `report_back_format` | Exact structure of the completion report |

---

## Template

Copy this block into the Agent tool's `prompt` parameter. Replace all `<placeholder>` values.

```
You are a sub-agent executing a delegated sub-task. Follow this specification exactly.

---

## Sub-Handoff

**title:** <one-line name>

**why:** <1-2 sentences explaining why this sub-task exists relative to the parent handoff>

**task:**
<Imperative description of what to do. Be specific. Reference exact file paths, function names, or behaviors where known.>

**scope:**
- <file or directory 1>
- <file or directory 2>

**out_of_scope:**
- <file, module, or concern you must not touch>
- <another thing to leave alone>

**owned_files:** (if applicable)
- <file exclusively owned by this sub-agent — other sub-agents must not edit this>

**constraints:**
- <technical or process constraint>
- <another constraint>

**must_preserve:** (from parent handoff — do not violate)
- <invariant 1>
- <invariant 2>

**must_not_do:** (from parent handoff — explicitly forbidden)
- <forbidden action or pattern>
- <another forbidden action>

**acceptance_criteria:**
1. <verifiable condition>
2. <verifiable condition>
3. <verifiable condition>

**covers_parent_criteria:** (if applicable)
Parent validation indices: <e.g., V1, V3>
Parent task acceptance indices: <e.g., TA2, TA4>
Parent room acceptance indices: <e.g., RA1>

**verification:**
<How to verify your own work. Include specific commands to run, outputs to check, or behaviors to confirm.>

**deliverables:**
- <concrete output 1>
- <concrete output 2>

**escalate_if:**
- <condition that means you should stop and report back rather than proceed>
- <another blocking condition>

**report_back_format:**
Use the exact structure below. Do not summarize or paraphrase — fill in each section.

## Completion Report
- **Summary:** 1-2 sentences
- **Files touched:** list with one-line description each
- **Commands run:** actual commands executed for verification
- **Tests run:** test results (pass/fail counts)
- **Criterion coverage:** which acceptance criteria met, with evidence
- **Known failures:** anything that didn't work
- **Risks:** anything that might break downstream
- **Unresolved:** questions not addressed
- **Recommended next:** what should happen after this

---

Begin execution now.
```

---

## Report Formats

### Sub-agent to Team Lead

```
## Completion Report
- **Summary:** 1-2 sentences
- **Files touched:** list with one-line description each
- **Commands run:** actual commands executed for verification
- **Tests run:** test results (pass/fail counts)
- **Criterion coverage:** which acceptance criteria met, with evidence
- **Known failures:** anything that didn't work
- **Risks:** anything that might break downstream
- **Unresolved:** questions not addressed
- **Recommended next:** what should happen after this
```

### Team Lead to CTO

The team lead synthesizes sub-agent reports into a single handoff completion report. Drop noise, elevate signal. The CTO should be able to make an accept/rework decision from this report alone. Never forward raw sub-agent output.

```
## Handoff Completion Report
- **Summary:** 1-3 sentences
- **Subtask ledger:** table of each sub-task (title | outcome: accepted/reworked/escalated | attempts)
- **Evidence:** key verification results across all sub-tasks
- **Parent criterion coverage:** how sub-task evidence maps to parent items (TA=task acceptance, RA=room acceptance, V=validation)
- **Risks:** aggregated risks needing CTO attention
- **Unresolved:** aggregated unresolved items
- **Recommendation:** what should happen next
- *Note: This is an internal QA report. Official handoff review is pending official CTO/reviewer decision.*
```

**Curation guidance for team lead:**
- Drop routine file lists and passing tests that were expected to pass
- Elevate failures, unexpected risks, design questions, and scope issues
- Map sub-task evidence back to specific parent criteria by index
- If a risk is speculative, say so — do not inflate

---

## Terminology

| Term | Meaning |
|---|---|
| **accept** | Team lead accepts sub-agent results internally (NOT the same as approve) |
| **rework** | Team lead sends sub-task back to sub-agent for corrections |
| **escalate** | Sub-agent or team lead bumps a blocking issue to the CTO |
| **approve** | RESERVED for CTO/reviewer formal review only — team lead never approves |

---

## Kind-Sensitivity

Sub-handoff delegation differs depending on the parent handoff kind.

### `implementation` handoffs

Sub-agents write code, tests, and documentation. Deliverables are file changes and passing verification commands. Acceptance criteria are concrete and checkable (e.g., "running `npm test` passes", "endpoint returns 200 with expected body").

### `discovery` handoffs

Sub-agents research and report findings — they do NOT write production code. Deliverables are written analysis, option comparisons, evidence, and uncertainty statements. Acceptance criteria are about completeness of investigation, not code correctness. A discovery sub-agent that writes code has violated scope.

---

## Anti-Patterns

- **Vague task:** "improve the code" — no sub-agent can verify completion
- **Missing acceptance criteria:** sub-agent has no definition of done
- **No scope boundaries:** sub-agent wanders into files outside the task
- **Free-text delegation:** not using this structured format
- **Copying parent handoff verbatim:** decompose, don't forward
- **Delegating sequential steps:** if step B must see step A's output, do not run them as parallel sub-agents

---

## Worked Example

### Scenario

Parent handoff: Fix a login redirect bug where authenticated users land on `/` instead of `/dashboard` after sign-in.

---

```
You are a sub-agent executing a delegated sub-task. Follow this specification exactly.

---

## Sub-Handoff

**title:** Fix post-login redirect destination in auth callback handler

**why:** The parent handoff reports that authenticated users land on `/` instead of
`/dashboard` after sign-in. The redirect logic lives in the auth callback handler,
which this sub-task addresses directly.

**task:**
In `src/auth/callback.ts`, locate the `handleOAuthCallback` function. After the
session is written, change the redirect destination from `"/"` to `"/dashboard"`.
Ensure the redirect only fires when the session write succeeds — do not redirect
on error paths.

**scope:**
- `src/auth/callback.ts`
- `src/auth/__tests__/callback.test.ts`

**out_of_scope:**
- `src/auth/middleware.ts` — do not touch rate limiting or session validation logic
- `src/routes/` — do not modify route definitions
- Any UI components

**owned_files:**
- `src/auth/callback.ts`
- `src/auth/__tests__/callback.test.ts`

**constraints:**
- Do not change the function signature of `handleOAuthCallback`
- Do not introduce new dependencies
- Test file must use the existing mock setup — do not refactor test infrastructure

**must_preserve:** (from parent handoff)
- Existing error handling paths must remain unchanged
- Session write behavior must not be altered

**must_not_do:** (from parent handoff)
- Do not change redirect behavior on failed authentication
- Do not touch any file outside the listed scope

**acceptance_criteria:**
1. `handleOAuthCallback` redirects to `/dashboard` on successful session write
2. `handleOAuthCallback` does not redirect to `/dashboard` on session write failure (error path unchanged)
3. `npm test -- --testPathPattern=callback` passes with no failures

**covers_parent_criteria:**
Parent task acceptance indices: TA1, TA2

**verification:**
Run `npm test -- --testPathPattern=callback` and confirm all tests pass.
Manually read the redirect line in `handleOAuthCallback` and confirm it reads `/dashboard`.

**deliverables:**
- Updated `src/auth/callback.ts` with corrected redirect
- Updated or added test cases in `src/auth/__tests__/callback.test.ts` covering success and failure paths

**escalate_if:**
- The redirect destination is determined dynamically (e.g., from a config value or database) and changing the string literal would be wrong
- The success/failure paths share a single redirect call making a clean fix impossible without restructuring
- Tests are failing before your change for unrelated reasons

**report_back_format:**
Use the exact structure below.

## Completion Report
- **Summary:** 1-2 sentences
- **Files touched:** list with one-line description each
- **Commands run:** actual commands executed for verification
- **Tests run:** test results (pass/fail counts)
- **Criterion coverage:** which acceptance criteria met, with evidence
- **Known failures:** anything that didn't work
- **Risks:** anything that might break downstream
- **Unresolved:** questions not addressed
- **Recommended next:** what should happen after this

---

Begin execution now.
```

---

### Expected sub-agent completion report

```
## Completion Report
- **Summary:** Changed redirect destination from `/` to `/dashboard` in the success
  path of `handleOAuthCallback`. Added a test case confirming the error path still
  redirects to `/`.
- **Files touched:**
  - `src/auth/callback.ts` — changed redirect string on line 47 from `"/"` to `"/dashboard"`
  - `src/auth/__tests__/callback.test.ts` — added test: "redirects to /dashboard on success"; updated existing test: "does not redirect to /dashboard on failure"
- **Commands run:**
  - `npm test -- --testPathPattern=callback`
- **Tests run:** 6 passed, 0 failed
- **Criterion coverage:**
  - A1: confirmed — success path redirects to `/dashboard` (line 47)
  - A2: confirmed — failure path redirects to `/` (line 61, unchanged)
  - A3: confirmed — `npm test -- --testPathPattern=callback` output: 6 passed
- **Known failures:** none
- **Risks:** none identified — change is a single string replacement on an isolated path
- **Unresolved:** none
- **Recommended next:** team lead can accept; parent handoff can proceed to CTO review
```

---

### Expected team lead to CTO report

```
## Handoff Completion Report
- **Summary:** The post-login redirect bug is fixed. The auth callback handler now
  sends authenticated users to `/dashboard`. One sub-task was required; it was
  accepted on first attempt.
- **Subtask ledger:**
  | Title | Outcome | Attempts |
  |---|---|---|
  | Fix post-login redirect destination in auth callback handler | accepted | 1 |
- **Evidence:**
  - `npm test -- --testPathPattern=callback`: 6 passed, 0 failed
  - Success path confirmed to redirect to `/dashboard` (src/auth/callback.ts:47)
  - Failure path confirmed unchanged, still redirects to `/`
- **Parent criterion coverage:**
  - TA1 (success redirect to /dashboard): covered by sub-task evidence
  - TA2 (failure path unchanged): covered by sub-task evidence
- **Risks:** none
- **Unresolved:** none
- **Recommendation:** ready for CTO review
- *Note: This is an internal QA report. Official handoff review is pending official CTO/reviewer decision.*
```
