"""Handoff dispatch plan — derived read-only allocation recommendation."""
import os
import sys
from datetime import datetime, timezone

from . import storage
from .handoffs import _load_handoff_with_room, _get_handoff_kind, _derive_review_state


def cmd_handoff_dispatch_plan(args):
    handoff_id = args.handoff_id
    handoff_state, room_state = _load_handoff_with_room(handoff_id)

    h = handoff_state.get("handoff", {})
    lifecycle = room_state.get("lifecycle", {})
    target_peer = h.get("to", "")
    handoff_room = h.get("room_id", "")
    handoff_status = h.get("status", "")
    handoff_kind = _get_handoff_kind(handoff_state)
    review_state = _derive_review_state(handoff_state)
    room_phase = lifecycle.get("current_phase", "") or ""
    room_blocker_summary = lifecycle.get("blocker_summary", "") or ""
    room_blocked_by = lifecycle.get("blocked_by") or ""

    # Load peer registry
    peer_entry = _load_peer_entry(target_peer)

    # Load all sessions
    sessions, session_parse_errors = _scan_sessions()

    # Filter to sessions for the target peer
    peer_sessions = [s for s in sessions if s.get("session", {}).get("peer_id") == target_peer]

    # Evaluate each session for eligibility
    session_evaluations = []
    for sess_state in peer_sessions:
        verdict, reason = _evaluate_session_eligibility(
            sess_state, target_peer, handoff_room, handoff_id, handoff_kind
        )
        session_evaluations.append({
            "state": sess_state,
            "verdict": verdict,
            "reason": reason,
        })

    # Decide outcome
    outcome, reasons = _compute_dispatch_outcome(
        h, peer_entry, target_peer, handoff_room, handoff_id,
        handoff_status, handoff_kind, review_state,
        room_phase, room_blocker_summary, room_blocked_by,
        peer_sessions, session_evaluations, session_parse_errors,
    )

    # Render
    output = _render_dispatch_plan(
        h, target_peer, peer_entry, handoff_kind, handoff_status, review_state,
        room_phase, room_blocker_summary, room_blocked_by,
        session_evaluations, session_parse_errors, outcome, reasons,
    )
    print(output)


def _load_peer_entry(peer_id: str):
    """Return peer dict from registry, or None if missing/malformed."""
    try:
        reg = storage.read_state(storage.PEER_REGISTRY_PATH)
        peers = reg.get("peers") or []
        for p in peers:
            if isinstance(p, dict) and p.get("id") == peer_id:
                return p
    except Exception:
        return None
    return None


def _scan_sessions():
    """Scan session files. Returns (valid_session_states, parse_error_filenames)."""
    if not os.path.isdir(storage.SESSIONS_DIR):
        return [], []
    results = []
    errors = []
    for fname in sorted(os.listdir(storage.SESSIONS_DIR)):
        if not fname.endswith(".yaml") or fname == ".gitkeep":
            continue
        path = os.path.join(storage.SESSIONS_DIR, fname)
        try:
            state = storage.read_state(path)
            if not isinstance(state, dict) or "session" not in state:
                raise ValueError("missing 'session' section")
            results.append(state)
        except Exception:
            errors.append(fname[:-5])
    return results, errors


def _lease_valid(lease_until: str) -> bool:
    """Check if a lease_until ISO timestamp is in the future. Empty/missing = treated as valid."""
    if not lease_until:
        return True
    try:
        ts_str = lease_until.replace("Z", "+00:00")
        ts = datetime.fromisoformat(ts_str)
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=timezone.utc)
        now = datetime.now(timezone.utc)
        return ts > now
    except Exception:
        # Unparseable lease → treat as expired (conservative)
        return False


def _evaluate_session_eligibility(sess_state, target_peer, handoff_room, handoff_id, handoff_kind):
    """Return (verdict, reason) for a single session."""
    s = sess_state.get("session", {})

    if s.get("peer_id") != target_peer:
        return "ineligible", f"peer mismatch (session.peer_id={s.get('peer_id')!r})"

    status = s.get("status", "")
    if status != "idle":
        return "ineligible", f"status is '{status}', not idle"

    if s.get("dirty"):
        return "ineligible", "session is dirty"

    sess_room = s.get("room_id")
    if sess_room and sess_room != handoff_room:
        return "ineligible", f"room mismatch (session.room_id={sess_room!r})"

    sess_handoff = s.get("handoff_id")
    if sess_handoff and sess_handoff != handoff_id:
        return "ineligible", f"already bound to handoff '{sess_handoff}'"

    lease_until = s.get("lease_until") or ""
    if lease_until and not _lease_valid(lease_until):
        return "ineligible", f"lease expired ({lease_until})"

    return "eligible", "matches peer/room, idle, clean, lease valid"


def _compute_dispatch_outcome(
    h, peer_entry, target_peer, handoff_room, handoff_id,
    handoff_status, handoff_kind, review_state,
    room_phase, room_blocker_summary, room_blocked_by,
    peer_sessions, session_evaluations, session_parse_errors,
):
    reasons = []

    # cannot_allocate: target peer missing
    if not target_peer:
        reasons.append("Handoff has no target peer (handoff.to is empty)")
        return "cannot_allocate", reasons

    if peer_entry is None:
        reasons.append(f"Target peer '{target_peer}' not found in peer registry")
        return "cannot_allocate", reasons

    if room_blocker_summary or room_blocked_by:
        reasons.append("Room is blocked")
        if room_blocker_summary:
            reasons.append(f"blocker_summary: {room_blocker_summary}")
        if room_blocked_by:
            reasons.append(f"blocked_by: {room_blocked_by}")
        return "cannot_allocate", reasons

    # cannot_allocate: handoff in non-dispatchable status
    if handoff_status in ("completed",):
        if review_state == "approved":
            reasons.append("Handoff is completed and approved — no further dispatch needed")
            return "cannot_allocate", reasons
        if review_state == "changes_requested":
            reasons.append("Handoff has changes_requested — create a rework handoff first")
            return "cannot_allocate", reasons
        # pending_review
        reasons.append("Handoff is completed and pending review — review or rework, not dispatch")
        return "cannot_allocate", reasons

    if handoff_status == "blocked":
        reasons.append("Handoff is blocked")
        return "cannot_allocate", reasons

    # wait_for_existing_assignment: same handoff already assigned to a busy session
    for sess_state in peer_sessions:
        s = sess_state.get("session", {})
        if s.get("handoff_id") == handoff_id and s.get("status") == "busy":
            reasons.append(f"Handoff already assigned to busy session '{s.get('id')}'")
            return "wait_for_existing_assignment", reasons

    # wait_for_existing_assignment: handoff already bound to any session (non-busy)
    for sess_state in peer_sessions:
        s = sess_state.get("session", {})
        if s.get("handoff_id") == handoff_id:
            reasons.append(f"Handoff already bound to session '{s.get('id')}'")
            return "wait_for_existing_assignment", reasons

    # Parse errors → conservative
    if session_parse_errors:
        reasons.append(
            f"{len(session_parse_errors)} session file(s) could not be parsed: "
            f"{', '.join(session_parse_errors)}"
        )
        reasons.append("Cannot trust runtime state for reuse — defaulting to fresh_session")
        return "fresh_session", reasons

    # reuse_existing_session: any eligible session
    eligible = [e for e in session_evaluations if e["verdict"] == "eligible"]
    if eligible:
        sess_id = eligible[0]["state"].get("session", {}).get("id", "?")
        reasons.append(f"Eligible clean idle session found: '{sess_id}'")
        reasons.append("Same peer, same room (or unbound), idle, not dirty, lease valid")
        return "reuse_existing_session", reasons

    # Default: fresh_session
    if not peer_sessions:
        reasons.append(f"No existing sessions for peer '{target_peer}' — fresh allocation")
    else:
        reasons.append(
            f"{len(peer_sessions)} session(s) for peer '{target_peer}', none eligible for reuse"
        )
    return "fresh_session", reasons


def _render_dispatch_plan(
    h, target_peer, peer_entry, handoff_kind, handoff_status, review_state,
    room_phase, room_blocker_summary, room_blocked_by,
    session_evaluations, session_parse_errors, outcome, reasons,
):
    handoff_id = h.get("id", "?")
    room_id = h.get("room_id", "?")

    lines = [
        f"# Dispatch Plan: {handoff_id}",
        "",
        "## Handoff",
        f"- **ID:** {handoff_id}",
        f"- **Room:** {room_id}",
        f"- **To:** {target_peer or '(none)'}",
        f"- **Kind:** {handoff_kind}",
        f"- **Status:** {handoff_status}",
        f"- **Review state:** {review_state}",
        "",
        "## Room",
        f"- **Phase:** {room_phase or '(none)'}",
        f"- **Blocked:** {'yes' if (room_blocker_summary or room_blocked_by) else 'no'}",
        f"- **Blocker summary:** {room_blocker_summary or '(none)'}",
        f"- **Blocked by:** {room_blocked_by or '(none)'}",
        "",
        "## Peer",
        f"- **Target peer:** {target_peer or '(none)'}",
    ]

    if peer_entry is not None:
        lines.append(f"- **Peer type:** {peer_entry.get('type', '(unknown)')}")
    else:
        lines.append("- **Peer type:** (NOT FOUND in registry)")

    lines.append("")
    lines.append("## Sessions Considered")

    if not session_evaluations:
        lines.append(f"No sessions exist for peer '{target_peer}'.")
    else:
        for ev in session_evaluations:
            s = ev["state"].get("session", {})
            lines.append(f"- **{s.get('id', '?')}**")
            lines.append(f"  - mode: {s.get('mode') or '(none)'}")
            lines.append(f"  - status: {s.get('status') or '(none)'}")
            lines.append(f"  - room_id: {s.get('room_id') or '(none)'}")
            lines.append(f"  - handoff_id: {s.get('handoff_id') or '(none)'}")
            lines.append(f"  - dirty: {'yes' if s.get('dirty') else 'no'}")
            lines.append(f"  - lease_until: {s.get('lease_until') or '(none)'}")
            lines.append(f"  - **verdict:** {ev['verdict']}")
            lines.append(f"  - **reason:** {ev['reason']}")

    if session_parse_errors:
        lines.append("")
        lines.append(
            f"**WARNING:** {len(session_parse_errors)} session file(s) could not be parsed: "
            f"{', '.join(session_parse_errors)}"
        )

    lines.append("")
    lines.append("## Recommended Allocation")
    lines.append(f"**{outcome}**")
    lines.append("")
    lines.append("### Why")
    for r in reasons:
        lines.append(f"- {r}")

    lines.append("")
    lines.append("---")
    lines.append(
        "*This is a derived read-only dispatch plan. "
        "No state has been modified. "
        "The operator should validate before acting.*"
    )

    return "\n".join(lines)
