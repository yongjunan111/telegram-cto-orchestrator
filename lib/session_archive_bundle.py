"""V2 session archive bundle writer and session YAML marker stamper.

Pure file IO and dict serialization. Touches session YAML only — never room or
handoff YAML. No external process invocation, no network, no derived-artifact
mutation outside the locked archive path.
"""
import os
from datetime import datetime, timezone
from typing import Any, Dict, Tuple

import yaml

from . import storage


SESSION_ARCHIVES_RELATIVE = os.path.join("runtime", "session-archives")

_BUNDLE_KEYS = (
    "session_summary",
    "room_summary",
    "handoff_summary",
    "completion_state",
    "review_state",
    "worker_evidence",
    "completion_note",
    "checkpoint_refs",
    "gc_audit_or_idle_snapshot_refs",
    "git_info",
    "next_action",
    "wiki_candidates",
)


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _archives_root(repo_root: str) -> str:
    return os.path.join(repo_root, ".orchestrator", SESSION_ARCHIVES_RELATIVE)


def _extract_session_id(validated_context: Dict) -> str:
    sid = validated_context.get("session_id")
    if sid:
        return sid
    summary = validated_context.get("session_summary") or {}
    if isinstance(summary, dict):
        if summary.get("id"):
            return summary["id"]
        if summary.get("session_id"):
            return summary["session_id"]
    state = validated_context.get("session_state") or {}
    if isinstance(state, dict):
        sect = state.get("session") or {}
        if isinstance(sect, dict) and sect.get("id"):
            return sect["id"]
    raise ValueError("validated_context missing session_id")


def _enrich_validated_context(ctx: Dict) -> Dict:
    """Return a new dict with bundle keys derived from validator-supplied fields.

    Idempotent and additive: if a key is already non-None in *ctx*, it is kept
    as-is.  Only missing / None keys are filled from the validator fields
    (session_state, handoff_state, room_state, git, report_path).
    Never mutates the caller's dict.
    """
    result = dict(ctx)

    session_state = ctx.get("session_state") or {}
    handoff_state = ctx.get("handoff_state") or {}
    room_state = ctx.get("room_state") or {}
    git = ctx.get("git") or {}
    report_path = ctx.get("report_path")

    # --- session_summary ---
    if result.get("session_summary") is None:
        sess = session_state.get("session") or {} if isinstance(session_state, dict) else {}
        if sess and isinstance(sess, dict):
            keys = ("id", "peer_id", "status", "mode", "room_id", "handoff_id",
                    "last_active_at", "lease_until", "dirty")
            result["session_summary"] = {k: sess[k] for k in keys if k in sess}
        else:
            result["session_summary"] = {}

    # --- room_summary ---
    if result.get("room_summary") is None:
        room = room_state.get("room") or {} if isinstance(room_state, dict) else {}
        lifecycle = room_state.get("lifecycle") or {} if isinstance(room_state, dict) else {}
        if room and isinstance(room, dict):
            summary: Dict[str, Any] = {}
            for k in ("id", "name", "status"):
                if k in room:
                    summary[k] = room[k]
            if isinstance(lifecycle, dict) and "current_phase" in lifecycle:
                summary["current_phase"] = lifecycle["current_phase"]
            result["room_summary"] = summary
        else:
            result["room_summary"] = {}

    # --- handoff_summary ---
    if result.get("handoff_summary") is None:
        handoff = handoff_state.get("handoff") or {} if isinstance(handoff_state, dict) else {}
        if handoff and isinstance(handoff, dict):
            result["handoff_summary"] = {
                k: handoff[k]
                for k in ("id", "status", "kind", "priority", "from", "to")
                if k in handoff
            }
        else:
            result["handoff_summary"] = {}

    # --- completion_state ---
    if result.get("completion_state") is None:
        handoff = handoff_state.get("handoff") or {} if isinstance(handoff_state, dict) else {}
        resolution = handoff_state.get("resolution") or {} if isinstance(handoff_state, dict) else {}
        status = handoff.get("status") if isinstance(handoff, dict) else None
        if status == "completed" and isinstance(resolution, dict):
            result["completion_state"] = {
                k: resolution[k]
                for k in ("status", "completed_at", "completed_by", "summary")
                if k in resolution
            } or {"status": status}
        elif status:
            result["completion_state"] = {"status": status}
        else:
            result["completion_state"] = {}

    # --- review_state ---
    if result.get("review_state") is None:
        # Three known layouts (canonical top-level first, then nested fallbacks)
        review = None
        if isinstance(handoff_state, dict):
            top = handoff_state.get("review")
            if isinstance(top, dict):
                review = top
        if review is None and isinstance(handoff_state, dict):
            resolution = handoff_state.get("resolution")
            if isinstance(resolution, dict):
                res_review = resolution.get("review")
                if isinstance(res_review, dict):
                    review = res_review
        if review is None and isinstance(handoff_state, dict):
            handoff = handoff_state.get("handoff")
            if isinstance(handoff, dict):
                hr = handoff.get("review")
                if isinstance(hr, dict):
                    review = hr
        if review:
            result["review_state"] = {
                k: review[k]
                for k in ("outcome", "reviewer", "reviewed_by", "reviewed_at", "note")
                if k in review
            }
        else:
            result["review_state"] = {}

    # --- worker_evidence ---
    if result.get("worker_evidence") is None:
        resolution = handoff_state.get("resolution") or {} if isinstance(handoff_state, dict) else {}
        evidence = resolution.get("verification") if isinstance(resolution, dict) else None
        result["worker_evidence"] = list(evidence) if isinstance(evidence, list) else []

    # --- git_info ---
    if result.get("git_info") is None:
        if git and isinstance(git, dict):
            result["git_info"] = {
                k: git[k]
                for k in ("head_sha", "worktree_dirty")
                if k in git
            }
        else:
            result["git_info"] = {}

    # --- next_action ---
    if result.get("next_action") is None:
        lifecycle = room_state.get("lifecycle") or {} if isinstance(room_state, dict) else {}
        result["next_action"] = lifecycle.get("next_action", "") if isinstance(lifecycle, dict) else ""

    # --- wiki_candidates ---
    if result.get("wiki_candidates") is None:
        execution = handoff_state.get("execution") or {} if isinstance(handoff_state, dict) else {}
        wiki_suggest = execution.get("wiki_suggest") or {} if isinstance(execution, dict) else {}
        hints = wiki_suggest.get("generated_hints") if isinstance(wiki_suggest, dict) else None
        result["wiki_candidates"] = list(hints) if isinstance(hints, list) else []

    # --- checkpoint_refs ---
    if result.get("checkpoint_refs") is None:
        result["checkpoint_refs"] = []

    # --- gc_audit_or_idle_snapshot_refs ---
    if result.get("gc_audit_or_idle_snapshot_refs") is None:
        result["gc_audit_or_idle_snapshot_refs"] = (
            [report_path] if report_path else []
        )

    # --- completion_note ---
    if result.get("completion_note") is None:
        resolution = handoff_state.get("resolution") or {} if isinstance(handoff_state, dict) else {}
        result["completion_note"] = (
            resolution.get("completion_note", "")
            if isinstance(resolution, dict)
            else ""
        )

    return result


def _build_bundle_payload(validated_context: Dict) -> Dict[str, Any]:
    return {key: validated_context.get(key) for key in _BUNDLE_KEYS}


def _resolve_collision_basenames(session_dir: str, timestamp: str):
    """Return (yaml_basename, md_basename) such that neither file exists yet.

    If session_dir does not exist, the bare timestamp names are safe to use.
    """
    if not os.path.isdir(session_dir):
        return f"{timestamp}.yaml", f"{timestamp}.md"
    yaml_p = os.path.join(session_dir, f"{timestamp}.yaml")
    md_p = os.path.join(session_dir, f"{timestamp}.md")
    if not os.path.exists(yaml_p) and not os.path.exists(md_p):
        return f"{timestamp}.yaml", f"{timestamp}.md"
    suffix = 1
    while True:
        y = os.path.join(session_dir, f"{timestamp}-{suffix}.yaml")
        m = os.path.join(session_dir, f"{timestamp}-{suffix}.md")
        if not os.path.exists(y) and not os.path.exists(m):
            return f"{timestamp}-{suffix}.yaml", f"{timestamp}-{suffix}.md"
        suffix += 1


def _md_section(lines, title, value) -> None:
    lines.append(f"## {title}")
    lines.append("")
    if value is None:
        lines.append("(none)")
    elif isinstance(value, list):
        if not value:
            lines.append("(empty)")
        else:
            for item in value:
                if isinstance(item, dict):
                    parts = ", ".join(f"{k}={v}" for k, v in item.items())
                    lines.append(f"- {parts}")
                else:
                    lines.append(f"- {item}")
    elif isinstance(value, dict):
        if not value:
            lines.append("(empty)")
        else:
            for k, v in value.items():
                lines.append(f"- **{k}:** {v}")
    else:
        lines.append(str(value))
    lines.append("")


def _render_bundle_md(session_id: str, archived_at: str, payload: Dict) -> str:
    lines = []
    lines.append(f"# Session Archive: {session_id}")
    lines.append("")
    lines.append(f"- **Archived at:** {archived_at}")
    lines.append("")
    _md_section(lines, "Session Summary", payload.get("session_summary"))
    _md_section(lines, "Room Summary", payload.get("room_summary"))
    _md_section(lines, "Handoff Summary", payload.get("handoff_summary"))
    _md_section(lines, "Completion State", payload.get("completion_state"))
    _md_section(lines, "Review State", payload.get("review_state"))
    _md_section(lines, "Worker Evidence", payload.get("worker_evidence"))
    _md_section(lines, "Completion Note", payload.get("completion_note"))
    _md_section(lines, "Checkpoint Refs", payload.get("checkpoint_refs"))
    _md_section(
        lines,
        "GC Audit / Idle Snapshot Refs",
        payload.get("gc_audit_or_idle_snapshot_refs"),
    )
    _md_section(lines, "Git Info", payload.get("git_info"))
    _md_section(lines, "Next Action", payload.get("next_action"))
    _md_section(lines, "Wiki Candidates", payload.get("wiki_candidates"))
    lines.append("---")
    lines.append(
        "*Derived archive bundle. Source of truth: session/room/handoff YAML at archive time.*"
    )
    return "\n".join(lines) + "\n"


def write_archive_bundle(
    validated_context: Dict, repo_root: str
) -> Tuple[str, str]:
    """Write archive bundle YAML+MD pair under the locked archive path.

    Both files share the same basename (UTC ISO timestamp, second precision).
    Returns (yaml_abs_path, md_abs_path).
    """
    if not isinstance(validated_context, dict):
        raise ValueError("validated_context must be a dict")
    if not repo_root:
        raise ValueError("repo_root must not be empty")

    session_id = _extract_session_id(validated_context)
    timestamp = _utc_now_iso()
    enriched = _enrich_validated_context(validated_context)
    payload = _build_bundle_payload(enriched)

    base_dir = _archives_root(repo_root)
    session_dir = os.path.join(base_dir, session_id)
    yaml_basename, md_basename = _resolve_collision_basenames(session_dir, timestamp)
    yaml_target = os.path.join(session_dir, yaml_basename)
    md_target = os.path.join(session_dir, md_basename)

    yaml_payload: Dict[str, Any] = dict(payload)
    yaml_payload["archived_at"] = timestamp
    yaml_payload["session_id"] = session_id

    yaml_text = yaml.safe_dump(
        yaml_payload,
        default_flow_style=False,
        allow_unicode=True,
        sort_keys=True,
    )
    md_text = _render_bundle_md(session_id, timestamp, payload)

    storage.safe_write_text(base_dir, yaml_target, yaml_text)
    storage.safe_write_text(base_dir, md_target, md_text)

    return os.path.realpath(yaml_target), os.path.realpath(md_target)


def stamp_session_archive_marker(
    session_id: str,
    archive_yaml_path: str,
    from_report_path: str,
) -> None:
    """Append archive marker to .orchestrator/runtime/sessions/<session-id>.yaml.

    Touches session YAML only. Atomic via storage.write_state's tempfile +
    os.replace path. Does not mutate room or handoff YAML.
    """
    if not session_id:
        raise ValueError("session_id must not be empty")
    if not archive_yaml_path:
        raise ValueError("archive_yaml_path must not be empty")
    if not from_report_path:
        raise ValueError("from_report_path must not be empty")

    sess_path = storage.session_path(session_id)
    state = storage.read_state(sess_path)
    if not isinstance(state, dict):
        raise ValueError(f"session '{session_id}' state is not a mapping")
    if "session" not in state or not isinstance(state["session"], dict):
        raise ValueError(f"session '{session_id}' state missing 'session' section")

    repo_root = os.path.dirname(storage.ORCHESTRATOR_DIR)
    abs_archive = os.path.abspath(archive_yaml_path)
    try:
        rel_archive = os.path.relpath(abs_archive, repo_root)
    except ValueError:
        rel_archive = abs_archive

    state["session"]["archive"] = {
        "status": "archived",
        "archived_at": _utc_now_iso(),
        "archive_path": rel_archive,
        "from_report": os.path.realpath(from_report_path),
    }

    storage.write_state(sess_path, state)
