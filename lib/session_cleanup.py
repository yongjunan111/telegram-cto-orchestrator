"""Session cleanup report — V1 read-only.

Pure compute over `.orchestrator/runtime/sessions/*.yaml` plus the related
handoff state. Produces a candidate list with a single locked recommendation
token per candidate so an operator can decide which sessions need attention.

V1 invariants (locked by handoff `orch-session-cleanup-policy-v1`):

- Read-only. No YAML mutation, no subprocess, no tmux. The module only reads
  YAML files that are already authoritative state.
- Recommendation tokens come from a closed enum:
  ``needs_worker_complete``, ``needs_cto_review``, ``needs_session_checkpoint``,
  ``awaiting_review_evidence``, ``leftover_after_complete``, ``parse_error``.
- CTO review-pending or rework-pending sessions never receive a kill-implying
  recommendation. They get ``needs_cto_review`` or ``awaiting_review_evidence``.
- ``leftover_after_complete`` is the only token that signals a candidate for
  operator-led tmux kill — it is NEVER an instruction to auto-kill.
- The report is informational, not authoritative state. CLI wiring is a V1.5
  follow-up; in V1 the report is callable from Python only.
"""
from __future__ import annotations

import os
from datetime import datetime, timezone
from typing import Dict, List, Optional

from . import storage
from .handoffs import _derive_review_state
from .validators import is_slug_safe


# ---------------------------------------------------------------------------
# Locked recommendation tokens
# ---------------------------------------------------------------------------

# Drift in this set requires a coordinated handoff contract bump. The names
# mirror the routing rules documented in ``docs/session-cleanup-policy.md``.
NEEDS_WORKER_COMPLETE = "needs_worker_complete"
NEEDS_CTO_REVIEW = "needs_cto_review"
NEEDS_SESSION_CHECKPOINT = "needs_session_checkpoint"
AWAITING_REVIEW_EVIDENCE = "awaiting_review_evidence"
LEFTOVER_AFTER_COMPLETE = "leftover_after_complete"
PARSE_ERROR = "parse_error"

RECOMMENDATION_TOKENS = frozenset({
    NEEDS_WORKER_COMPLETE,
    NEEDS_CTO_REVIEW,
    NEEDS_SESSION_CHECKPOINT,
    AWAITING_REVIEW_EVIDENCE,
    LEFTOVER_AFTER_COMPLETE,
    PARSE_ERROR,
})

# Forbidden tokens — a defensive grep target. None of these may appear in the
# rendered markdown. They are recorded here as a regression magnet for future
# refactors that might be tempted to re-introduce kill-implying language.
FORBIDDEN_TOKENS = frozenset({
    "auto_kill",
    "safe_to_kill",
    "archive_ready",
    "can_archive",
    "green_light",
    "ready_for_archive",
    "auto_archive_eligible",
})

# Invariants we surface inside the report so operators reading the markdown
# know what V1 promises and what it explicitly does not do.
_INVARIANTS_ACKNOWLEDGED = (
    "V1 is read-only / report-only.",
    "No automatic tmux kill, YAML mutation, or checkpoint generation.",
    "CTO review-pending or rework-pending sessions are never recommended for kill.",
    "Operator-led kill is the only sanctioned path in V1.",
    "The report is informational; authoritative state remains the YAML files.",
)


# ---------------------------------------------------------------------------
# Time helpers
# ---------------------------------------------------------------------------

def _parse_iso_timestamp(raw):
    """Parse a YAML timestamp value into an aware UTC datetime.

    Accepts the ISO-Z format we write, naive ISO strings, and ``datetime``
    instances yielded by ``yaml.safe_load``. Returns ``None`` when the value
    cannot be interpreted — caller treats absence as "no signal".
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
    if s.endswith("Z"):
        s = s[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(s)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def _compute_idle_minutes(session_dict, now_utc):
    """Return idle minutes (float) using newest of last_active_at / heartbeat_at.

    If neither timestamp is present, returns ``None`` so the caller can treat
    absence distinctly from "stale". Negative values clamp to 0 — a clock
    skew should never read as "negative idle".
    """
    last_active = _parse_iso_timestamp(session_dict.get("last_active_at"))
    heartbeat = _parse_iso_timestamp(session_dict.get("heartbeat_at"))
    candidates = [t for t in (last_active, heartbeat) if t is not None]
    if not candidates:
        return None
    newest = max(candidates)
    delta_seconds = (now_utc - newest).total_seconds()
    if delta_seconds < 0:
        delta_seconds = 0.0
    return delta_seconds / 60.0


# ---------------------------------------------------------------------------
# Checkpoint presence (read-only filesystem listing)
# ---------------------------------------------------------------------------

def _has_checkpoint(session_id):
    """Return True iff a checkpoint file exists for ``session_id``.

    Read-only listing of ``.orchestrator/runtime/checkpoints/`` — we never
    open, mutate, or re-write the directory. ``session_id`` is validated
    slug-safe before any prefix match so a tampered session row cannot
    influence path matching.
    """
    if not session_id or not is_slug_safe(session_id):
        return False
    checkpoints_dir = os.path.join(storage.RUNTIME_DIR, "checkpoints")
    if not os.path.isdir(checkpoints_dir):
        return False
    prefix = session_id + "-"
    try:
        for fname in os.listdir(checkpoints_dir):
            if fname.endswith(".md") and fname.startswith(prefix):
                return True
    except OSError:
        return False
    return False


# ---------------------------------------------------------------------------
# Recommendation routing
# ---------------------------------------------------------------------------

def _classify(session_dict, related_handoff_status, related_review_state,
              idle_minutes, threshold_minutes, has_checkpoint):
    """Pick a single recommendation token per the locked routing rules.

    Order matters. Review-pending / changes-requested are checked before any
    rule that could imply "this session is done", which protects the
    invariant that CTO-review-pending sessions never receive a kill-implying
    recommendation.

    Returns a token from ``RECOMMENDATION_TOKENS`` or ``None`` when the
    session is not a cleanup candidate.
    """
    session_status = session_dict.get("status")

    # Rule 1: changes_requested → never kill-implying.
    if related_review_state == "changes_requested":
        return AWAITING_REVIEW_EVIDENCE

    # Rule 2: completed + pending_review → operator review needed.
    if related_handoff_status == "completed" and related_review_state == "pending_review":
        return NEEDS_CTO_REVIEW

    # Rule 3: completed + approved + busy → leftover (operator-led kill candidate).
    if (
        related_handoff_status == "completed"
        and related_review_state == "approved"
        and session_status == "busy"
    ):
        return LEFTOVER_AFTER_COMPLETE

    # Rule 4: open + busy + idle ≥ threshold → worker should complete.
    if (
        related_handoff_status == "open"
        and session_status == "busy"
        and idle_minutes is not None
        and idle_minutes >= threshold_minutes
    ):
        return NEEDS_WORKER_COMPLETE

    # Rule 5: busy + idle ≥ threshold + no checkpoint → ask for a checkpoint.
    if (
        session_status == "busy"
        and idle_minutes is not None
        and idle_minutes >= threshold_minutes
        and not has_checkpoint
    ):
        return NEEDS_SESSION_CHECKPOINT

    return None


def _parse_error_candidate(session_id, note):
    """Build a candidate dict for an unparseable session or unsafe handoff_id ref."""
    return {
        "session_id": session_id,
        "peer_id": None,
        "status": None,
        "room_id": None,
        "handoff_id": None,
        "idle_minutes": None,
        "related_handoff_status": None,
        "related_review_state": None,
        "recommendation_token": PARSE_ERROR,
        "note": note,
    }


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def build_cleanup_report(rooms_filter: Optional[List[str]] = None,
                         idle_minutes: int = 60) -> Dict:
    """Build the V1 session cleanup report (pure compute, no writes).

    Args:
        rooms_filter: When provided, only sessions whose ``room_id`` appears
            in the iterable are considered. ``None`` means all rooms.
        idle_minutes: Idle threshold in minutes for rules that gate on
            staleness (``needs_worker_complete``, ``needs_session_checkpoint``).

    Returns:
        ``{"generated_at": iso8601, "threshold_minutes": int,
           "candidates": [...], "invariants_acknowledged": [...]}``

        Each candidate has the locked field set:
        ``session_id``, ``peer_id``, ``status``, ``room_id``, ``handoff_id``,
        ``idle_minutes``, ``related_handoff_status``, ``related_review_state``,
        ``recommendation_token`` (locked enum) plus an optional ``note``.

    The report only contains sessions that received a recommendation — i.e.
    sessions that warrant operator attention. Sessions in good standing are
    omitted to keep the report focused.
    """
    now_utc = datetime.now(timezone.utc)
    threshold_minutes = max(0, int(idle_minutes))
    rooms_filter_set = set(rooms_filter) if rooms_filter is not None else None

    candidates = []

    if not os.path.isdir(storage.SESSIONS_DIR):
        return {
            "generated_at": now_utc.strftime("%Y-%m-%dT%H:%M:%SZ"),
            "threshold_minutes": threshold_minutes,
            "candidates": candidates,
            "invariants_acknowledged": list(_INVARIANTS_ACKNOWLEDGED),
        }

    for fname in sorted(os.listdir(storage.SESSIONS_DIR)):
        if not fname.endswith(".yaml") or fname == ".gitkeep":
            continue
        session_id_from_name = fname[:-5]
        path = os.path.join(storage.SESSIONS_DIR, fname)

        try:
            state = storage.read_state(path)
            if not isinstance(state, dict):
                raise ValueError("not a mapping")
            s = state.get("session")
            if not isinstance(s, dict):
                raise ValueError("missing 'session' section")
        except Exception as e:  # pragma: no cover - exception text varies
            candidates.append(_parse_error_candidate(
                session_id_from_name,
                f"session YAML failed to parse: {e}",
            ))
            continue

        room_id = s.get("room_id")
        if rooms_filter_set is not None and room_id not in rooms_filter_set:
            continue

        session_id = s.get("id") or session_id_from_name
        raw_handoff_id = s.get("handoff_id")

        # Reject path-traversal payloads in handoff_id BEFORE constructing any
        # filesystem path. Mirrors the gc-audit / idle-snapshot invariant.
        if raw_handoff_id is not None and not is_slug_safe(raw_handoff_id):
            candidates.append({
                "session_id": session_id,
                "peer_id": s.get("peer_id"),
                "status": s.get("status"),
                "room_id": room_id,
                "handoff_id": None,
                "idle_minutes": None,
                "related_handoff_status": None,
                "related_review_state": None,
                "recommendation_token": PARSE_ERROR,
                "note": "handoff_id ref is not slug-safe; binding dropped",
            })
            continue

        handoff_id = raw_handoff_id

        related_handoff_status = None
        related_review_state = None
        if handoff_id:
            handoff_path = storage.handoff_path(handoff_id)
            if os.path.isfile(handoff_path):
                try:
                    handoff_state = storage.read_state(handoff_path)
                    if isinstance(handoff_state, dict) and "handoff" in handoff_state:
                        related_handoff_status = handoff_state["handoff"].get("status")
                        related_review_state = _derive_review_state(handoff_state)
                except Exception:
                    # Handoff file unreadable: leave both fields as None.
                    # We do not promote this to parse_error on the session
                    # itself — the session row is fine, the linked handoff is
                    # what is corrupt.
                    pass

        idle_min = _compute_idle_minutes(s, now_utc)
        has_cp = _has_checkpoint(session_id)

        token = _classify(
            s, related_handoff_status, related_review_state,
            idle_min, threshold_minutes, has_cp,
        )
        if token is None:
            continue

        candidates.append({
            "session_id": session_id,
            "peer_id": s.get("peer_id"),
            "status": s.get("status"),
            "room_id": room_id,
            "handoff_id": handoff_id,
            "idle_minutes": (
                round(idle_min, 2) if idle_min is not None else None
            ),
            "related_handoff_status": related_handoff_status,
            "related_review_state": related_review_state,
            "recommendation_token": token,
        })

    return {
        "generated_at": now_utc.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "threshold_minutes": threshold_minutes,
        "candidates": candidates,
        "invariants_acknowledged": list(_INVARIANTS_ACKNOWLEDGED),
    }


# ---------------------------------------------------------------------------
# Markdown rendering
# ---------------------------------------------------------------------------

def _fmt(value):
    if value is None or value == "":
        return "(none)"
    return str(value)


def render_markdown(report: Dict) -> str:
    """Render a build_cleanup_report() result as operator-facing markdown.

    Output is safe to grep for the locked recommendation tokens — no other
    token shapes appear in the recommendation column.
    """
    if not isinstance(report, dict):
        raise TypeError("report must be a dict produced by build_cleanup_report")

    generated_at = report.get("generated_at") or "(unknown)"
    threshold = report.get("threshold_minutes")
    candidates = report.get("candidates") or []
    invariants = report.get("invariants_acknowledged") or list(_INVARIANTS_ACKNOWLEDGED)

    lines = [
        "# Session Cleanup Report",
        "",
        "> Read-only V1. Informational; authoritative state remains in YAML.",
        "> No tmux kill or YAML mutation is performed by this report.",
        "",
        f"- **Generated at:** {generated_at}",
        f"- **Idle threshold (minutes):** {_fmt(threshold)}",
        f"- **Candidate count:** {len(candidates)}",
        "",
        "## Invariants Acknowledged",
        "",
    ]
    for inv in invariants:
        lines.append(f"- {inv}")
    lines.append("")

    lines.append("## Candidates")
    lines.append("")
    if not candidates:
        lines.append("- (none)")
        lines.append("")
    else:
        for c in candidates:
            sid = _fmt(c.get("session_id"))
            token = c.get("recommendation_token")
            # Defense-in-depth: drop unexpected tokens before they hit output.
            if token not in RECOMMENDATION_TOKENS:
                continue
            lines.append(f"### `{sid}`")
            lines.append("")
            lines.append(f"- **Peer:** {_fmt(c.get('peer_id'))}")
            lines.append(f"- **Session status:** {_fmt(c.get('status'))}")
            lines.append(f"- **Room:** {_fmt(c.get('room_id'))}")
            lines.append(f"- **Handoff:** {_fmt(c.get('handoff_id'))}")
            idle_val = c.get("idle_minutes")
            if idle_val is None:
                lines.append("- **Idle minutes:** (unknown)")
            else:
                lines.append(f"- **Idle minutes:** {idle_val}")
            lines.append(
                f"- **Related handoff status:** "
                f"{_fmt(c.get('related_handoff_status'))}"
            )
            lines.append(
                f"- **Related review state:** "
                f"{_fmt(c.get('related_review_state'))}"
            )
            lines.append(f"- **Recommendation:** `{token}`")
            note = c.get("note")
            if note:
                lines.append(f"- **Note:** {note}")
            lines.append("")

    lines.append("---")
    lines.append(
        "*Operator-led kill is the only sanctioned cleanup action in V1. "
        "This report does not authorize automated archival, kill, or compaction.*"
    )
    return "\n".join(lines) + "\n"
