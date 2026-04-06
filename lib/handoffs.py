"""Handoff command logic."""
import os
import sys

from . import storage
from .validators import validate_slug, require_room, require_handoff, require_peer


def scan_room_handoffs(room_id: str):
    """Scan handoff files and return handoffs for a given room plus parse errors.

    Returns (matching_handoffs, parse_error_filenames).
    matching_handoffs: list of handoff state dicts where handoff.room_id matches.
    parse_error_filenames: list of filenames that could not be parsed.
    """
    if not os.path.isdir(storage.HANDOFFS_DIR):
        return [], []

    results = []
    errors = []
    for fname in sorted(os.listdir(storage.HANDOFFS_DIR)):
        if not fname.endswith(".yaml") or fname == ".gitkeep":
            continue
        path = os.path.join(storage.HANDOFFS_DIR, fname)
        try:
            state = storage.read_state(path)
            h = state.get("handoff", {})
            if h.get("room_id") == room_id:
                results.append(state)
        except Exception:
            errors.append(fname[:-5])  # strip .yaml

    return results, errors


def _derive_review_state(handoff_state: dict) -> str:
    """Derive review state from handoff status and review outcome.

    Returns: 'n/a', 'pending_review', 'approved', or 'changes_requested'.
    """
    status = handoff_state.get("handoff", {}).get("status", "")
    if status != "completed":
        return "n/a"
    outcome = handoff_state.get("review", {}).get("outcome")
    if not outcome:
        return "pending_review"
    return outcome


def cmd_handoff_create(args):
    handoff_id = args.handoff_id
    room_id = args.room
    to = args.to
    task_desc = args.task
    priority = args.priority
    scope = args.scope or ""
    report_back = args.report_back or ""

    validate_slug(handoff_id, "handoff_id")
    require_room(room_id)
    require_peer(to)

    dest = storage.handoff_path(handoff_id)
    if os.path.exists(dest):
        print(f"Error: handoff '{handoff_id}' already exists.", file=sys.stderr)
        sys.exit(1)

    os.makedirs(storage.HANDOFFS_DIR, exist_ok=True)

    now = storage.now_iso()
    state = {
        "handoff": {
            "id": handoff_id,
            "room_id": room_id,
            "program_id": None,
            "from": "orchestrator",
            "to": to,
            "status": "open",
            "priority": priority,
        },
        "task": {
            "description": task_desc,
            "scope": scope,
            "constraints": args.constraints or [],
            "acceptance_criteria": args.acceptance_criteria or [],
            "report_back": report_back,
            "non_goals": args.non_goals or [],
            "invariants": args.invariants or [],
            "failure_examples": args.failure_examples or [],
            "validation": args.validation or [],
        },
        "timestamps": {
            "created_at": now,
            "claimed_at": None,
            "completed_at": None,
        },
    }
    storage.write_state(dest, state)

    # Append handoff creation to room log
    log_entry = (
        f"\n## {now} — orchestrator\n"
        f"- Handoff `{handoff_id}` created -> {to}\n"
        f"- Task: {task_desc}\n"
    )
    storage.append_log(storage.room_log_path(room_id), log_entry)

    # Update room's updated_at
    storage.update_state(storage.room_state_path(room_id), {"room.updated_at": now})

    print(f"Handoff '{handoff_id}' created.")
    print(f"  room:     {room_id}")
    print(f"  to:       {to}")
    print(f"  priority: {priority}")
    print(f"  task:     {task_desc[:72]}")


def cmd_handoff_list(args):
    room_id = getattr(args, "room", None)

    if room_id is not None:
        require_room(room_id)

    if not os.path.isdir(storage.HANDOFFS_DIR):
        if room_id is not None:
            print(f"No handoffs found for room '{room_id}'.")
        else:
            print("No handoffs found.")
        return

    files = sorted(
        f for f in os.listdir(storage.HANDOFFS_DIR)
        if f.endswith(".yaml") and f != ".gitkeep"
    )

    if not files:
        if room_id is not None:
            print(f"No handoffs found for room '{room_id}'.")
        else:
            print("No handoffs found.")
        return

    fmt = "{:<24} {:<16} {:<16} {:<10} {:<10} {:<18}"
    header_printed = False
    matched = 0
    parse_errors = 0
    parse_error_files = []

    for fname in files:
        path = os.path.join(storage.HANDOFFS_DIR, fname)
        try:
            state = storage.read_state(path)
            h = state.get("handoff", {})
            if room_id is not None and h.get("room_id") != room_id:
                continue
            if not header_printed:
                print(fmt.format("ID", "ROOM", "TO", "STATUS", "PRIORITY", "REVIEW"))
                print("-" * 96)
                header_printed = True
            hid = str(h.get("id") or fname[:-5])[:23]
            room = str(h.get("room_id") or "")[:15]
            to = str(h.get("to") or "")[:15]
            status = str(h.get("status") or "")[:9]
            priority = str(h.get("priority") or "")[:9]
            review_state = _derive_review_state(state)
            print(fmt.format(hid, room, to, status, priority, review_state))
            matched += 1
        except Exception:
            parse_errors += 1
            parse_error_files.append(fname[:-5])
            if room_id is None:
                # Unfiltered: show inline as before
                if not header_printed:
                    print(fmt.format("ID", "ROOM", "TO", "STATUS", "PRIORITY", "REVIEW"))
                    print("-" * 96)
                    header_printed = True
                print(fmt.format(fname[:-5], "(parse error)", "-", "-", "-", "-"))

    # Post-loop output
    if room_id is not None:
        if matched == 0 and parse_errors == 0:
            print(f"No handoffs found for room '{room_id}'.")
        elif matched == 0 and parse_errors > 0:
            print(
                f"No parseable handoffs found for room '{room_id}'. "
                f"{parse_errors} file(s) could not be parsed: {', '.join(parse_error_files)}"
            )
        elif matched > 0 and parse_errors > 0:
            print(
                f"\nWarning: {parse_errors} handoff file(s) could not be parsed "
                f"and were excluded: {', '.join(parse_error_files)}"
            )


def cmd_handoff_show(args):
    handoff_id = args.handoff_id
    require_handoff(handoff_id)
    path = storage.handoff_path(handoff_id)
    with open(path, "r") as f:
        print(f.read(), end="")


def _load_handoff_with_room(handoff_id: str):
    """Load and validate a handoff and its associated room state.

    Returns (handoff_state, room_state) or exits with error.
    """
    require_handoff(handoff_id)
    path = storage.handoff_path(handoff_id)

    try:
        handoff_state = storage.read_state(path)
        if not isinstance(handoff_state, dict) or "handoff" not in handoff_state:
            raise ValueError("missing 'handoff' section")
    except Exception as e:
        print(
            f"Error: handoff '{handoff_id}' is malformed: {e}",
            file=sys.stderr,
        )
        sys.exit(1)

    room_id = handoff_state.get("handoff", {}).get("room_id")
    if not room_id:
        print(f"Error: handoff '{handoff_id}' has no room_id.", file=sys.stderr)
        sys.exit(1)

    room_state_path = storage.room_state_path(room_id)
    if not os.path.isfile(room_state_path):
        print(
            f"Error: room '{room_id}' referenced by handoff '{handoff_id}' does not exist.",
            file=sys.stderr,
        )
        sys.exit(1)

    try:
        room_state = storage.read_state(room_state_path)
        if not isinstance(room_state, dict) or "room" not in room_state:
            raise ValueError("missing 'room' section")
    except Exception as e:
        print(
            f"Error: room '{room_id}' state is malformed: {e}",
            file=sys.stderr,
        )
        sys.exit(1)

    return handoff_state, room_state


# ---------------------------------------------------------------------------
# brief
# ---------------------------------------------------------------------------

def _field(value) -> str:
    """Return value as string, or 'Not specified' if empty/None."""
    if value is None or value == "":
        return "Not specified"
    return str(value)


def _bullet_list(items) -> str:
    """Render a list as markdown bullets, or 'None specified' if empty/None."""
    if not items:
        return "None specified"
    return "\n".join(f"- {item}" for item in items)


def _build_verification(task_criteria, room_criteria, task_validation=None) -> str:
    lines = ["When reporting completion, provide:\n"]

    has_specific = False

    if task_validation:
        has_specific = True
        lines.append("**Validation steps defined by contract:**")
        for v in task_validation:
            lines.append(f"- [ ] {v}")
        lines.append("")

    if task_criteria:
        has_specific = True
        lines.append("**Task acceptance criteria to verify:**")
        for c in task_criteria:
            lines.append(f"- [ ] {c}")
        lines.append("")

    if room_criteria:
        has_specific = True
        lines.append("**Room acceptance criteria to verify:**")
        for c in room_criteria:
            lines.append(f"- [ ] {c}")
        lines.append("")

    if not has_specific:
        lines.append("No acceptance criteria pre-defined. Provide:")
        lines.append("- Evidence of task completion with specific details")
        lines.append("- Explanation of approach taken")
        lines.append("")

    # Always include these baseline items
    lines.append("**In all cases, also report:**")
    lines.append("- List of files created or modified")
    lines.append("- Any risks, edge cases, or deferred items")

    return "\n".join(lines)


def _render_brief(handoff_state: dict, room_state: dict) -> str:
    h = handoff_state.get("handoff", {})
    task = handoff_state.get("task", {})
    room = room_state.get("room", {})
    context = room_state.get("context", {})
    lifecycle = room_state.get("lifecycle", {})
    rework_data = handoff_state.get("rework", {})

    handoff_id = _field(h.get("id"))
    room_id = _field(h.get("room_id"))
    assigned_to = _field(h.get("to"))
    handoff_status = _field(h.get("status"))
    priority = _field(h.get("priority"))

    goal = _field(context.get("goal"))
    room_status = _field(room.get("status"))
    phase = _field(lifecycle.get("current_phase"))
    next_action = _field(lifecycle.get("next_action"))
    blocked_by = _field(lifecycle.get("blocked_by"))

    request_summary = _field(context.get("request_summary"))
    current_summary = _field(context.get("current_summary"))
    open_questions = _bullet_list(context.get("open_questions"))
    blocker_summary = _field(lifecycle.get("blocker_summary"))

    room_constraints = _bullet_list(context.get("constraints"))
    room_acceptance_criteria = _bullet_list(context.get("acceptance_criteria"))

    description = _field(task.get("description"))
    scope = _field(task.get("scope"))
    task_constraints = _bullet_list(task.get("constraints"))
    task_acceptance_criteria = _bullet_list(task.get("acceptance_criteria"))
    report_back = _field(task.get("report_back"))
    non_goals = _bullet_list(task.get("non_goals"))
    invariants = _bullet_list(task.get("invariants"))
    failure_examples = _bullet_list(task.get("failure_examples"))
    validation_checklist = _bullet_list(task.get("validation"))

    verification = _build_verification(
        task.get("acceptance_criteria") or [],
        context.get("acceptance_criteria") or [],
        task.get("validation") or [],
    )

    # Rework Delta (only for rework handoffs)
    if rework_data:
        rework_of = h.get("rework_of", "unknown")
        rework_note = _field(rework_data.get("review_note"))
        rework_must = rework_data.get("must_address") or []
        if rework_must:
            must_text = "\n".join(f"- {item}" for item in rework_must)
        else:
            must_text = "No structured must-address items recorded in source review"

        rework_delta_section = f"""
## Rework Delta
- **Rework of:** {rework_of}
- **Review note:** {rework_note}

### Must Address
{must_text}
"""
    else:
        rework_delta_section = ""

    return f"""\
# Execution Brief: {handoff_id}

## Assignment
- **Handoff:** {handoff_id}
- **Room:** {room_id}
- **Assigned to:** {assigned_to}
- **Status:** {handoff_status}
- **Priority:** {priority}
{rework_delta_section}
## Room Context
- **Goal:** {goal}
- **Room status:** {room_status}
- **Phase:** {phase}
- **Next action:** {next_action}
- **Blocked by:** {blocked_by}

## Room Memory
- **Request summary:** {request_summary}
- **Current summary:** {current_summary}
- **Open questions:**
{open_questions}
- **Blocker summary:** {blocker_summary}

## Room-Level Contract
### Constraints
{room_constraints}

### Acceptance Criteria
{room_acceptance_criteria}

## Task
{description}

### Scope
{scope}

### Task-Level Positive Spec
#### Constraints
{task_constraints}

#### Acceptance Criteria
{task_acceptance_criteria}

### Task Contract (Negative Boundaries)
#### Non-Goals
{non_goals}

#### Invariants
{invariants}

#### Failure Examples
{failure_examples}

#### Validation Checklist
{validation_checklist}

## Reporting
{report_back}

## Verification Expectations
{verification}

---
*This brief is a derived view of handoff and room state. It is not authoritative — the source of truth is the handoff YAML and room state.yaml.*"""


def cmd_handoff_brief(args):
    handoff_id = args.handoff_id
    handoff_state, room_state = _load_handoff_with_room(handoff_id)
    print(_render_brief(handoff_state, room_state))


# ---------------------------------------------------------------------------
# room-memory suggestion
# ---------------------------------------------------------------------------

def cmd_handoff_room_memory(args):
    handoff_id = args.handoff_id
    handoff_state, room_state = _load_handoff_with_room(handoff_id)

    h = handoff_state.get("handoff", {})
    task = handoff_state.get("task", {})
    resolution = handoff_state.get("resolution", {})

    status = h.get("status", "")
    review_state = _derive_review_state(handoff_state)

    if status == "blocked":
        # Blocker suggestions — unchanged
        suggestions = _build_room_memory_suggestions(status, h, task, resolution, room_state)
        output = _render_room_memory_suggestions(handoff_id, h.get("room_id", ""), status, suggestions)
        print(output)
    elif status == "completed" and review_state == "approved":
        # Success-path suggestions — only after approval
        suggestions = _build_room_memory_suggestions(status, h, task, resolution, room_state)
        output = _render_room_memory_suggestions(handoff_id, h.get("room_id", ""), status, suggestions)
        print(output)
    elif status == "completed" and review_state == "pending_review":
        print(f"Handoff '{handoff_id}' is completed but not yet reviewed.")
        print(f"Completion-derived room memory updates are withheld until approval.")
        print(f"Run 'orchctl handoff review {handoff_id}' to inspect, then approve or request changes.")
    elif status == "completed" and review_state == "changes_requested":
        print(f"Handoff '{handoff_id}' has review outcome: changes_requested.")
        print(f"Completion result has not been accepted into room broad context.")
        print(f"A rework handoff should address the review feedback before room memory is updated.")
    else:
        print(
            f"Error: Handoff '{handoff_id}' is in '{status}' state. "
            f"Room memory suggestions are only available for 'blocked' or 'completed' handoffs.",
            file=sys.stderr,
        )
        sys.exit(1)


def _build_room_memory_suggestions(status, h, task, resolution, room_state):
    """Build conservative room memory suggestions from handoff terminal state."""
    suggestions = {}

    if status == "blocked":
        blocked_reason = resolution.get("blocked_reason", "")
        blocked_by = resolution.get("blocked_by") or h.get("to", "")

        if blocked_reason:
            suggestions["blocker_summary"] = blocked_reason
        if blocked_by:
            suggestions["blocked_by"] = blocked_by

        # Conservative current_summary only if there's a reason
        task_desc = task.get("description", "")
        if blocked_reason and task_desc:
            suggestions["current_summary"] = f"Blocked during: {task_desc[:80]}"

    elif status == "completed":
        summary = resolution.get("summary", "")

        if summary:
            suggestions["current_summary"] = summary

        # Clear blocker fields if room currently has them
        room_lifecycle = room_state.get("lifecycle", {})
        if room_lifecycle.get("blocker_summary") or room_lifecycle.get("blocked_by"):
            suggestions["clear_blocker"] = True

        # Don't fabricate next_action — only suggest clearing blocker

    return suggestions


def _render_room_memory_suggestions(handoff_id, room_id, status, suggestions):
    """Render suggestions as readable output with ready-to-run command."""
    lines = [
        f"# Room Memory Suggestions",
        f"",
        f"**Source:** handoff `{handoff_id}` ({status})",
        f"**Target room:** `{room_id}`",
        f"",
    ]

    if not suggestions:
        lines.append("No room memory updates suggested — insufficient data in handoff result.")
        lines.append("")
        lines.append("---")
        lines.append("*This is a read-only suggestion. No state has been modified.*")
        return "\n".join(lines)

    # Suggested changes section
    lines.append("## Suggested Updates")
    lines.append("")

    if "current_summary" in suggestions:
        lines.append(f"- **current_summary:** {suggestions['current_summary']}")
    if "blocker_summary" in suggestions:
        lines.append(f"- **blocker_summary:** {suggestions['blocker_summary']}")
    if "blocked_by" in suggestions:
        lines.append(f"- **blocked_by:** {suggestions['blocked_by']}")
    if suggestions.get("clear_blocker"):
        lines.append(f"- **clear blocker:** remove blocker_summary and blocked_by")
    # next_action: only if explicitly present (we don't fabricate)
    if "next_action" in suggestions:
        lines.append(f"- **next_action:** {suggestions['next_action']}")

    lines.append("")

    # Ready-to-run command
    lines.append("## Ready-to-Run Command")
    lines.append("")
    lines.append("Review the suggestion above, then apply with:")
    lines.append("")

    cmd_parts = [f".venv/bin/python orchctl room memory {room_id}"]

    if "current_summary" in suggestions:
        escaped = suggestions["current_summary"].replace('"', '\\"')
        cmd_parts.append(f'  --current-summary "{escaped}"')
    if "blocker_summary" in suggestions:
        escaped = suggestions["blocker_summary"].replace('"', '\\"')
        cmd_parts.append(f'  --blocker-summary "{escaped}"')
    if "blocked_by" in suggestions:
        escaped = suggestions["blocked_by"].replace('"', '\\"')
        cmd_parts.append(f'  --blocked-by "{escaped}"')
    if suggestions.get("clear_blocker"):
        cmd_parts.append(f'  --clear-blocker')

    lines.append("```bash")
    lines.append(" \\\n".join(cmd_parts))
    lines.append("```")
    lines.append("")
    lines.append("---")
    lines.append("*Review before applying. This is a suggestion, not an automatic update. No state has been modified.*")

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# review
# ---------------------------------------------------------------------------

def _build_review_signals(task, resolution, room_context, room_lifecycle):
    """Build review signals from evidence and criteria."""
    signals = []

    files = resolution.get("files_changed") or []
    verifications = resolution.get("verification") or []
    risks = resolution.get("risks") or []
    task_criteria = task.get("acceptance_criteria") or []
    room_criteria = room_context.get("acceptance_criteria") or []
    has_criteria = bool(task_criteria or room_criteria)

    # Missing verification
    if not verifications:
        if has_criteria:
            signals.append(("WARNING", "No verification steps recorded, but acceptance criteria are defined"))
        else:
            signals.append(("WARNING", "No verification steps recorded"))

    # Missing files
    if not files:
        signals.append(("NOTE", "No files_changed recorded"))

    # Risks present
    if risks:
        signals.append(("WARNING", f"{len(risks)} risk(s) reported by worker"))

    # No acceptance criteria at all
    if not has_criteria:
        signals.append(("NOTE", "No acceptance criteria defined (room or task level)"))

    # Room blocker still set
    if room_lifecycle.get("blocker_summary") or room_lifecycle.get("blocked_by"):
        signals.append(("NOTE", "Room still has blocker context set"))

    # --- Contract-aware signals ---

    task_validation = task.get("validation") or []
    invariants = task.get("invariants") or []
    non_goals = task.get("non_goals") or []
    failure_examples = task.get("failure_examples") or []

    # Validation coverage signals
    task_validation_steps = task.get("validation") or []
    stored_coverage = resolution.get("validation_coverage") or []

    if task_validation_steps:
        covered_indices = set(c.get("validation_index") for c in stored_coverage)
        uncovered_count = sum(1 for i in range(1, len(task_validation_steps) + 1) if i not in covered_indices)

        if not stored_coverage:
            signals.append(("WARNING", f"Validation contract defines {len(task_validation_steps)} step(s) but no validation coverage was recorded"))
        elif uncovered_count > 0:
            signals.append(("WARNING", f"{uncovered_count} of {len(task_validation_steps)} validation step(s) remain uncovered"))
        else:
            signals.append(("NOTE", f"All {len(task_validation_steps)} validation step(s) have explicit coverage recorded — reviewer should verify adequacy"))

    # Invariants
    if invariants:
        signals.append(("NOTE", f"{len(invariants)} invariant(s) defined — reviewer should verify these were preserved"))

    # Non-goals
    if non_goals:
        signals.append(("NOTE", f"{len(non_goals)} non-goal(s) defined — reviewer should confirm no scope creep or forbidden changes"))

    # Failure examples
    if failure_examples:
        signals.append(("NOTE", f"{len(failure_examples)} failure example(s) defined — reviewer should confirm these failure modes were avoided"))

    if not signals:
        signals.append(("OK", "Evidence appears complete — no warnings"))

    return signals


def _render_review(handoff_state, room_state):
    """Render a structured review packet."""
    h = handoff_state.get("handoff", {})
    task = handoff_state.get("task", {})
    resolution = handoff_state.get("resolution", {})
    timestamps = handoff_state.get("timestamps", {})
    room = room_state.get("room", {})
    context = room_state.get("context", {})
    lifecycle = room_state.get("lifecycle", {})

    handoff_id = _field(h.get("id"))
    room_id = _field(h.get("room_id"))
    assigned_to = _field(h.get("to"))
    status = _field(h.get("status"))
    completed_by = _field(resolution.get("completed_by"))
    completed_at = _field(timestamps.get("completed_at"))

    # Task context
    task_desc = _field(task.get("description"))
    scope = _field(task.get("scope"))
    room_goal = _field(context.get("goal"))
    room_phase = _field(lifecycle.get("current_phase"))

    # Constraints and acceptance criteria
    room_constraints = _bullet_list(context.get("constraints"))
    task_constraints = _bullet_list(task.get("constraints"))
    room_criteria = _bullet_list(context.get("acceptance_criteria"))
    task_criteria = _bullet_list(task.get("acceptance_criteria"))

    # Task contract fields
    non_goals = _bullet_list(task.get("non_goals"))
    invariants = _bullet_list(task.get("invariants"))
    failure_examples = _bullet_list(task.get("failure_examples"))
    validation_list = _bullet_list(task.get("validation"))

    # Evidence
    summary = _field(resolution.get("summary"))
    files = _bullet_list(resolution.get("files_changed"))
    verifications = _bullet_list(resolution.get("verification"))
    risks = _bullet_list(resolution.get("risks"))

    # Build signals
    signals = _build_review_signals(task, resolution, context, lifecycle)
    signals_text = "\n".join(f"- **{level}:** {msg}" for level, msg in signals)

    # Build contract review prompts
    contract_prompts = []

    task_validation = task.get("validation") or []
    task_invariants = task.get("invariants") or []
    task_non_goals = task.get("non_goals") or []
    task_failure_examples = task.get("failure_examples") or []

    if task_validation:
        contract_prompts.append("### Validation Contract")
        contract_prompts.append("Confirm each validation step was addressed:")
        for v in task_validation:
            contract_prompts.append(f"- [ ] {v}")
        contract_prompts.append("")

    if task_invariants:
        contract_prompts.append("### Invariant Checks")
        contract_prompts.append("Confirm each invariant was preserved:")
        for inv in task_invariants:
            contract_prompts.append(f"- [ ] {inv}")
        contract_prompts.append("")

    if task_non_goals:
        contract_prompts.append("### Non-Goal Boundary Checks")
        contract_prompts.append("Confirm none of these were done:")
        for ng in task_non_goals:
            contract_prompts.append(f"- [ ] Not done: {ng}")
        contract_prompts.append("")

    if task_failure_examples:
        contract_prompts.append("### Failure Mode Checks")
        contract_prompts.append("Confirm none of these failure modes occurred:")
        for fe in task_failure_examples:
            contract_prompts.append(f"- [ ] Not occurring: {fe}")
        contract_prompts.append("")

    if contract_prompts:
        contract_review_text = "\n".join(contract_prompts)
    else:
        contract_review_text = "No task contract defined — no additional review prompts."

    # Build validation coverage display
    task_validation_list = task.get("validation") or []
    stored_coverage = resolution.get("validation_coverage") or []

    if task_validation_list:
        coverage_lines = [""]
        for i, step in enumerate(task_validation_list, 1):
            covers = [c for c in stored_coverage if c.get("validation_index") == i]
            if covers:
                coverage_lines.append(f"**[{i}]** {step}")
                for c in covers:
                    coverage_lines.append(f"  - Covered by: {c.get('evidence', '(no evidence)')}")
            else:
                coverage_lines.append(f"**[{i}]** {step}")
                coverage_lines.append(f"  - **UNCOVERED**")
        validation_coverage_text = "\n".join(coverage_lines)
    else:
        validation_coverage_text = "No validation contract defined."

    # Review outcome (if already reviewed)
    review = handoff_state.get("review", {})
    if review.get("outcome"):
        must_address = review.get("must_address") or []
        must_address_text = ""
        if must_address:
            must_address_text = "\n- **Must address:**\n" + "\n".join(f"  - {item}" for item in must_address)

        review_section = f"""
## Review Outcome (recorded)
- **Outcome:** {review['outcome']}
- **Reviewed by:** {review.get('reviewed_by', 'unknown')}
- **Reviewed at:** {review.get('reviewed_at', 'unknown')}
- **Note:** {review.get('note') or '(none)'}{must_address_text}"""
    else:
        review_section = """
## Review Outcome
Not yet reviewed."""

    return f"""\
# Completion Review: {handoff_id}

## Review Target
- **Handoff:** {handoff_id}
- **Room:** {room_id}
- **Assigned to:** {assigned_to}
- **Status:** {status}
- **Completed by:** {completed_by}
- **Completed at:** {completed_at}

## Task Context
- **Task:** {task_desc}
- **Scope:** {scope}
- **Room goal:** {room_goal}
- **Room phase:** {room_phase}

## Constraints

### Room-Level
{room_constraints}

### Task-Level
{task_constraints}

## Acceptance Criteria

### Room-Level
{room_criteria}

### Task-Level
{task_criteria}

## Task Contract

### Non-Goals
{non_goals}

### Invariants
{invariants}

### Failure Examples
{failure_examples}

### Validation
{validation_list}

## Completion Evidence

### Summary
{summary}

### Files Changed
{files}

### Verification Steps
{verifications}

### Risks
{risks}

## Review Signals
{signals_text}

## Contract Review Prompts
{contract_review_text}

## Validation Coverage
{validation_coverage_text}
{review_section}

---
*{"A review decision has been recorded above. This remains a read-only review view." if review.get("outcome") else "This is a read-only review packet. No approval or rejection has been performed. The reviewer should assess the evidence above and decide on next steps manually."}*"""


def cmd_handoff_review(args):
    handoff_id = args.handoff_id
    handoff_state, room_state = _load_handoff_with_room(handoff_id)

    status = handoff_state.get("handoff", {}).get("status", "")
    if status != "completed":
        print(
            f"Error: Handoff '{handoff_id}' is in '{status}' state. "
            f"Review is only available for 'completed' handoffs.",
            file=sys.stderr,
        )
        sys.exit(1)

    print(_render_review(handoff_state, room_state))


# ---------------------------------------------------------------------------
# review authority helpers
# ---------------------------------------------------------------------------

def _load_peer(peer_id: str) -> dict:
    """Load peer entry from registry. Returns the peer dict or exits with error."""
    if not os.path.isfile(storage.PEER_REGISTRY_PATH):
        print(f"Error: peer_registry.yaml not found.", file=sys.stderr)
        sys.exit(1)

    reg = storage.read_state(storage.PEER_REGISTRY_PATH)
    peers = reg.get("peers") or []
    for p in peers:
        if isinstance(p, dict) and p.get("id") == peer_id:
            return p

    print(f"Error: peer '{peer_id}' not found in peer_registry.yaml.", file=sys.stderr)
    sys.exit(1)


def _enforce_review_authority(handoff_state: dict, reviewer_id: str, action_name: str) -> None:
    """Enforce review authority: reviewer type + no self-review.

    Exits with error if:
    - Peer not found
    - Peer type is not 'reviewer'
    - Reviewer is the handoff assignee (handoff.to)
    - Reviewer is the completer (resolution.completed_by)
    """
    from .validators import validate_slug
    validate_slug(reviewer_id, "peer_id")

    peer = _load_peer(reviewer_id)

    peer_type = peer.get("type", "")
    if peer_type != "reviewer":
        print(
            f"Error: Peer '{reviewer_id}' has type '{peer_type}'. "
            f"Only peers with type 'reviewer' can {action_name}.",
            file=sys.stderr,
        )
        sys.exit(1)

    h = handoff_state.get("handoff", {})
    resolution = handoff_state.get("resolution", {})

    assignee = h.get("to", "")
    if reviewer_id == assignee:
        print(
            f"Error: Peer '{reviewer_id}' is the assignee of this handoff. "
            f"Self-review is not allowed.",
            file=sys.stderr,
        )
        sys.exit(1)

    completed_by = resolution.get("completed_by", "")
    if completed_by and reviewer_id == completed_by:
        print(
            f"Error: Peer '{reviewer_id}' completed this handoff. "
            f"Self-review is not allowed.",
            file=sys.stderr,
        )
        sys.exit(1)


# ---------------------------------------------------------------------------
# review outcome
# ---------------------------------------------------------------------------

def cmd_handoff_approve(args):
    handoff_id = args.handoff_id
    reviewer = args.by
    note = args.note or ""

    handoff_state, room_state = _load_handoff_with_room(handoff_id)
    _enforce_review_authority(handoff_state, reviewer, "approve")

    status = handoff_state.get("handoff", {}).get("status", "")
    if status != "completed":
        print(
            f"Error: Handoff '{handoff_id}' is in '{status}' state. "
            f"Only 'completed' handoffs can be reviewed.",
            file=sys.stderr,
        )
        sys.exit(1)

    # Check if already reviewed
    existing_review = handoff_state.get("review", {})
    if existing_review.get("outcome"):
        print(
            f"Error: Handoff '{handoff_id}' already has review outcome: '{existing_review['outcome']}'.",
            file=sys.stderr,
        )
        sys.exit(1)

    # Validation coverage gate
    task_validation = handoff_state.get("task", {}).get("validation") or []
    if task_validation:
        stored_coverage = handoff_state.get("resolution", {}).get("validation_coverage") or []
        covered_indices = set(c.get("validation_index") for c in stored_coverage)
        uncovered = [
            (i, task_validation[i - 1])
            for i in range(1, len(task_validation) + 1)
            if i not in covered_indices
        ]

        if not stored_coverage:
            print(
                f"Error: Cannot approve — validation contract defines {len(task_validation)} step(s) "
                f"but no validation coverage was recorded. Use --validation-cover during completion.",
                file=sys.stderr,
            )
            sys.exit(1)

        if uncovered:
            print(
                f"Error: Cannot approve — {len(uncovered)} validation step(s) remain uncovered:",
                file=sys.stderr,
            )
            for idx, text in uncovered:
                print(f"  [{idx}] {text}", file=sys.stderr)
            sys.exit(1)

    now = storage.now_iso()
    handoff_state["review"] = {
        "outcome": "approved",
        "reviewed_by": reviewer,
        "reviewed_at": now,
        "note": note,
    }
    storage.write_state(storage.handoff_path(handoff_id), handoff_state)

    # Update room
    room_id = handoff_state["handoff"]["room_id"]
    extra = f"Review: approved"
    if note:
        extra += f" | Note: {note}"
    _log_transition(room_id, handoff_id, reviewer, "approved", extra, now)

    print(f"Handoff '{handoff_id}' approved by '{reviewer}'.")
    if note:
        print(f"  note: {note}")


def cmd_handoff_request_changes(args):
    handoff_id = args.handoff_id
    reviewer = args.by
    note = args.note
    must_address = args.must_address  # list, required + action=append

    handoff_state, room_state = _load_handoff_with_room(handoff_id)
    _enforce_review_authority(handoff_state, reviewer, "request changes")

    status = handoff_state.get("handoff", {}).get("status", "")
    if status != "completed":
        print(
            f"Error: Handoff '{handoff_id}' is in '{status}' state. "
            f"Only 'completed' handoffs can be reviewed.",
            file=sys.stderr,
        )
        sys.exit(1)

    # Check if already reviewed
    existing_review = handoff_state.get("review", {})
    if existing_review.get("outcome"):
        print(
            f"Error: Handoff '{handoff_id}' already has review outcome: '{existing_review['outcome']}'.",
            file=sys.stderr,
        )
        sys.exit(1)

    now = storage.now_iso()
    handoff_state["review"] = {
        "outcome": "changes_requested",
        "reviewed_by": reviewer,
        "reviewed_at": now,
        "note": note,
        "must_address": must_address,
    }
    storage.write_state(storage.handoff_path(handoff_id), handoff_state)

    # Update room
    room_id = handoff_state["handoff"]["room_id"]
    extra = f"Review: changes_requested | Note: {note} | {len(must_address)} must-address item(s)"
    _log_transition(room_id, handoff_id, reviewer, "changes_requested", extra, now)

    print(f"Handoff '{handoff_id}' — changes requested by '{reviewer}'.")
    print(f"  note: {note}")
    print(f"  must-address items: {len(must_address)}")


# ---------------------------------------------------------------------------
# rework
# ---------------------------------------------------------------------------

def cmd_handoff_rework(args):
    source_id = args.handoff_id
    requester = args.by
    assignee = args.to  # may be None

    require_peer(requester)

    # Load and validate source handoff
    source_state, room_state = _load_handoff_with_room(source_id)
    source_h = source_state.get("handoff", {})
    source_task = source_state.get("task", {})
    source_review = source_state.get("review", {})

    # Must be completed
    if source_h.get("status") != "completed":
        print(
            f"Error: Handoff '{source_id}' is in '{source_h.get('status', '')}' state. "
            f"Rework requires 'completed' status.",
            file=sys.stderr,
        )
        sys.exit(1)

    # Must have changes_requested
    if source_review.get("outcome") != "changes_requested":
        outcome = source_review.get("outcome", "(no review)")
        print(
            f"Error: Handoff '{source_id}' has review outcome '{outcome}'. "
            f"Rework requires 'changes_requested'.",
            file=sys.stderr,
        )
        sys.exit(1)

    # Determine assignee
    if assignee:
        require_peer(assignee)
    else:
        assignee = source_h.get("to", "")
        if assignee:
            require_peer(assignee)
        else:
            print(f"Error: Cannot determine assignee for rework.", file=sys.stderr)
            sys.exit(1)

    # Generate new handoff id
    room_id = source_h.get("room_id")
    rework_id = f"{source_id}-rework-1"

    # Find unique id if rework already exists
    counter = 1
    while os.path.exists(storage.handoff_path(rework_id)):
        counter += 1
        rework_id = f"{source_id}-rework-{counter}"

    os.makedirs(storage.HANDOFFS_DIR, exist_ok=True)

    now = storage.now_iso()
    review_note = source_review.get("note", "")
    must_address = source_review.get("must_address") or []

    # Build rework task description
    original_desc = source_task.get("description", "")
    rework_desc = f"[Rework of {source_id}] {original_desc}"
    if review_note:
        rework_desc += f"\n\nReview feedback: {review_note}"

    new_state = {
        "handoff": {
            "id": rework_id,
            "room_id": room_id,
            "program_id": source_h.get("program_id"),
            "from": "orchestrator",
            "to": assignee,
            "status": "open",
            "priority": source_h.get("priority", "medium"),
            "rework_of": source_id,
        },
        "task": {
            "description": rework_desc,
            "scope": source_task.get("scope", ""),
            "constraints": source_task.get("constraints") or [],
            "acceptance_criteria": source_task.get("acceptance_criteria") or [],
            "report_back": source_task.get("report_back", ""),
            "non_goals": source_task.get("non_goals") or [],
            "invariants": source_task.get("invariants") or [],
            "failure_examples": source_task.get("failure_examples") or [],
            "validation": source_task.get("validation") or [],
        },
        "rework": {
            "review_note": review_note,
            "must_address": must_address,
        },
        "timestamps": {
            "created_at": now,
            "claimed_at": None,
            "completed_at": None,
        },
    }

    storage.write_state(storage.handoff_path(rework_id), new_state)

    # Room log
    log_entry = (
        f"\n## {now} — {requester}\n"
        f"- Rework handoff `{rework_id}` created from `{source_id}`\n"
        f"- Assigned to: {assignee}\n"
        f"- Reason: changes_requested by {source_review.get('reviewed_by', 'unknown')}\n"
        f"- Must-address items: {len(must_address)}\n"
    )
    storage.append_log(storage.room_log_path(room_id), log_entry)
    storage.update_state(storage.room_state_path(room_id), {"room.updated_at": now})

    print(f"Rework handoff '{rework_id}' created.")
    print(f"  source:   {source_id}")
    print(f"  room:     {room_id}")
    print(f"  to:       {assignee}")
    print(f"  priority: {source_h.get('priority', 'medium')}")


# ---------------------------------------------------------------------------
# Transition helpers
# ---------------------------------------------------------------------------

_VALID_TRANSITIONS = {
    "open": {"claimed"},
    "claimed": {"blocked", "completed"},
    "blocked": set(),
    "completed": set(),
}


def _load_handoff(handoff_id: str) -> dict:
    require_handoff(handoff_id)
    return storage.read_state(storage.handoff_path(handoff_id))


def _assert_transition(current: str, target: str) -> None:
    allowed = _VALID_TRANSITIONS.get(current, set())
    if target not in allowed:
        print(
            f"Error: Cannot transition from '{current}' to '{target}'.",
            file=sys.stderr,
        )
        sys.exit(1)


def _assert_assignee(state: dict, peer_id: str, handoff_id: str) -> None:
    assignee = state.get("handoff", {}).get("to", "")
    if peer_id != assignee:
        print(
            f"Error: Peer '{peer_id}' is not the assignee of handoff '{handoff_id}'. "
            f"Current assignee: '{assignee}'.",
            file=sys.stderr,
        )
        sys.exit(1)


def _write_transition(handoff_id: str, state: dict, updates: dict) -> None:
    """Merge updates into state and write atomically."""
    for dotkey, value in updates.items():
        parts = dotkey.split(".", 1)
        section, key = parts[0], parts[1] if len(parts) == 2 else None
        if key is None:
            state[section] = value
        else:
            if section not in state:
                state[section] = {}
            state[section][key] = value
    storage.write_state(storage.handoff_path(handoff_id), state)


def _log_transition(room_id: str, handoff_id: str, peer_id: str, action: str, extra: str, now: str) -> None:
    entry = (
        f"\n## {now} — {peer_id}\n"
        f"- Handoff `{handoff_id}` {action} by {peer_id}\n"
    )
    if extra:
        entry += f"- {extra}\n"
    storage.append_log(storage.room_log_path(room_id), entry)
    storage.update_state(storage.room_state_path(room_id), {"room.updated_at": now})


# ---------------------------------------------------------------------------
# claim
# ---------------------------------------------------------------------------

def cmd_handoff_claim(args):
    handoff_id = args.handoff_id
    peer_id = args.by

    require_peer(peer_id)
    state = _load_handoff(handoff_id)
    current = state.get("handoff", {}).get("status", "")
    _assert_transition(current, "claimed")
    _assert_assignee(state, peer_id, handoff_id)

    now = storage.now_iso()
    _write_transition(handoff_id, state, {
        "handoff.status": "claimed",
        "handoff.to": peer_id,
        "timestamps.claimed_at": now,
    })

    room_id = state["handoff"]["room_id"]
    _log_transition(room_id, handoff_id, peer_id, "claimed", "", now)

    print(f"Handoff '{handoff_id}' claimed by '{peer_id}'.")


# ---------------------------------------------------------------------------
# block
# ---------------------------------------------------------------------------

def cmd_handoff_block(args):
    handoff_id = args.handoff_id
    peer_id = args.by
    reason = args.reason

    require_peer(peer_id)
    state = _load_handoff(handoff_id)
    current = state.get("handoff", {}).get("status", "")
    _assert_transition(current, "blocked")
    _assert_assignee(state, peer_id, handoff_id)

    now = storage.now_iso()
    _write_transition(handoff_id, state, {
        "handoff.status": "blocked",
        "timestamps.blocked_at": now,
        "resolution.blocked_by": peer_id,
        "resolution.blocked_reason": reason,
    })

    room_id = state["handoff"]["room_id"]
    _log_transition(room_id, handoff_id, peer_id, "blocked", f"Reason: {reason}", now)

    print(f"Handoff '{handoff_id}' blocked by '{peer_id}'.")
    print(f"  reason: {reason}")


# ---------------------------------------------------------------------------
# complete
# ---------------------------------------------------------------------------

def cmd_handoff_complete(args):
    handoff_id = args.handoff_id
    peer_id = args.by
    summary = args.summary
    files = args.files or []
    verifications = args.verifications or []
    risks = args.risks or []
    validation_covers = args.validation_covers or []

    require_peer(peer_id)
    state = _load_handoff(handoff_id)
    current = state.get("handoff", {}).get("status", "")
    _assert_transition(current, "completed")
    _assert_assignee(state, peer_id, handoff_id)

    # Parse and validate coverage
    task_validation = state.get("task", {}).get("validation") or []
    parsed_coverage = []
    if validation_covers:
        if not task_validation:
            print(
                "Error: --validation-cover specified but handoff has no task.validation contract.",
                file=sys.stderr,
            )
            sys.exit(1)

        for vc in validation_covers:
            if ":" not in vc:
                print(
                    f"Error: Invalid --validation-cover format: '{vc}'. Expected '<index>:<evidence>'.",
                    file=sys.stderr,
                )
                sys.exit(1)
            idx_str, evidence = vc.split(":", 1)
            try:
                idx = int(idx_str)
            except ValueError:
                print(
                    f"Error: Invalid validation index: '{idx_str}'. Must be a number.",
                    file=sys.stderr,
                )
                sys.exit(1)
            if idx < 1 or idx > len(task_validation):
                print(
                    f"Error: Validation index {idx} out of range. "
                    f"Valid range: 1-{len(task_validation)}.",
                    file=sys.stderr,
                )
                sys.exit(1)
            parsed_coverage.append({
                "validation_index": idx,
                "validation_text": task_validation[idx - 1],
                "evidence": evidence.strip(),
            })

    now = storage.now_iso()

    # Set validation coverage before transition write
    if "resolution" not in state:
        state["resolution"] = {}
    state["resolution"]["validation_coverage"] = parsed_coverage

    _write_transition(handoff_id, state, {
        "handoff.status": "completed",
        "timestamps.completed_at": now,
        "resolution.completed_by": peer_id,
        "resolution.summary": summary,
        "resolution.files_changed": files,
        "resolution.verification": verifications,
        "resolution.risks": risks,
    })

    room_id = state["handoff"]["room_id"]
    extra_parts = [f"Summary: {summary}"]
    if files:
        extra_parts.append(f"{len(files)} file(s)")
    if verifications:
        extra_parts.append(f"{len(verifications)} verification(s)")
    if risks:
        extra_parts.append(f"{len(risks)} risk(s)")
    _log_transition(room_id, handoff_id, peer_id, "completed", " | ".join(extra_parts), now)

    print(f"Handoff '{handoff_id}' completed by '{peer_id}'.")
    print(f"  summary: {summary}")
    if files:
        print(f"  files:   {len(files)}")
    if verifications:
        print(f"  checks:  {len(verifications)}")
    if risks:
        print(f"  risks:   {len(risks)}")
