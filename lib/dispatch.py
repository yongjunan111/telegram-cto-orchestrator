"""Handoff dispatch — read-only plan and executable allocation."""
import os
import sys
import subprocess
from datetime import datetime, timezone, timedelta

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
    decision = _compute_dispatch_decision(
        h, peer_entry, target_peer, handoff_room, handoff_id,
        handoff_status, handoff_kind, review_state,
        peer_sessions, session_evaluations, session_parse_errors,
        room_blocker_summary, room_blocked_by,
    )
    outcome = decision["outcome"]
    reasons = decision["reasons"]

    # Render
    output = _render_dispatch_plan(
        h, target_peer, peer_entry, handoff_kind, handoff_status, review_state,
        room_phase, room_blocker_summary, room_blocked_by,
        session_evaluations, session_parse_errors, outcome, reasons,
    )
    print(output)


def cmd_handoff_dispatch(args):
    handoff_id = args.handoff_id

    # Reuse plan computation
    handoff_state, room_state = _load_handoff_with_room(handoff_id)
    h = handoff_state.get("handoff", {})
    target_peer = h.get("to", "")
    handoff_room = h.get("room_id", "")
    handoff_status = h.get("status", "")
    handoff_kind = _get_handoff_kind(handoff_state)
    review_state = _derive_review_state(handoff_state)

    peer_entry = _load_peer_entry(target_peer)
    sessions, session_parse_errors = _scan_sessions()
    peer_sessions = [s for s in sessions if s.get("session", {}).get("peer_id") == target_peer]

    session_evaluations = []
    for sess_state in peer_sessions:
        verdict, reason = _evaluate_session_eligibility(
            sess_state, target_peer, handoff_room, handoff_id, handoff_kind
        )
        session_evaluations.append({"state": sess_state, "verdict": verdict, "reason": reason})

    room_lifecycle = room_state.get("lifecycle", {})
    room_blocker_summary = room_lifecycle.get("blocker_summary") or ""
    room_blocked_by = room_lifecycle.get("blocked_by") or ""

    decision = _compute_dispatch_decision(
        h, peer_entry, target_peer, handoff_room, handoff_id,
        handoff_status, handoff_kind, review_state,
        peer_sessions, session_evaluations, session_parse_errors,
        room_blocker_summary, room_blocked_by,
    )
    outcome = decision["outcome"]
    reasons = decision["reasons"]
    chosen_session = decision.get("chosen_session")

    # Handle non-executable outcomes
    if outcome == "cannot_allocate":
        print(f"Error: cannot dispatch handoff '{handoff_id}'.", file=sys.stderr)
        for r in reasons:
            print(f"  - {r}", file=sys.stderr)
        sys.exit(1)

    if outcome == "wait_for_existing_assignment":
        print(f"Cannot dispatch '{handoff_id}': handoff is already assigned.")
        for r in reasons:
            print(f"  - {r}")
        print("No state changed.")
        return

    # Determine cwd
    cwd = (peer_entry or {}).get("cwd") or os.getcwd()
    if not os.path.isdir(cwd):
        print(f"Error: cwd '{cwd}' does not exist for peer '{target_peer}'.", file=sys.stderr)
        sys.exit(1)

    now = storage.now_iso()
    lease_until = _conservative_lease(now)

    if outcome == "fresh_session":
        result = _execute_fresh_dispatch(
            handoff_state, room_state, target_peer, handoff_id, handoff_room,
            handoff_kind, cwd, now, lease_until
        )
    elif outcome == "reuse_existing_session":
        result = _execute_reuse_dispatch(
            handoff_state, room_state, chosen_session, target_peer, handoff_id,
            handoff_room, handoff_kind, now, lease_until
        )
    else:
        print(f"Error: unhandled outcome '{outcome}'.", file=sys.stderr)
        sys.exit(1)

    if not result["ok"]:
        print(f"Error: dispatch failed: {result['error']}", file=sys.stderr)
        sys.exit(1)

    print(f"Handoff '{handoff_id}' dispatched.")
    print(f"  outcome:       {outcome}")
    print(f"  session:       {result['session_id']}")
    print(f"  tmux_session:  {result['tmux_session']}")
    print(f"  cwd:           {cwd}")
    print(f"  artifact:      {result['artifact_path']}")
    for r in reasons:
        print(f"  - {r}")


# ---------------------------------------------------------------------------
# Peer / session helpers
# ---------------------------------------------------------------------------

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


def _compute_dispatch_decision(
    h, peer_entry, target_peer, handoff_room, handoff_id,
    handoff_status, handoff_kind, review_state,
    peer_sessions, session_evaluations, session_parse_errors,
    room_blocker_summary, room_blocked_by,
):
    """Returns dict: {outcome, reasons, chosen_session}."""
    reasons = []

    # cannot_allocate: target peer missing
    if not target_peer:
        reasons.append("Handoff has no target peer (handoff.to is empty)")
        return {"outcome": "cannot_allocate", "reasons": reasons, "chosen_session": None}

    if peer_entry is None:
        reasons.append(f"Target peer '{target_peer}' not found in peer registry")
        return {"outcome": "cannot_allocate", "reasons": reasons, "chosen_session": None}

    # Room blocker hard stop
    if room_blocker_summary or room_blocked_by:
        reasons = ["Room is blocked"]
        if room_blocker_summary:
            reasons.append(f"blocker_summary: {room_blocker_summary}")
        if room_blocked_by:
            reasons.append(f"blocked_by: {room_blocked_by}")
        return {
            "outcome": "cannot_allocate",
            "reasons": reasons,
            "chosen_session": None,
        }

    # cannot_allocate: handoff in non-dispatchable status
    if handoff_status in ("completed",):
        if review_state == "approved":
            reasons.append("Handoff is completed and approved — no further dispatch needed")
            return {"outcome": "cannot_allocate", "reasons": reasons, "chosen_session": None}
        if review_state == "changes_requested":
            reasons.append("Handoff has changes_requested — create a rework handoff first")
            return {"outcome": "cannot_allocate", "reasons": reasons, "chosen_session": None}
        # pending_review
        reasons.append("Handoff is completed and pending review — review or rework, not dispatch")
        return {"outcome": "cannot_allocate", "reasons": reasons, "chosen_session": None}

    if handoff_status == "blocked":
        reasons.append("Handoff is blocked")
        return {"outcome": "cannot_allocate", "reasons": reasons, "chosen_session": None}

    # wait_for_existing_assignment: same handoff already assigned to a busy session
    for sess_state in peer_sessions:
        s = sess_state.get("session", {})
        if s.get("handoff_id") == handoff_id and s.get("status") == "busy":
            reasons.append(f"Handoff already assigned to busy session '{s.get('id')}'")
            return {"outcome": "wait_for_existing_assignment", "reasons": reasons, "chosen_session": None}

    # wait_for_existing_assignment: handoff already bound to any session (non-busy)
    for sess_state in peer_sessions:
        s = sess_state.get("session", {})
        if s.get("handoff_id") == handoff_id:
            reasons.append(f"Handoff already bound to session '{s.get('id')}'")
            return {"outcome": "wait_for_existing_assignment", "reasons": reasons, "chosen_session": None}

    # Parse errors → conservative
    if session_parse_errors:
        reasons.append(
            f"{len(session_parse_errors)} session file(s) could not be parsed: "
            f"{', '.join(session_parse_errors)}"
        )
        reasons.append("Cannot trust runtime state for reuse — defaulting to fresh_session")
        return {"outcome": "fresh_session", "reasons": reasons, "chosen_session": None}

    # reuse_existing_session: any eligible session
    eligible = [e for e in session_evaluations if e["verdict"] == "eligible"]
    if eligible:
        chosen = eligible[0]["state"]
        sess_id = chosen.get("session", {}).get("id", "?")
        reasons.append(f"Eligible clean idle session found: '{sess_id}'")
        reasons.append("Same peer, same room (or unbound), idle, not dirty, lease valid")
        return {"outcome": "reuse_existing_session", "reasons": reasons, "chosen_session": chosen}

    # Default: fresh_session
    if not peer_sessions:
        reasons.append(f"No existing sessions for peer '{target_peer}' — fresh allocation")
    else:
        reasons.append(
            f"{len(peer_sessions)} session(s) for peer '{target_peer}', none eligible for reuse"
        )
    return {"outcome": "fresh_session", "reasons": reasons, "chosen_session": None}


# ---------------------------------------------------------------------------
# Tmux helpers
# ---------------------------------------------------------------------------

def _tmux_session_exists(name: str) -> bool:
    try:
        result = subprocess.run(
            ["tmux", "has-session", "-t", name],
            capture_output=True, timeout=5
        )
        return result.returncode == 0
    except Exception:
        return False


def _tmux_create_session(name: str, cwd: str) -> tuple:
    """Returns (success, error_message)."""
    try:
        result = subprocess.run(
            ["tmux", "new-session", "-d", "-s", name, "-c", cwd],
            capture_output=True, text=True, timeout=10
        )
        if result.returncode != 0:
            return False, result.stderr.strip() or "tmux new-session failed"
        return True, ""
    except FileNotFoundError:
        return False, "tmux command not found"
    except Exception as e:
        return False, f"tmux error: {e}"


def _tmux_kill_session(name: str) -> None:
    """Best-effort cleanup. Ignore errors."""
    try:
        subprocess.run(
            ["tmux", "kill-session", "-t", name],
            capture_output=True, timeout=5
        )
    except Exception:
        pass


def _tmux_send_keys(name: str, keys: str) -> None:
    """Best-effort send-keys. Ignore errors."""
    try:
        subprocess.run(
            ["tmux", "send-keys", "-t", name, keys, "Enter"],
            capture_output=True, timeout=5
        )
    except Exception:
        pass


# ---------------------------------------------------------------------------
# Naming / ID helpers
# ---------------------------------------------------------------------------

def _slug_safe(s: str, max_len: int = 30) -> str:
    """Make a string slug-safe and truncate."""
    safe = "".join(c if c.isalnum() or c in "-_" else "-" for c in s)
    return safe[:max_len]


def _generate_tmux_name(peer_id: str, handoff_id: str) -> str:
    return f"orch-{_slug_safe(peer_id, 20)}-{_slug_safe(handoff_id, 30)}"[:80]


def _generate_session_id(peer_id: str, handoff_id: str) -> str:
    return f"{_slug_safe(peer_id, 20)}-{_slug_safe(handoff_id, 30)}"[:60]


# ---------------------------------------------------------------------------
# Lease helper
# ---------------------------------------------------------------------------

def _conservative_lease(now_iso: str) -> str:
    """Return now + 1 hour as ISO string. Conservative default."""
    try:
        ts = datetime.fromisoformat(now_iso.replace("Z", "+00:00"))
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=timezone.utc)
        lease = ts + timedelta(hours=1)
        return lease.isoformat().replace("+00:00", "Z")
    except Exception:
        return ""


# ---------------------------------------------------------------------------
# Dispatch execution
# ---------------------------------------------------------------------------

def _execute_fresh_dispatch(
    handoff_state, room_state, target_peer, handoff_id, handoff_room,
    handoff_kind, cwd, now, lease_until
):
    tmux_name = _generate_tmux_name(target_peer, handoff_id)
    session_id = _generate_session_id(target_peer, handoff_id)

    # Check tmux name collision
    if _tmux_session_exists(tmux_name):
        return {"ok": False, "error": f"tmux session '{tmux_name}' already exists"}

    # Check session id collision
    if os.path.isfile(storage.session_path(session_id)):
        return {"ok": False, "error": f"session '{session_id}' state already exists"}

    # Create tmux session
    ok, err = _tmux_create_session(tmux_name, cwd)
    if not ok:
        return {"ok": False, "error": err}

    # Write session state
    try:
        os.makedirs(storage.SESSIONS_DIR, exist_ok=True)
        sess_state = {
            "session": {
                "id": session_id,
                "peer_id": target_peer,
                "tmux_session": tmux_name,
                "mode": "ephemeral",
                "status": "busy",
                "room_id": handoff_room,
                "handoff_id": handoff_id,
                "cwd": cwd,
                "branch": None,
                "dirty": False,
                "reuse_count": 0,
                "heartbeat_at": now,
                "lease_until": lease_until,
                "last_active_at": now,
            }
        }
        storage.write_state(storage.session_path(session_id), sess_state)
    except Exception as e:
        # Roll back tmux session on state write failure
        _tmux_kill_session(tmux_name)
        return {"ok": False, "error": f"session state write failed: {e}; tmux rolled back"}

    # Write dispatch artifact
    try:
        artifact_path = _write_dispatch_artifact(
            handoff_state, room_state, session_id, tmux_name, target_peer, now
        )
    except Exception as e:
        # Don't roll back state — artifact is derived, but warn
        print(f"Warning: artifact write failed: {e}", file=sys.stderr)
        artifact_path = "(failed)"

    # Send-keys to display artifact (best effort)
    if artifact_path and artifact_path != "(failed)":
        _tmux_send_keys(tmux_name, "clear")
        _tmux_send_keys(tmux_name, f"echo 'Dispatch artifact:' && cat {artifact_path}")

    return {
        "ok": True,
        "session_id": session_id,
        "tmux_session": tmux_name,
        "artifact_path": artifact_path,
    }


def _execute_reuse_dispatch(
    handoff_state, room_state, chosen_session, target_peer, handoff_id,
    handoff_room, handoff_kind, now, lease_until
):
    s = chosen_session.get("session", {})
    session_id = s.get("id", "")
    tmux_name = s.get("tmux_session", "")

    if not session_id:
        return {"ok": False, "error": "chosen session has no id"}

    # Preflight: tmux must exist
    if not tmux_name:
        return {"ok": False, "error": f"reuse candidate '{session_id}' has no tmux_session name"}
    if not _tmux_session_exists(tmux_name):
        return {"ok": False, "error": f"reuse candidate's tmux session '{tmux_name}' does not exist"}

    # Update session state
    try:
        path = storage.session_path(session_id)
        if not os.path.isfile(path):
            return {"ok": False, "error": f"session '{session_id}' state file disappeared"}
        sess_state = storage.read_state(path)
        sess = sess_state.get("session", {})
        sess["status"] = "busy"
        sess["room_id"] = handoff_room
        sess["handoff_id"] = handoff_id
        sess["reuse_count"] = (sess.get("reuse_count") or 0) + 1
        sess["heartbeat_at"] = now
        sess["lease_until"] = lease_until
        sess["last_active_at"] = now
        sess_state["session"] = sess
        storage.write_state(path, sess_state)
    except Exception as e:
        return {"ok": False, "error": f"session state update failed: {e}"}

    # Write dispatch artifact
    try:
        artifact_path = _write_dispatch_artifact(
            handoff_state, room_state, session_id, tmux_name, target_peer, now
        )
    except Exception as e:
        print(f"Warning: artifact write failed: {e}", file=sys.stderr)
        artifact_path = "(failed)"

    # Send-keys to existing session (if tmux session still exists)
    if tmux_name and _tmux_session_exists(tmux_name) and artifact_path != "(failed)":
        _tmux_send_keys(tmux_name, "clear")
        _tmux_send_keys(tmux_name, f"echo 'Dispatch artifact:' && cat {artifact_path}")

    return {
        "ok": True,
        "session_id": session_id,
        "tmux_session": tmux_name,
        "artifact_path": artifact_path,
    }


def _write_dispatch_artifact(handoff_state, room_state, session_id, tmux_name, target_peer, now):
    from .handoffs import _render_brief

    DISPATCHES_DIR = os.path.join(storage.RUNTIME_DIR, "dispatches")
    os.makedirs(DISPATCHES_DIR, exist_ok=True)

    h = handoff_state.get("handoff", {})
    handoff_id = h.get("id", "?")
    artifact_path = os.path.join(DISPATCHES_DIR, f"{handoff_id}.md")

    brief = _render_brief(handoff_state, room_state)

    content = f"""# Dispatch Artifact: {handoff_id}

- **Handoff ID:** {handoff_id}
- **Room ID:** {h.get('room_id', '?')}
- **Target peer:** {target_peer}
- **Kind:** {_get_handoff_kind(handoff_state)}
- **Session ID:** {session_id}
- **Tmux session:** {tmux_name}
- **Generated at:** {now}

---

{brief}

---

*This file is a derived dispatch artifact. The source of truth is the handoff and room YAML state. Do not edit this file directly — it will be regenerated on the next dispatch.*
"""

    with open(artifact_path, "w") as f:
        f.write(content)

    return artifact_path


# ---------------------------------------------------------------------------
# Dispatch plan renderer
# ---------------------------------------------------------------------------

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
