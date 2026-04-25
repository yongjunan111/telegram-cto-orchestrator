"""Room-level Idle Snapshot V1 (read-only).

`orchctl room idle-snapshot <room-id> --idle-minutes N` produces a markdown
operational snapshot describing room state, handoff summary by status, and
idle candidate sessions. Purpose is preservation of work, NOT archive
eligibility (gc-audit is for that).

V1 rules (locked by handoff orch-room-idle-snapshot-v1):

- Read-only except report file creation. No YAML / wiki / tmux / provider
  context mutation. No archive command stub. No `git fetch`.
- Recommendations use only the locked token set:
  needs_worker_complete, needs_cto_review, at_risk, unbound, parse_error,
  repair_needed. No other recommendation tokens.
- Tier 1 (authoritative): YAML state. Tier 2 (tmux liveness/pane capture)
  may appear ONLY in a `runtime_observation` block; in V1 we omit it
  entirely so it cannot influence recommendations.
- Idle threshold filter: a session is "idle candidate" iff the most recent
  of `last_active_at` / `heartbeat_at` is older than --idle-minutes. If
  only one of the two timestamps is present, that one is used. If both are
  missing but the YAML otherwise parses, the session is classified as
  `repair_needed` (key missing — recoverable). If the YAML is malformed or
  the handoff_id ref is not slug-safe, the session is classified as
  `parse_error` (binding cannot be trusted).
- Report header MUST include the literal authority-disclaimer line.
- Report path is contained under
  `.orchestrator/runtime/idle-snapshots/<room-id>/` via
  `storage.safe_write_text` (containment + symlink-refuse + atomic rename).
"""
import os
import sys
from datetime import datetime, timezone

from . import storage
from .handoffs import _derive_review_state, scan_room_handoffs
from .validators import is_slug_safe, require_room


HEADER_DISCLAIMER = (
    "> This is a read-only operational snapshot. Not authoritative state. "
    "Does not authorize any archive/compact/kill action."
)

# Locked recommendation token set. Any drift requires a coordinated handoff
# contract bump.
RECOMMENDATION_TOKENS = {
    "needs_worker_complete",
    "needs_cto_review",
    "at_risk",
    "unbound",
    "parse_error",
    "repair_needed",
}


def _idle_snapshot_dir() -> str:
    return os.path.join(storage.RUNTIME_DIR, "idle-snapshots")


# Time helpers --------------------------------------------------------------

def _parse_iso_timestamp(raw):
    """Best-effort parse of YAML timestamp into aware UTC datetime.

    Accepts the ISO-Z format we write (`YYYY-MM-DDTHH:MM:SSZ`), naive ISO
    strings, and `datetime` objects produced by yaml.safe_load. Returns None
    if the value cannot be interpreted — caller treats as "no timestamp".
    """
    if raw is None or raw == "":
        return None
    if isinstance(raw, datetime):
        if raw.tzinfo is None:
            return raw.replace(tzinfo=timezone.utc)
        return raw.astimezone(timezone.utc)
    if not isinstance(raw, str):
        return None
    s = raw.strip()
    if not s:
        return None
    # Normalise trailing 'Z' since fromisoformat on 3.10 rejects it.
    if s.endswith("Z"):
        s = s[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(s)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def _format_duration(seconds: float) -> str:
    """Render a duration as `XdYhZmWs` with leading zeros suppressed.

    Always positive; caller clamps. We render days/hours/minutes/seconds so
    operator-facing output stays compact regardless of magnitude.
    """
    total = int(seconds)
    if total < 0:
        total = 0
    days, rem = divmod(total, 86400)
    hours, rem = divmod(rem, 3600)
    minutes, secs = divmod(rem, 60)
    parts = []
    if days:
        parts.append(f"{days}d")
    if hours:
        parts.append(f"{hours}h")
    if minutes:
        parts.append(f"{minutes}m")
    if secs or not parts:
        parts.append(f"{secs}s")
    return "".join(parts)


# Session scanning ---------------------------------------------------------

def _scan_room_sessions(room_id: str):
    """Return (sessions_for_room, parse_error_session_ids).

    Walks `.orchestrator/runtime/sessions/*.yaml`. A parse error surfaces
    regardless of room binding — operators need to see corrupt session
    files even if the room binding is unreadable.
    """
    sessions = []
    parse_errors = []
    if not os.path.isdir(storage.SESSIONS_DIR):
        return sessions, parse_errors

    for fname in sorted(os.listdir(storage.SESSIONS_DIR)):
        if not fname.endswith(".yaml") or fname == ".gitkeep":
            continue
        path = os.path.join(storage.SESSIONS_DIR, fname)
        try:
            state = storage.read_state(path)
            if not isinstance(state, dict):
                raise ValueError("not a mapping")
            s = state.get("session", {})
            if not isinstance(s, dict):
                raise ValueError("missing 'session' section")
            if s.get("room_id") == room_id:
                sessions.append(state)
        except Exception:
            parse_errors.append(fname[:-5])
    return sessions, parse_errors


# Classification -----------------------------------------------------------

def _classify_session(s_state, handoffs_by_id, idle_threshold_seconds, now_utc):
    """Classify a session for the idle snapshot.

    Returns a dict carrying:
      - classification: one of `idle`, `not_idle`, `parse_error`,
        `repair_needed`. Only `idle` / `parse_error` / `repair_needed`
        sessions appear in the report's session block list.
      - recommendation: a single locked token from RECOMMENDATION_TOKENS
        (or None if `not_idle` and no flag fired).
      - resolved session metadata used by the renderer.
    """
    s = s_state.get("session", {})
    session_id = s.get("id") or "(unknown)"
    raw_handoff_id = s.get("handoff_id")
    peer_id = s.get("peer_id")
    status = s.get("status")
    mode = s.get("mode")
    room_id = s.get("room_id")
    last_launch_status = s.get("last_launch_status")

    # Hardening: validate slug-safety of handoff_id BEFORE we let it reach
    # storage.handoff_path() / runtime artifact paths. Same invariant locked
    # for gc-audit and checkpoints. Tainted bindings classify as parse_error
    # and the binding is dropped from the report.
    if raw_handoff_id is not None and not is_slug_safe(raw_handoff_id):
        return {
            "session_id": session_id,
            "classification": "parse_error",
            "recommendation": "parse_error",
            "peer_id": peer_id,
            "status": status,
            "mode": mode,
            "room_id": room_id,
            "handoff_id": None,
            "idle_duration": None,
            "idle_seconds": None,
            "newest_signal_at": None,
            "newest_signal_field": None,
            "last_launch_status": last_launch_status,
            "related_handoff_status": None,
            "related_review_state": None,
            "dispatch_artifact_path": None,
            "bootstrap_path": None,
            "checkpoint_path": None,
            "resolution_summary": None,
            "note": "handoff_id ref is not slug-safe; binding dropped",
        }

    handoff_id = raw_handoff_id

    # Idle threshold: take the more recent of last_active_at / heartbeat_at.
    # If both are missing, classify as repair_needed — the operator can fix
    # the session YAML and re-run, so we never silently treat absence as
    # liveness.
    last_active_at = _parse_iso_timestamp(s.get("last_active_at"))
    heartbeat_at = _parse_iso_timestamp(s.get("heartbeat_at"))

    candidates = []
    if last_active_at is not None:
        candidates.append((last_active_at, "last_active_at"))
    if heartbeat_at is not None:
        candidates.append((heartbeat_at, "heartbeat_at"))

    if not candidates:
        return {
            "session_id": session_id,
            "classification": "repair_needed",
            "recommendation": "repair_needed",
            "peer_id": peer_id,
            "status": status,
            "mode": mode,
            "room_id": room_id,
            "handoff_id": handoff_id,
            "idle_duration": None,
            "idle_seconds": None,
            "newest_signal_at": None,
            "newest_signal_field": None,
            "last_launch_status": last_launch_status,
            "related_handoff_status": None,
            "related_review_state": None,
            "dispatch_artifact_path": None,
            "bootstrap_path": None,
            "checkpoint_path": None,
            "resolution_summary": None,
            "note": "missing both last_active_at and heartbeat_at",
        }

    candidates.sort(key=lambda t: t[0], reverse=True)
    newest_signal_at, newest_signal_field = candidates[0]
    age_seconds = max(0.0, (now_utc - newest_signal_at).total_seconds())
    is_idle = age_seconds >= idle_threshold_seconds

    if not is_idle:
        return {
            "session_id": session_id,
            "classification": "not_idle",
            "recommendation": None,
            "peer_id": peer_id,
            "status": status,
            "mode": mode,
            "room_id": room_id,
            "handoff_id": handoff_id,
            "idle_duration": _format_duration(age_seconds),
            "idle_seconds": age_seconds,
            "newest_signal_at": newest_signal_at,
            "newest_signal_field": newest_signal_field,
            "last_launch_status": last_launch_status,
            "related_handoff_status": None,
            "related_review_state": None,
            "dispatch_artifact_path": None,
            "bootstrap_path": None,
            "checkpoint_path": None,
            "resolution_summary": None,
            "note": None,
        }

    # Idle candidate — derive related handoff state and a recommendation.
    related_handoff_status = None
    related_review_state = None
    resolution_summary = None
    recommendation = "unbound"

    if handoff_id and handoff_id in handoffs_by_id:
        ho_state = handoffs_by_id[handoff_id]
        h = ho_state.get("handoff", {}) or {}
        related_handoff_status = h.get("status")
        related_review_state = _derive_review_state(ho_state)
        resolution = ho_state.get("resolution") or {}
        if isinstance(resolution, dict):
            resolution_summary = resolution.get("summary")

        if related_handoff_status in ("open", "claimed"):
            recommendation = "needs_worker_complete"
        elif related_handoff_status == "completed":
            if related_review_state == "pending_review":
                recommendation = "needs_cto_review"
            elif related_review_state == "changes_requested":
                recommendation = "at_risk"
            elif related_review_state == "approved":
                # Idle on an already-approved handoff: there is no live work
                # to preserve, but the session is still occupying runtime
                # state. Surface as `at_risk` so the operator notices it
                # rather than dropping it silently.
                recommendation = "at_risk"
            else:
                recommendation = "needs_cto_review"
        elif related_handoff_status == "blocked":
            recommendation = "at_risk"
        else:
            # Unknown / missing handoff status field on an existing handoff.
            recommendation = "at_risk"
    elif handoff_id:
        # Handoff id present but not in this room (or file missing).
        recommendation = "unbound"
    else:
        recommendation = "unbound"

    # Derived artifact paths (read-only pointers; we never write to them).
    dispatch_artifact_path = None
    bootstrap_path = None
    if handoff_id:
        dispatch_artifact_path = os.path.join(
            storage.RUNTIME_DIR, "dispatches", f"{handoff_id}.md"
        )
        if not os.path.isfile(dispatch_artifact_path):
            dispatch_artifact_path = None

    if is_slug_safe(session_id):
        candidate_bootstrap = os.path.join(
            storage.RUNTIME_DIR, "bootstrap", f"{session_id}.md"
        )
        if os.path.isfile(candidate_bootstrap):
            bootstrap_path = candidate_bootstrap

    checkpoint_path = _latest_checkpoint(session_id)

    return {
        "session_id": session_id,
        "classification": "idle",
        "recommendation": recommendation,
        "peer_id": peer_id,
        "status": status,
        "mode": mode,
        "room_id": room_id,
        "handoff_id": handoff_id,
        "idle_duration": _format_duration(age_seconds),
        "idle_seconds": age_seconds,
        "newest_signal_at": newest_signal_at,
        "newest_signal_field": newest_signal_field,
        "last_launch_status": last_launch_status,
        "related_handoff_status": related_handoff_status,
        "related_review_state": related_review_state,
        "dispatch_artifact_path": dispatch_artifact_path,
        "bootstrap_path": bootstrap_path,
        "checkpoint_path": checkpoint_path,
        "resolution_summary": resolution_summary,
        "note": None,
    }


def _latest_checkpoint(session_id: str):
    """Return path of newest checkpoint file for `session_id`, or None.

    Checkpoint filenames are `<session_id>-<event>-<timestamp>.md`. We pick
    the newest by mtime to avoid coupling to the timestamp encoding (which
    has changed historically). Read-only — we never mutate the directory.
    """
    if not is_slug_safe(session_id):
        return None
    checkpoints_dir = os.path.join(storage.RUNTIME_DIR, "checkpoints")
    if not os.path.isdir(checkpoints_dir):
        return None
    prefix = session_id + "-"
    matches = []
    try:
        for fname in os.listdir(checkpoints_dir):
            if not fname.endswith(".md") or not fname.startswith(prefix):
                continue
            full = os.path.join(checkpoints_dir, fname)
            try:
                mt = os.path.getmtime(full)
            except OSError:
                continue
            matches.append((mt, full))
    except OSError:
        return None
    if not matches:
        return None
    matches.sort(reverse=True)
    return matches[0][1]


# Handoff summary ----------------------------------------------------------

def _bucket_handoffs(room_handoffs):
    """Group handoffs by status; for `completed`, attach review_state."""
    buckets = {
        "open": [],
        "claimed": [],
        "completed": [],
        "blocked": [],
        "other": [],
    }
    for ho_state in room_handoffs:
        h = ho_state.get("handoff", {}) or {}
        status = h.get("status") or "other"
        entry = {
            "id": h.get("id"),
            "to": h.get("to"),
            "priority": h.get("priority"),
            "kind": h.get("kind"),
            "review_state": (
                _derive_review_state(ho_state) if status == "completed" else None
            ),
        }
        if status in buckets:
            buckets[status].append(entry)
        else:
            buckets["other"].append({**entry, "status": status})
    return buckets


# Filename selection -------------------------------------------------------

def _report_filename(base_dir: str) -> str:
    """Pick `<UTC-ISO-TIMESTAMP>[-NNN].md` that does not yet exist.

    Format: `YYYY-MM-DDTHH-MM-SS.ffffffZ.md` (colons replaced with hyphens
    so the filename is portable across filesystems while remaining a clear
    UTC ISO timestamp).
    """
    now = datetime.now(timezone.utc)
    iso = now.strftime("%Y-%m-%dT%H-%M-%S.%f") + "Z"
    candidate = os.path.join(base_dir, f"{iso}.md")
    if not os.path.exists(candidate):
        return candidate
    for n in range(1, 1000):
        candidate = os.path.join(base_dir, f"{iso}-{n:03d}.md")
        if not os.path.exists(candidate):
            return candidate
    raise RuntimeError(
        "Exceeded 1000 microsecond-collision suffixes; refusing to proceed"
    )


# Rendering ----------------------------------------------------------------

def _fmt(v):
    if v is None or v == "":
        return "(none)"
    return str(v)


def _render_room_section(room_state, room_id) -> list:
    room = room_state.get("room", {}) or {}
    context = room_state.get("context", {}) or {}
    lifecycle = room_state.get("lifecycle", {}) or {}
    return [
        "## Room",
        "",
        f"- **ID:** {_fmt(room.get('id') or room_id)}",
        f"- **Name:** {_fmt(room.get('name'))}",
        f"- **Status:** {_fmt(room.get('status'))}",
        f"- **Phase:** {_fmt(lifecycle.get('current_phase'))}",
        f"- **Current summary:** {_fmt(context.get('current_summary'))}",
        f"- **Next action:** {_fmt(lifecycle.get('next_action'))}",
        "",
    ]


def _render_handoff_summary(buckets) -> list:
    lines = ["## Handoff Summary", ""]
    for status_key in ("open", "claimed", "completed", "blocked"):
        items = buckets.get(status_key) or []
        lines.append(f"### {status_key.capitalize()} ({len(items)})")
        if not items:
            lines.append("- (none)")
        else:
            for entry in items:
                hid = _fmt(entry.get("id"))
                to_peer = _fmt(entry.get("to"))
                if status_key == "completed":
                    rs = _fmt(entry.get("review_state"))
                    lines.append(f"- `{hid}` → {to_peer} (review: {rs})")
                else:
                    lines.append(f"- `{hid}` → {to_peer}")
        lines.append("")

    other = buckets.get("other") or []
    if other:
        lines.append(f"### Other ({len(other)})")
        for entry in other:
            hid = _fmt(entry.get("id"))
            to_peer = _fmt(entry.get("to"))
            st = _fmt(entry.get("status"))
            lines.append(f"- `{hid}` → {to_peer} (status: {st})")
        lines.append("")
    return lines


def _render_session_block(result) -> list:
    lines = [f"### {result['session_id']}", ""]
    lines.append(f"- **Peer ID:** {_fmt(result.get('peer_id'))}")
    lines.append(f"- **Status:** {_fmt(result.get('status'))}")
    lines.append(f"- **Mode:** {_fmt(result.get('mode'))}")
    lines.append(f"- **Room:** {_fmt(result.get('room_id'))}")
    lines.append(f"- **Handoff:** {_fmt(result.get('handoff_id'))}")
    lines.append(
        f"- **Idle duration:** {_fmt(result.get('idle_duration'))}"
    )
    newest_at = result.get("newest_signal_at")
    if newest_at is not None:
        lines.append(
            f"- **Newest activity signal:** "
            f"{newest_at.strftime('%Y-%m-%dT%H:%M:%SZ')} "
            f"({_fmt(result.get('newest_signal_field'))})"
        )
    lines.append(
        f"- **Last launch status:** {_fmt(result.get('last_launch_status'))}"
    )
    lines.append(
        f"- **Related handoff status:** "
        f"{_fmt(result.get('related_handoff_status'))}"
    )
    lines.append(
        f"- **Related review state:** "
        f"{_fmt(result.get('related_review_state'))}"
    )
    lines.append(
        f"- **Dispatch artifact:** {_fmt(result.get('dispatch_artifact_path'))}"
    )
    lines.append(f"- **Bootstrap:** {_fmt(result.get('bootstrap_path'))}")
    lines.append(
        f"- **Latest checkpoint:** {_fmt(result.get('checkpoint_path'))}"
    )
    lines.append(
        f"- **Resolution summary:** {_fmt(result.get('resolution_summary'))}"
    )
    lines.append(
        f"- **Classification:** {_fmt(result.get('classification'))}"
    )
    lines.append(
        f"- **Recommendation:** {_fmt(result.get('recommendation'))}"
    )
    if result.get("note"):
        lines.append(f"- **Note:** {result['note']}")
    lines.append("")
    return lines


def _render_recommendations(reportable) -> list:
    lines = ["## Recommendations", ""]
    if not reportable:
        lines.append("- (none)")
        lines.append("")
        return lines
    for r in reportable:
        token = r.get("recommendation")
        if not token:
            continue
        if token not in RECOMMENDATION_TOKENS:
            # Defense-in-depth: never let an unexpected token reach output.
            continue
        sid = r.get("session_id")
        hid = r.get("handoff_id")
        bits = []
        if hid:
            bits.append(f"handoff `{hid}`")
        cls = r.get("classification")
        if cls and cls != "idle":
            bits.append(f"classification `{cls}`")
        elif r.get("idle_duration"):
            bits.append(f"idle for {r['idle_duration']}")
        related = r.get("related_handoff_status")
        if related:
            bits.append(f"handoff status `{related}`")
        review = r.get("related_review_state")
        if review:
            bits.append(f"review `{review}`")
        if r.get("note"):
            bits.append(r["note"])
        suffix = ", ".join(bits)
        if suffix:
            lines.append(f"- `{token}`: session `{sid}` — {suffix}")
        else:
            lines.append(f"- `{token}`: session `{sid}`")
    lines.append("")
    return lines


def _render_report(
    room_id, room_state, idle_minutes, results, parse_error_session_ids,
    handoff_buckets, handoff_parse_errors, now,
):
    lines = [
        f"# Idle Snapshot: {room_id}",
        "",
        HEADER_DISCLAIMER,
        "",
        f"- **Generated at:** {now}",
        f"- **Idle threshold (minutes):** {idle_minutes}",
        f"- **Version:** 1",
        "",
    ]
    lines.extend(_render_room_section(room_state, room_id))
    lines.extend(_render_handoff_summary(handoff_buckets))

    reportable = [r for r in results if r["classification"] != "not_idle"]
    lines.append("## Idle Candidate Sessions")
    lines.append("")
    if not reportable:
        lines.append("- (none)")
        lines.append("")
    else:
        for r in reportable:
            lines.extend(_render_session_block(r))

    if parse_error_session_ids:
        lines.append("## Session File Parse Errors")
        lines.append("")
        for sid in parse_error_session_ids:
            lines.append(f"- `{sid}`")
        lines.append("")

    if handoff_parse_errors:
        lines.append("## Handoff File Parse Errors")
        lines.append("")
        for hid in handoff_parse_errors:
            lines.append(f"- `{hid}`")
        lines.append("")

    lines.extend(_render_recommendations(reportable))

    lines.append("---")
    lines.append(
        "*This file is a derived idle snapshot. The source of truth is "
        "room/handoff/session YAML state. Do not edit manually.*"
    )

    return "\n".join(lines) + "\n"


# Public entrypoint --------------------------------------------------------

def cmd_room_idle_snapshot(args):
    room_id = args.room_id
    # Validate room slug + existence BEFORE deriving any path. require_room
    # already calls validate_slug, which fails closed on tampered input.
    require_room(room_id)

    try:
        idle_minutes = int(args.idle_minutes)
    except (TypeError, ValueError):
        print(
            f"Error: --idle-minutes must be a non-negative integer, "
            f"got {args.idle_minutes!r}.",
            file=sys.stderr,
        )
        sys.exit(1)
    if idle_minutes < 0:
        print(
            "Error: --idle-minutes cannot be negative.",
            file=sys.stderr,
        )
        sys.exit(1)
    idle_threshold_seconds = idle_minutes * 60

    # Load authoritative state.
    try:
        room_state = storage.read_state(storage.room_state_path(room_id))
        if not isinstance(room_state, dict):
            room_state = {}
    except Exception:
        room_state = {}

    sessions, parse_error_session_ids = _scan_room_sessions(room_id)
    room_handoffs, handoff_parse_errors = scan_room_handoffs(room_id)

    handoffs_by_id = {}
    for ho_state in room_handoffs:
        h = ho_state.get("handoff", {}) or {}
        hid = h.get("id")
        if hid:
            handoffs_by_id[hid] = ho_state

    handoff_buckets = _bucket_handoffs(room_handoffs)

    now_utc = datetime.now(timezone.utc)
    results = [
        _classify_session(
            s_state, handoffs_by_id, idle_threshold_seconds, now_utc
        )
        for s_state in sessions
    ]

    # Surface session-file parse errors as their own report entries with the
    # locked `parse_error` recommendation token. They never gain any other
    # state and never get a handoff binding.
    for err_session_id in parse_error_session_ids:
        results.append({
            "session_id": err_session_id,
            "classification": "parse_error",
            "recommendation": "parse_error",
            "peer_id": None,
            "status": None,
            "mode": None,
            "room_id": None,
            "handoff_id": None,
            "idle_duration": None,
            "idle_seconds": None,
            "newest_signal_at": None,
            "newest_signal_field": None,
            "last_launch_status": None,
            "related_handoff_status": None,
            "related_review_state": None,
            "dispatch_artifact_path": None,
            "bootstrap_path": None,
            "checkpoint_path": None,
            "resolution_summary": None,
            "note": "session YAML failed to parse",
        })

    now_iso = now_utc.strftime("%Y-%m-%dT%H:%M:%SZ")
    content = _render_report(
        room_id, room_state, idle_minutes, results,
        parse_error_session_ids, handoff_buckets, handoff_parse_errors,
        now_iso,
    )

    base_dir = _idle_snapshot_dir()
    room_dir = os.path.join(base_dir, room_id)

    try:
        # Pre-create so we can pick a non-colliding filename deterministically.
        os.makedirs(room_dir, exist_ok=True)
        target_path = _report_filename(room_dir)
        # safe_write_text enforces containment + symlink-refuse + atomic
        # rename. base_dir is the idle-snapshots root; the per-room subdir
        # parent chain is checked via _check_parent_chain_no_symlinks.
        storage.safe_write_text(base_dir, target_path, content)
    except SystemExit:
        raise
    except Exception as e:
        print(
            f"Error: failed to write idle snapshot report: {e}",
            file=sys.stderr,
        )
        sys.exit(1)

    print(target_path)
