"""Room readiness assessment — derived read-only packet."""
import os
import sys

from . import storage
from .validators import require_room
from .handoffs import scan_room_handoffs, _derive_review_state


def cmd_room_readiness(args):
    room_id = args.room_id
    require_room(room_id)

    # Load room state
    state_file = storage.room_state_path(room_id)
    state = storage.read_state(state_file)
    context = state.get("context", {})
    lifecycle = state.get("lifecycle", {})
    discovery = state.get("discovery", {})

    # Load handoffs
    handoffs, parse_errors = scan_room_handoffs(room_id)

    # Compute handoff signals
    status_counts = {"open": 0, "claimed": 0, "blocked": 0, "completed": 0}
    review_counts = {"pending_review": 0, "approved": 0, "changes_requested": 0}

    for ho_state in handoffs:
        h = ho_state.get("handoff", {})
        s = h.get("status", "unknown")
        if s in status_counts:
            status_counts[s] += 1
        rs = _derive_review_state(ho_state)
        if rs in review_counts:
            review_counts[rs] += 1

    # Check for rework coverage of changes_requested
    changes_requested_ids = []
    reworked_ids = set()
    for ho_state in handoffs:
        h = ho_state.get("handoff", {})
        review = ho_state.get("review", {})
        if h.get("status") == "completed" and review.get("outcome") == "changes_requested":
            changes_requested_ids.append(h.get("id", "?"))
        if h.get("rework_of"):
            reworked_ids.add(h["rework_of"])

    unreworked_changes = [hid for hid in changes_requested_ids if hid not in reworked_ids]

    # Compute discovery signals
    problem_statement = discovery.get("problem_statement", "") or ""
    chosen_direction = discovery.get("chosen_direction", "") or ""
    confirmed_facts = discovery.get("confirmed_facts") or []
    implementation_unknowns = discovery.get("implementation_unknowns") or []
    options_considered = discovery.get("options_considered") or []
    decisions_made = discovery.get("decisions_made") or []
    readiness_notes = discovery.get("readiness_notes", "") or ""

    # Compute contract signals
    constraints = context.get("constraints") or []
    acceptance_criteria = context.get("acceptance_criteria") or []

    # Compute operational signals
    open_questions = context.get("open_questions") or []
    blocker_summary = lifecycle.get("blocker_summary", "") or ""
    blocked_by = lifecycle.get("blocked_by") or ""
    next_action = lifecycle.get("next_action", "") or ""
    phase = lifecycle.get("current_phase", "") or ""
    goal = context.get("goal", "") or ""

    # Decision logic
    recommendation, reasons = _compute_recommendation(
        status_counts, review_counts, unreworked_changes,
        problem_statement, chosen_direction, open_questions,
        implementation_unknowns, blocker_summary, blocked_by,
        parse_errors,
    )

    # Render
    output = _render_readiness(
        room_id, phase, next_action, goal,
        problem_statement, chosen_direction, confirmed_facts,
        implementation_unknowns, options_considered, decisions_made,
        constraints, acceptance_criteria,
        open_questions, blocker_summary, blocked_by,
        status_counts, review_counts,
        parse_errors, recommendation, reasons,
    )
    print(output)


def _compute_recommendation(
    status_counts, review_counts, unreworked_changes,
    problem_statement, chosen_direction, open_questions,
    implementation_unknowns, blocker_summary, blocked_by,
    parse_errors,
):
    reasons = []

    # Parse errors → conservative
    if parse_errors:
        reasons.append(f"{len(parse_errors)} handoff file(s) could not be parsed — readiness assessment may be incomplete")

    # Blocked handoff or room blocker
    if status_counts["blocked"] > 0 or blocker_summary or blocked_by:
        reasons.append("Blocker present — resolve before proceeding")
        return "wait_on_active_handoff", reasons

    # Open or claimed handoffs exist
    if status_counts["open"] > 0 or status_counts["claimed"] > 0:
        active = status_counts["open"] + status_counts["claimed"]
        reasons.append(f"{active} active handoff(s) in progress — wait for completion")
        return "wait_on_active_handoff", reasons

    # Pending review
    if review_counts["pending_review"] > 0:
        reasons.append(f"{review_counts['pending_review']} completed handoff(s) awaiting review")
        return "review_completed_handoff", reasons

    # Parse errors mean dispatch recommendations would be based on incomplete handoff truth.
    if parse_errors:
        reasons.append("Resolve handoff parse errors before issuing new handoffs or rework")
        return "clarify_in_room", reasons

    # Unreworked changes_requested
    if unreworked_changes:
        reasons.append(f"{len(unreworked_changes)} changes_requested handoff(s) without rework: {', '.join(unreworked_changes)}")
        return "create_rework_handoff", reasons

    # Discovery gaps
    has_problem = bool(problem_statement.strip())
    has_direction = bool(chosen_direction.strip())
    has_open_questions = len(open_questions) > 0
    has_unknowns = len(implementation_unknowns) > 0

    if not has_problem and has_open_questions:
        reasons.append("No problem statement defined and open questions remain")
        return "clarify_in_room", reasons

    if not has_direction and has_open_questions:
        reasons.append("No chosen direction and open questions remain")
        return "clarify_in_room", reasons

    if has_direction and has_unknowns and len(implementation_unknowns) >= 3:
        reasons.append(f"Chosen direction exists but {len(implementation_unknowns)} implementation unknowns remain — consider discovery work first")
        return "create_discovery_handoff", reasons

    # Implementation-ready check (conservative)
    if has_problem and has_direction and not has_open_questions:
        reasons.append("Problem defined, direction chosen, no open questions, no active handoffs")
        if has_unknowns:
            reasons.append(f"Note: {len(implementation_unknowns)} implementation unknown(s) remain — reviewer should assess risk")
        return "create_implementation_handoff", reasons

    if has_problem and has_direction:
        if has_open_questions:
            reasons.append(f"Direction chosen but {len(open_questions)} open question(s) remain")
            return "clarify_in_room", reasons
        reasons.append("Problem defined, direction chosen")
        return "create_implementation_handoff", reasons

    # Fallback
    if not has_problem:
        reasons.append("No problem statement — room needs initial scoping")
    if not has_direction:
        reasons.append("No chosen direction — room needs discovery/planning work")
    return "clarify_in_room", reasons


def _render_readiness(
    room_id, phase, next_action, goal,
    problem_statement, chosen_direction, confirmed_facts,
    implementation_unknowns, options_considered, decisions_made,
    constraints, acceptance_criteria,
    open_questions, blocker_summary, blocked_by,
    status_counts, review_counts,
    parse_errors, recommendation, reasons,
):
    def _present(val):
        return "present" if val and val.strip() else "missing"

    lines = [
        f"# Room Readiness: {room_id}",
        "",
        "## Room",
        f"- **Goal:** {goal or '(none)'}",
        f"- **Phase:** {phase or '(none)'}",
        f"- **Next action:** {next_action or '(none)'}",
        "",
        "## Discovery Signals",
        f"- problem_statement: {_present(problem_statement)}",
        f"- chosen_direction: {_present(chosen_direction)}",
        f"- confirmed_facts: {len(confirmed_facts)}",
        f"- implementation_unknowns: {len(implementation_unknowns)}",
        f"- options_considered: {len(options_considered)}",
        f"- decisions_made: {len(decisions_made)}",
        "",
        "## Contract Signals",
        f"- constraints: {len(constraints)}",
        f"- acceptance_criteria: {len(acceptance_criteria)}",
        "",
        "## Operational Blockers",
        f"- open_questions: {len(open_questions)}",
        f"- blocker: {'present' if blocker_summary else 'none'}",
        f"- blocked_by: {blocked_by or 'none'}",
        "",
        "## Handoff Signals",
        f"- open: {status_counts['open']}",
        f"- claimed: {status_counts['claimed']}",
        f"- blocked: {status_counts['blocked']}",
        f"- completed: {status_counts['completed']}",
        f"- pending_review: {review_counts['pending_review']}",
        f"- approved: {review_counts['approved']}",
        f"- changes_requested: {review_counts['changes_requested']}",
    ]

    if parse_errors:
        lines.append("")
        lines.append(f"**WARNING:** {len(parse_errors)} handoff file(s) could not be parsed: {', '.join(parse_errors)}")
        lines.append("Readiness assessment may be incomplete.")

    lines.extend([
        "",
        "## Recommended Next Action",
        f"**{recommendation}**",
        "",
        "### Why",
    ])
    for r in reasons:
        lines.append(f"- {r}")

    lines.extend([
        "",
        "---",
        "*This is a derived read-only assessment. No state has been modified. The operator should validate the recommendation before acting.*",
    ])

    return "\n".join(lines)
