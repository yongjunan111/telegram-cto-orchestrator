"""Handoff dispatch — read-only plan and executable allocation."""
import os
import re
import sys
import shlex
import subprocess
from datetime import datetime, timezone, timedelta

from . import storage
from .handoffs import _load_handoff_with_room, _get_handoff_kind, _derive_review_state
from .validators import validate_slug, is_slug_safe
from .config import load_config, ConfigError


_TMUX_NAME_RE = re.compile(r'^[A-Za-z0-9_-]+$')
# tmux pane id format: %N where N is one or more digits.
_TMUX_TARGET_RE = re.compile(r'^%[0-9]+$')


def _is_safe_tmux_name(name: str) -> bool:
    return bool(name) and bool(_TMUX_NAME_RE.match(name))


def _is_safe_tmux_target(target: str) -> bool:
    """A safe tmux pane target is `%N` where N is one or more digits."""
    return bool(target) and bool(_TMUX_TARGET_RE.match(target))


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

    # Check if auto-registration would apply
    try:
        config = load_config()
    except ConfigError:
        config = {}
    auto_register = config.get("dispatch", {}).get("auto_register_peer", True)

    if peer_entry is None and auto_register:
        # Simulate what _ensure_peer would do: check execution_cwd
        execution_cwd = (room_state.get("context") or {}).get("execution_cwd") or ""
        if execution_cwd and os.path.isdir(execution_cwd):
            # Plan should reflect that peer would be auto-registered
            peer_entry = {
                "id": target_peer,
                "name": target_peer.replace("-", " ").title(),
                "type": "worker",
                "cwd": execution_cwd,
                "capabilities": [],
                "status": "available",
                "_auto_registered": True,  # marker for plan rendering
            }

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

    # Ensure peer exists (auto-register if missing)
    peer_entry, peer_err = _ensure_peer(target_peer, room_state)
    if peer_err:
        print(f"Error: {peer_err}", file=sys.stderr)
        sys.exit(1)

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

    # Determine cwd from peer entry (never fallback to os.getcwd())
    cwd = (peer_entry or {}).get("cwd") or ""
    if not cwd:
        print(f"Error: peer '{target_peer}' has no cwd configured.", file=sys.stderr)
        sys.exit(1)
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
    print(f"  tmux_target:   {result.get('tmux_target', '(none)')}")
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


def _ensure_peer(peer_id: str, room_state: dict):
    """Ensure peer exists in registry. Auto-register if missing.

    Uses room_state context.execution_cwd as the peer's cwd.
    Returns (peer_entry, error_message). On success error_message is None.
    On failure peer_entry is None.
    """
    existing = _load_peer_entry(peer_id)
    if existing is not None:
        return existing, None

    # Check config: is auto-registration enabled?
    try:
        config = load_config()
    except ConfigError:
        config = {}
    if not config.get("dispatch", {}).get("auto_register_peer", True):
        return None, (
            f"Peer '{peer_id}' not in registry and auto_register_peer is disabled in config. "
            f"Register manually: orchctl peer add {peer_id} --type worker --cwd /path"
        )

    # Auto-register: require execution_cwd from room state
    execution_cwd = (room_state.get("context") or {}).get("execution_cwd") or ""
    if not execution_cwd:
        return None, (
            f"Peer '{peer_id}' not in registry and room has no context.execution_cwd — "
            f"cannot auto-register. Set execution_cwd via: "
            f"orchctl room memory <room-id> --execution-cwd /path/to/repo"
        )

    if not os.path.isdir(execution_cwd):
        return None, (
            f"Peer '{peer_id}' not in registry and room's execution_cwd "
            f"'{execution_cwd}' does not exist"
        )

    new_peer = {
        "id": peer_id,
        "name": peer_id.replace("-", " ").title(),
        "type": "worker",
        "cwd": execution_cwd,
        "capabilities": [],
        "status": "available",
    }

    # Atomic read-modify-write of peer registry
    try:
        if os.path.isfile(storage.PEER_REGISTRY_PATH):
            reg = storage.read_state(storage.PEER_REGISTRY_PATH)
        else:
            reg = {"peers": []}

        peers = reg.get("peers") or []
        # Double-check: another process might have registered concurrently
        for p in peers:
            if isinstance(p, dict) and p.get("id") == peer_id:
                return p, None

        peers.append(new_peer)
        reg["peers"] = peers
        storage.write_state(storage.PEER_REGISTRY_PATH, reg)
    except Exception as e:
        return None, f"Failed to auto-register peer '{peer_id}': {e}"

    return new_peer, None


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
    """Return (verdict, reason) for a single session.

    Reuse eligibility is strict: a session must have an exact tmux pane target
    (`tmux_target`) recorded in its authoritative state and that pane must be
    alive. Legacy sessions without `tmux_target` are NEVER reusable — the
    duplicate-blocking path in _compute_dispatch_decision keeps them visible
    via wait_for_existing_assignment instead.
    """
    s = sess_state.get("session", {})

    if s.get("peer_id") != target_peer:
        return "ineligible", f"peer mismatch (session.peer_id={s.get('peer_id')!r})"

    # Fix 1: skip sessions with dead or missing tmux session name
    tmux_name = s.get("tmux_session") or ""
    if not tmux_name or not _tmux_session_exists(tmux_name):
        return "ineligible", "tmux session dead or missing"

    # Exact pane targeting: legacy sessions without tmux_target are NOT reusable
    tmux_target = s.get("tmux_target") or ""
    if not _is_safe_tmux_target(tmux_target):
        return "ineligible", "no exact tmux_target (legacy session — not reusable)"
    if not _tmux_target_exists(tmux_target):
        return "ineligible", f"tmux_target '{tmux_target}' dead or missing"

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

    return "eligible", "matches peer/room, idle, clean, lease valid, exact tmux_target alive"


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

    # Fix 2: Parse errors → cannot_allocate (fail-closed on corrupted runtime state)
    if session_parse_errors:
        reasons.append(
            f"{len(session_parse_errors)} session file(s) could not be parsed: "
            f"{', '.join(session_parse_errors)}"
        )
        reasons.append("Cannot trust runtime state for allocation — fix or remove the malformed file(s).")
        return {"outcome": "cannot_allocate", "reasons": reasons, "chosen_session": None}

    # Fix 1: wait_for_existing_assignment: same handoff already assigned to a busy session
    # Skip sessions whose tmux is dead — they are stale bindings and should not block allocation.
    stale_skipped = []
    for sess_state in peer_sessions:
        s = sess_state.get("session", {})
        if s.get("handoff_id") == handoff_id and s.get("status") == "busy":
            tmux_name = s.get("tmux_session") or ""
            if not tmux_name or not _tmux_session_exists(tmux_name):
                stale_skipped.append(s.get("id", "?"))
                continue
            reasons.append(f"Handoff already assigned to busy session '{s.get('id')}'")
            return {"outcome": "wait_for_existing_assignment", "reasons": reasons, "chosen_session": None}

    # Fix 1: wait_for_existing_assignment: handoff already bound to any session (non-busy)
    # Same stale-tmux guard applies.
    for sess_state in peer_sessions:
        s = sess_state.get("session", {})
        if s.get("handoff_id") == handoff_id:
            tmux_name = s.get("tmux_session") or ""
            if not tmux_name or not _tmux_session_exists(tmux_name):
                if s.get("id", "?") not in stale_skipped:
                    stale_skipped.append(s.get("id", "?"))
                continue
            reasons.append(f"Handoff already bound to session '{s.get('id')}'")
            return {"outcome": "wait_for_existing_assignment", "reasons": reasons, "chosen_session": None}

    if stale_skipped:
        reasons.append(
            f"Warning: {len(stale_skipped)} stale session binding(s) skipped (dead tmux): "
            f"{', '.join(stale_skipped)}"
        )

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


def _tmux_capture_pane_target(session_name: str) -> tuple:
    """Capture the active pane id of a tmux session immediately after creation.

    Used by fresh dispatch to bind the new session to an exact pane target so
    later send-keys hits exactly that pane (not whichever pane the user happens
    to have selected after splitting/window-creating).

    Returns (True, pane_target) on success, (False, reason) on failure.
    pane_target is verified to match the safe regex (%N).
    """
    if not _is_safe_tmux_name(session_name):
        return False, f"unsafe tmux session name '{session_name}'"
    try:
        result = subprocess.run(
            ["tmux", "display-message", "-p", "-t", session_name, "#{pane_id}"],
            capture_output=True, text=True, timeout=5,
        )
    except FileNotFoundError:
        return False, "tmux command not found"
    except Exception as e:
        return False, f"tmux display-message error: {e}"
    if result.returncode != 0:
        return False, f"tmux display-message failed: {result.stderr.strip() or 'unknown error'}"
    target = (result.stdout or "").strip()
    if not _is_safe_tmux_target(target):
        return False, f"captured pane target '{target}' is not a safe pane id"
    return True, target


def _tmux_target_exists(target: str) -> bool:
    """Check that a specific pane target is alive and resolves to itself.

    A safe target's `display-message -p -t %N '#{pane_id}'` must echo back
    the same `%N`. Returns False on any tmux error, mismatch, or unsafe input.
    """
    if not _is_safe_tmux_target(target):
        return False
    try:
        result = subprocess.run(
            ["tmux", "display-message", "-p", "-t", target, "#{pane_id}"],
            capture_output=True, text=True, timeout=5,
        )
    except Exception:
        return False
    if result.returncode != 0:
        return False
    return (result.stdout or "").strip() == target


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
# Reuse race hardening: per-session lock + CAS revalidation
# ---------------------------------------------------------------------------

LOCKS_DIR = os.path.join(storage.RUNTIME_DIR, "locks")


def _session_lock_path(session_id: str) -> str:
    return os.path.join(LOCKS_DIR, f"session-{session_id}.lock")


def _acquire_session_lock(session_id: str) -> tuple:
    """Atomic per-session lock acquire via O_CREAT|O_EXCL, with runtime path safety.

    Returns (True, lock_path) on success.
    Returns (False, reason) on any failure (lock held, symlinked locks dir,
    symlinked lock path, containment violation, payload write failure, etc).

    Safety guarantees:
    - LOCKS_DIR must not be a symlink (checked before and after makedirs via
      storage.ensure_safe_runtime_dir).
    - LOCKS_DIR realpath must be inside realpath(storage.RUNTIME_DIR) — lock
      files cannot escape the runtime tree.
    - lock_path must not be a pre-existing symlink. O_EXCL also refuses
      symlinks atomically; the explicit check yields a clearer error.
    - On payload write failure the partially-created lock file is removed so
      no stale lock is left behind. fd is always closed.

    Stale locks from crashed dispatches are NOT auto-recovered. Operator must
    remove them manually from .orchestrator/runtime/locks/.
    """
    if not is_slug_safe(session_id):
        return False, f"session_id '{session_id}' is not slug-safe"

    # Runtime path safety: LOCKS_DIR must not be a symlink and must be creatable.
    try:
        locks_real = storage.ensure_safe_runtime_dir(LOCKS_DIR)
    except ValueError as e:
        return False, f"locks dir rejected: {e}"
    except OSError as e:
        return False, f"locks dir creation failed: {e}"

    # Containment: LOCKS_DIR realpath must be inside realpath(RUNTIME_DIR).
    try:
        runtime_real = os.path.realpath(storage.RUNTIME_DIR)
    except OSError as e:
        return False, f"runtime dir realpath failed: {e}"
    if not (locks_real == runtime_real or locks_real.startswith(runtime_real + os.sep)):
        return False, (
            f"locks dir '{LOCKS_DIR}' escapes runtime root '{storage.RUNTIME_DIR}'"
        )

    lock_path = _session_lock_path(session_id)

    # Refuse pre-existing symlink at lock_path (clearer error than EEXIST path).
    if os.path.islink(lock_path):
        return False, f"lock path '{lock_path}' is a symlink; refusing to follow"

    # Atomic O_CREAT|O_EXCL. If lock_path becomes a symlink between islink
    # check and this open, O_EXCL still refuses the create atomically.
    try:
        fd = os.open(lock_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o644)
    except FileExistsError:
        return False, (
            f"session '{session_id}' is being claimed by another dispatch "
            f"(lock {lock_path} held; if stale, operator must rm)"
        )
    except OSError as e:
        return False, f"lock acquire failed: {e}"

    # Payload write — on failure, close fd and remove the partially-created
    # lock file so no stale lock remains. fd is always closed.
    payload = f"pid={os.getpid()} ts={storage.now_iso()}\n".encode("utf-8")
    write_error = None
    try:
        os.write(fd, payload)
    except OSError as e:
        write_error = e
    try:
        os.close(fd)
    except OSError:
        pass

    if write_error is not None:
        try:
            os.remove(lock_path)
        except OSError:
            pass
        return False, f"lock payload write failed: {write_error}"

    return True, lock_path


def _release_session_lock(lock_path: str) -> None:
    """Best-effort lock release. Idempotent."""
    if not lock_path:
        return
    try:
        os.remove(lock_path)
    except OSError:
        pass


def _revalidate_reuse_target(
    session_id: str,
    expected_tmux_name: str,
    expected_tmux_target: str,
    target_peer: str,
    handoff_room: str,
    handoff_id: str,
) -> tuple:
    """CAS-style revalidation: re-read session state from disk and re-check
    every eligibility condition. Used by _execute_reuse_dispatch under the
    per-session lock to close the decision -> execution drift window.

    Adds exact pane/window targeting checks: the on-disk tmux_target must be
    safe, equal to the snapshot's expected target, and refer to a live pane.

    Returns (True, fresh_state_dict) if the session is still eligible.
    Returns (False, reason) on any drift / mismatch / parse error.
    """
    path = storage.session_path(session_id)
    if not os.path.isfile(path):
        return False, "session state file disappeared"

    try:
        fresh = storage.read_state(path)
    except Exception as e:
        return False, f"session state file could not be parsed: {e}"

    if not isinstance(fresh, dict) or "session" not in fresh:
        return False, "session state file missing 'session' section"

    s = fresh.get("session") or {}

    # Internal ref re-validation (slug-safe)
    sess_room = s.get("room_id") or ""
    sess_handoff = s.get("handoff_id") or ""
    sess_tmux = s.get("tmux_session") or ""
    sess_target = s.get("tmux_target") or ""

    if sess_room:
        try:
            validate_slug(sess_room, "session.room_id")
        except SystemExit:
            return False, "invalid internal ref: session.room_id not slug-safe"
    if sess_handoff:
        try:
            validate_slug(sess_handoff, "session.handoff_id")
        except SystemExit:
            return False, "invalid internal ref: session.handoff_id not slug-safe"
    if not _is_safe_tmux_name(sess_tmux):
        return False, f"invalid internal ref: session.tmux_session '{sess_tmux}' unsafe"
    if not _is_safe_tmux_target(sess_target):
        return False, f"invalid internal ref: session.tmux_target '{sess_target}' unsafe or missing"

    # tmux_session must not have been re-pointed since the decision snapshot
    if sess_tmux != expected_tmux_name:
        return False, (
            f"tmux_session changed from '{expected_tmux_name}' to '{sess_tmux}' "
            f"since decision"
        )
    # tmux_target must not have drifted either
    if sess_target != expected_tmux_target:
        return False, (
            f"tmux_target changed from '{expected_tmux_target}' to '{sess_target}' "
            f"since decision"
        )

    # Eligibility re-check (mirror of _evaluate_session_eligibility)
    if s.get("peer_id") != target_peer:
        return False, f"peer mismatch (session.peer_id={s.get('peer_id')!r})"

    status = s.get("status", "")
    if status != "idle":
        return False, f"status is '{status}', not idle"

    if s.get("dirty"):
        return False, "session became dirty"

    if sess_room and sess_room != handoff_room:
        return False, f"room mismatch (session.room_id={sess_room!r})"

    if sess_handoff and sess_handoff != handoff_id:
        return False, f"already bound to handoff '{sess_handoff}'"

    lease_until = s.get("lease_until") or ""
    if lease_until and not _lease_valid(lease_until):
        return False, f"lease expired ({lease_until})"

    # tmux session name still live
    if not _tmux_session_exists(sess_tmux):
        return False, "tmux session no longer exists"
    # exact pane target still live
    if not _tmux_target_exists(sess_target):
        return False, f"tmux_target '{sess_target}' no longer exists"

    return True, fresh


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
# Session hook helpers
# ---------------------------------------------------------------------------

def _install_session_hook_file() -> str:
    """Install the session hook template to runtime dir if not present. Return absolute path."""
    template_src = os.path.join(os.path.dirname(__file__), "session_hooks.sh.template")
    target_dir = os.path.join(storage.RUNTIME_DIR, "hooks")
    target_path = os.path.join(target_dir, "session_hooks.sh")

    # Always copy fresh so template updates propagate
    try:
        with open(template_src) as src:
            content = src.read()
        storage.safe_write_text(target_dir, target_path, content)
    except Exception as e:
        print(f"Warning: session hook install failed: {e}", file=sys.stderr)

    return os.path.abspath(target_path)


def _get_orchctl_invocation() -> tuple:
    """Return (python_path, orchctl_path) as absolute paths."""
    # ORCHESTRATOR_DIR is .orchestrator/; repo root is its parent
    repo_root = os.path.dirname(os.path.abspath(storage.ORCHESTRATOR_DIR))
    venv_python = os.path.join(repo_root, ".venv", "bin", "python")
    orchctl_script = os.path.join(repo_root, "orchctl")
    return venv_python, orchctl_script


def _inject_session_hooks(tmux_target: str, session_id: str, handoff_id: str, room_id: str) -> None:
    """Inject env vars + source hook file into the EXACT pane identified by
    tmux_target. Best effort, idempotent. Caller is responsible for passing a
    target captured/revalidated under the appropriate guard.
    """
    if not _is_safe_tmux_target(tmux_target):
        return
    if not _tmux_target_exists(tmux_target):
        return

    hook_path = _install_session_hook_file()
    venv_python, orchctl_script = _get_orchctl_invocation()

    # Send env var exports — use shlex.quote for safe shell quoting (Fix 4)
    exports = (
        f"export ORCH_SESSION_ID={shlex.quote(session_id)} "
        f"ORCH_HANDOFF_ID={shlex.quote(handoff_id)} "
        f"ORCH_ROOM_ID={shlex.quote(room_id)} "
        f"ORCHCTL_PYTHON={shlex.quote(venv_python)} "
        f"ORCHCTL_SCRIPT={shlex.quote(orchctl_script)}"
    )
    _tmux_send_keys(tmux_target, exports)
    _tmux_send_keys(tmux_target, f"source {shlex.quote(hook_path)}")


# ---------------------------------------------------------------------------
# Dispatch execution
# ---------------------------------------------------------------------------

def _execute_fresh_dispatch(
    handoff_state, room_state, target_peer, handoff_id, handoff_room,
    handoff_kind, cwd, now, lease_until
):
    # Validate handoff.id slug BEFORE any tmux creation or state mutation
    handoff_id_internal = handoff_state.get("handoff", {}).get("id", "")
    if not is_slug_safe(handoff_id_internal):
        return {"ok": False, "error": f"handoff internal id '{handoff_id_internal}' is not slug-safe"}

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

    # Capture exact pane target IMMEDIATELY after creation. If capture fails,
    # roll back the tmux session and bail before any state / artifact / hook
    # side effects. This binds the session to a known pane id for the lifetime
    # of the dispatch, so later send-keys cannot be silently mis-routed if the
    # user splits the window.
    capture_ok, capture_value = _tmux_capture_pane_target(tmux_name)
    if not capture_ok:
        _tmux_kill_session(tmux_name)
        return {
            "ok": False,
            "error": f"tmux pane target capture failed: {capture_value}; tmux rolled back",
        }
    tmux_target = capture_value

    # Write session state
    try:
        os.makedirs(storage.SESSIONS_DIR, exist_ok=True)
        sess_state = {
            "session": {
                "id": session_id,
                "peer_id": target_peer,
                "tmux_session": tmux_name,
                "tmux_target": tmux_target,
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
            handoff_state, room_state, session_id, tmux_name, target_peer, now,
            tmux_target=tmux_target,
        )
    except Exception as e:
        # Don't roll back state — artifact is derived, but warn
        print(f"Warning: artifact write failed: {e}", file=sys.stderr)
        artifact_path = "(failed)"

    # Inject session hooks at the EXACT captured pane (best effort, idempotent)
    _inject_session_hooks(tmux_target, session_id, handoff_id, handoff_room)

    # Generate bootstrap and launch worker
    bootstrap_path = _run_bootstrap_and_display(tmux_target, session_id)
    _launch_worker(tmux_target, session_id, bootstrap_path)

    return {
        "ok": True,
        "session_id": session_id,
        "tmux_session": tmux_name,
        "tmux_target": tmux_target,
        "artifact_path": artifact_path,
    }


def _execute_reuse_dispatch(
    handoff_state, room_state, chosen_session, target_peer, handoff_id,
    handoff_room, handoff_kind, now, lease_until
):
    # Validate handoff.id slug BEFORE any state mutation
    handoff_id_internal = handoff_state.get("handoff", {}).get("id", "")
    if not is_slug_safe(handoff_id_internal):
        return {"ok": False, "error": f"handoff internal id '{handoff_id_internal}' is not slug-safe"}

    s = chosen_session.get("session", {})
    session_id = s.get("id", "")
    tmux_name = s.get("tmux_session", "")
    tmux_target = s.get("tmux_target", "")
    reuse_room_id = s.get("room_id", "")
    reuse_handoff_id = s.get("handoff_id", "")

    if not session_id:
        return {"ok": False, "error": "chosen session has no id"}

    # Fix 3: revalidate internal references from session state before use
    try:
        if reuse_room_id:
            validate_slug(reuse_room_id, "session.room_id")
        if reuse_handoff_id:
            validate_slug(reuse_handoff_id, "session.handoff_id")
    except SystemExit:
        return {"ok": False, "error": f"chosen session '{session_id}' has invalid room_id or handoff_id — cannot reuse"}

    if not tmux_name or not _is_safe_tmux_name(tmux_name):
        return {"ok": False, "error": f"chosen session '{session_id}' has unsafe tmux_session name '{tmux_name}'"}

    # Exact pane targeting is mandatory for reuse. Legacy sessions without a
    # tmux_target are NOT reusable: bail before lock acquire so we don't write
    # any state, touch any tmux, or hold any lock for an inert session.
    if not _is_safe_tmux_target(tmux_target):
        return {
            "ok": False,
            "error": (
                f"chosen session '{session_id}' has missing or unsafe tmux_target "
                f"'{tmux_target}' — cannot reuse (legacy session)"
            ),
        }

    # Acquire per-session lock BEFORE any revalidation or state mutation.
    # Closes the concurrent-reuse-dispatch race for the same session_id:
    # without this, two parallel dispatches could both pass revalidation and
    # both write status=busy, double-claiming the same idle session.
    lock_ok, lock_value = _acquire_session_lock(session_id)
    if not lock_ok:
        return {"ok": False, "error": lock_value}
    lock_path = lock_value

    try:
        # Preflight: both tmux session AND exact pane target must exist
        # (cheap fast-fail before disk re-read).
        if not _tmux_session_exists(tmux_name):
            return {"ok": False, "error": f"reuse candidate's tmux session '{tmux_name}' does not exist"}
        if not _tmux_target_exists(tmux_target):
            return {"ok": False, "error": f"reuse candidate's tmux_target '{tmux_target}' does not exist"}

        # CAS-style revalidation: re-read session state from disk and re-check
        # every eligibility condition against the snapshot's expectations.
        # Closes the decision -> execution drift window. Fail-closed on any drift.
        revalid_ok, revalid_value = _revalidate_reuse_target(
            session_id=session_id,
            expected_tmux_name=tmux_name,
            expected_tmux_target=tmux_target,
            target_peer=target_peer,
            handoff_room=handoff_room,
            handoff_id=handoff_id,
        )
        if not revalid_ok:
            return {"ok": False, "error": f"session no longer eligible for reuse: {revalid_value}"}

        fresh_state = revalid_value

        # Update session state using the freshly-read state, not the stale snapshot.
        # tmux_session and tmux_target are preserved as-is (revalidation already
        # confirmed they match the snapshot and are alive).
        try:
            path = storage.session_path(session_id)
            sess = fresh_state.get("session", {})
            sess["status"] = "busy"
            sess["room_id"] = handoff_room
            sess["handoff_id"] = handoff_id
            sess["reuse_count"] = (sess.get("reuse_count") or 0) + 1
            sess["heartbeat_at"] = now
            sess["lease_until"] = lease_until
            sess["last_active_at"] = now
            fresh_state["session"] = sess
            storage.write_state(path, fresh_state)
        except Exception as e:
            return {"ok": False, "error": f"session state update failed: {e}"}

        # Write dispatch artifact
        try:
            artifact_path = _write_dispatch_artifact(
                handoff_state, room_state, session_id, tmux_name, target_peer, now,
                tmux_target=tmux_target,
            )
        except Exception as e:
            print(f"Warning: artifact write failed: {e}", file=sys.stderr)
            artifact_path = "(failed)"

        # Re-inject session hooks at the EXACT captured pane (idempotent).
        _inject_session_hooks(tmux_target, session_id, handoff_id, handoff_room)

        # Generate bootstrap and launch worker.
        bootstrap_path = _run_bootstrap_and_display(tmux_target, session_id)
        _launch_worker(tmux_target, session_id, bootstrap_path)

        return {
            "ok": True,
            "session_id": session_id,
            "tmux_session": tmux_name,
            "tmux_target": tmux_target,
            "artifact_path": artifact_path,
        }
    finally:
        _release_session_lock(lock_path)


def _run_bootstrap_and_display(tmux_target: str, session_id: str) -> str:
    """Run session bootstrap and return the artifact path.

    Best-effort; failures do not abort dispatch. Returns the bootstrap path
    if generation succeeded and the file exists, empty string otherwise.
    """
    venv_python, orchctl_script = _get_orchctl_invocation()
    bootstrap_path = os.path.join(storage.RUNTIME_DIR, "bootstrap", f"{session_id}.md")

    # Run bootstrap command (synchronous)
    success = False
    try:
        result = subprocess.run(
            [venv_python, orchctl_script, "session", "bootstrap", session_id],
            capture_output=True, text=True, timeout=15
        )
        if result.returncode == 0:
            success = True
        else:
            print(f"Warning: bootstrap generation failed: {result.stderr.strip()}", file=sys.stderr)
    except Exception as e:
        print(f"Warning: bootstrap subprocess error: {e}", file=sys.stderr)

    if success and os.path.isfile(bootstrap_path):
        return bootstrap_path
    return ""


def _get_worker_launch_script() -> str:
    """Return absolute path to the worker launch helper script."""
    return os.path.join(os.path.dirname(os.path.abspath(__file__)), "worker_launch.py")


def _launch_worker(tmux_target: str, session_id: str, bootstrap_path: str) -> None:
    """Launch a Claude worker in the EXACT pane identified by tmux_target.

    Best-effort; failures do not abort dispatch. If a worker process is
    already running in the pane, skip to avoid duplicate execution.
    """
    if not _is_safe_tmux_target(tmux_target):
        return
    if not _tmux_target_exists(tmux_target):
        return

    # Check config: is auto-launch enabled?
    try:
        config = load_config()
    except ConfigError:
        config = {}
    if not config.get("dispatch", {}).get("auto_launch_worker", True):
        return

    if not bootstrap_path or not os.path.isfile(bootstrap_path):
        _tmux_send_keys(tmux_target, "echo 'Bootstrap artifact not available — worker not launched.'")
        return

    # Check if a worker process (claude) is already running in the pane
    if _pane_has_worker(tmux_target):
        print(f"Warning: pane {tmux_target} already has a worker process — skipping launch.", file=sys.stderr)
        return

    launch_script = _get_worker_launch_script()
    venv_python, _ = _get_orchctl_invocation()

    # Single command sent to tmux — no multiline, no quoting issues
    cmd = f"{shlex.quote(venv_python)} {shlex.quote(launch_script)} {shlex.quote(bootstrap_path)}"
    _tmux_send_keys(tmux_target, cmd)


def _pane_has_worker(tmux_target: str) -> bool:
    """Check if the pane's foreground process is claude (or similar worker).

    Uses tmux's pane_current_command format. Returns True if a worker is
    detected, False otherwise (including on any error — conservative toward
    launching).
    """
    if not _is_safe_tmux_target(tmux_target):
        return False
    try:
        result = subprocess.run(
            ["tmux", "display-message", "-p", "-t", tmux_target, "#{pane_current_command}"],
            capture_output=True, text=True, timeout=5,
        )
        if result.returncode != 0:
            return False
        current_cmd = (result.stdout or "").strip().lower()
        return current_cmd in ("claude", "codex", "node", "python", "python3")
    except Exception:
        return False


def _write_dispatch_artifact(handoff_state, room_state, session_id, tmux_name, target_peer, now, tmux_target=None):
    from .handoffs import _render_brief

    DISPATCHES_DIR = os.path.join(storage.RUNTIME_DIR, "dispatches")
    os.makedirs(DISPATCHES_DIR, exist_ok=True)

    h = handoff_state.get("handoff", {})
    handoff_id = h.get("id", "?")

    artifact_path = os.path.join(DISPATCHES_DIR, f"{handoff_id}.md")

    brief = _render_brief(handoff_state, room_state)

    target_line = tmux_target if tmux_target else "(none)"
    content = f"""# Dispatch Artifact: {handoff_id}

- **Handoff ID:** {handoff_id}
- **Room ID:** {h.get('room_id', '?')}
- **Target peer:** {target_peer}
- **Kind:** {_get_handoff_kind(handoff_state)}
- **Session ID:** {session_id}
- **Tmux session:** {tmux_name}
- **Tmux target:** {target_line}
- **Generated at:** {now}

---

{brief}

---

*This file is a derived dispatch artifact. The source of truth is the handoff and room YAML state. Do not edit this file directly — it will be regenerated on the next dispatch.*
"""

    storage.safe_write_text(DISPATCHES_DIR, artifact_path, content)

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
        if (peer_entry or {}).get("_auto_registered"):
            lines.append("- **Note:** Peer will be auto-registered at dispatch time")
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
