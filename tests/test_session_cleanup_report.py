"""Tests for ``lib.session_cleanup`` V1 (read-only cleanup report).

The module is locked to a specific recommendation enum and a small set of
routing rules. These tests guard the routing rules, the forbidden-word
invariant, and the read-only contract (no YAML mutation, no subprocess
spawn, no tmux call).
"""
import hashlib
import os
import re
import subprocess
import sys

import pytest
import yaml

_REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), os.pardir))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

from lib import session_cleanup, storage  # noqa: E402


# ---------------------------------------------------------------------------
# Hermetic fixture helpers
# ---------------------------------------------------------------------------

def _patch_storage(monkeypatch, base_dir):
    orch_dir = os.path.join(base_dir, ".orchestrator")
    rooms_dir = os.path.join(orch_dir, "rooms")
    handoffs_dir = os.path.join(orch_dir, "handoffs")
    runtime_dir = os.path.join(orch_dir, "runtime")
    sessions_dir = os.path.join(runtime_dir, "sessions")
    checkpoints_dir = os.path.join(runtime_dir, "checkpoints")

    monkeypatch.setattr(storage, "ORCHESTRATOR_DIR", orch_dir)
    monkeypatch.setattr(storage, "ROOMS_DIR", rooms_dir)
    monkeypatch.setattr(storage, "HANDOFFS_DIR", handoffs_dir)
    monkeypatch.setattr(storage, "RUNTIME_DIR", runtime_dir)
    monkeypatch.setattr(storage, "SESSIONS_DIR", sessions_dir)

    for d in (sessions_dir, handoffs_dir, rooms_dir, checkpoints_dir):
        os.makedirs(d, exist_ok=True)


def _write_handoff(base_dir, handoff_id, room_id, status="open",
                   review_outcome=None):
    state = {
        "handoff": {
            "id": handoff_id,
            "room_id": room_id,
            "from": "orchestrator",
            "to": "test-worker",
            "status": status,
            "priority": "medium",
            "kind": "implementation",
        },
        "task": {"description": "x"},
        "timestamps": {"created_at": "2026-01-01T00:00:00Z"},
    }
    if review_outcome is not None:
        state["review"] = {"outcome": review_outcome, "reviewed_by": "cto"}
    if status == "completed":
        state["resolution"] = {"summary": "done", "completed_by": "test-worker"}
    path = os.path.join(
        base_dir, ".orchestrator", "handoffs", f"{handoff_id}.yaml"
    )
    with open(path, "w") as f:
        yaml.dump(state, f)


def _write_session(base_dir, session_id, room_id, handoff_id,
                   status="busy", peer_id="test-worker",
                   last_active_at="2020-01-01T00:00:00Z",
                   heartbeat_at="2020-01-01T00:00:00Z",
                   raw_overrides=None):
    state = {
        "session": {
            "id": session_id,
            "peer_id": peer_id,
            "room_id": room_id,
            "handoff_id": handoff_id,
            "tmux_session": f"sess-{session_id}",
            "tmux_target": "%1",
            "mode": "ephemeral",
            "status": status,
            "last_active_at": last_active_at,
            "heartbeat_at": heartbeat_at,
        }
    }
    if raw_overrides:
        state["session"].update(raw_overrides)
    path = os.path.join(
        base_dir, ".orchestrator", "runtime", "sessions",
        f"{session_id}.yaml",
    )
    with open(path, "w") as f:
        yaml.dump(state, f)


def _write_checkpoint(base_dir, session_id, suffix="manual"):
    path = os.path.join(
        base_dir, ".orchestrator", "runtime", "checkpoints",
        f"{session_id}-{suffix}-2026-04-25T00-00-00Z.md",
    )
    with open(path, "w") as f:
        f.write("# checkpoint\n")


def _hash_orch_yaml_tree(base_dir):
    orch = os.path.join(base_dir, ".orchestrator")
    snap = {}
    for root, _dirs, files in os.walk(orch):
        for fn in files:
            if not fn.endswith(".yaml"):
                continue
            p = os.path.join(root, fn)
            with open(p, "rb") as fh:
                snap[os.path.relpath(p, orch)] = hashlib.sha256(
                    fh.read()
                ).hexdigest()
    return snap


def _candidate_for(report, session_id):
    for c in report["candidates"]:
        if c["session_id"] == session_id:
            return c
    return None


# ---------------------------------------------------------------------------
# Locked token enum
# ---------------------------------------------------------------------------

def test_recommendation_token_set_is_locked():
    expected = {
        "needs_worker_complete",
        "needs_cto_review",
        "needs_session_checkpoint",
        "awaiting_review_evidence",
        "leftover_after_complete",
        "parse_error",
    }
    assert set(session_cleanup.RECOMMENDATION_TOKENS) == expected


def test_forbidden_token_set_includes_all_kill_implying_words():
    """Drift-detector — the forbidden list must include every banned token
    the contract enumerates so that the markdown grep test covers them."""
    must_include = {
        "auto_kill", "safe_to_kill", "archive_ready", "can_archive",
        "green_light", "ready_for_archive", "auto_archive_eligible",
    }
    assert must_include.issubset(set(session_cleanup.FORBIDDEN_TOKENS))


# ---------------------------------------------------------------------------
# Routing rules
# ---------------------------------------------------------------------------

def test_open_busy_idle_yields_needs_worker_complete(monkeypatch, tmp_path):
    base_dir = str(tmp_path)
    _patch_storage(monkeypatch, base_dir)
    _write_handoff(base_dir, "h1", "r1", status="open")
    _write_session(base_dir, "s1", "r1", "h1", status="busy")

    report = session_cleanup.build_cleanup_report(
        rooms_filter=["r1"], idle_minutes=1
    )
    cand = _candidate_for(report, "s1")
    assert cand is not None
    assert cand["recommendation_token"] == "needs_worker_complete"
    assert cand["related_handoff_status"] == "open"


def test_completed_pending_review_yields_needs_cto_review(monkeypatch, tmp_path):
    base_dir = str(tmp_path)
    _patch_storage(monkeypatch, base_dir)
    _write_handoff(base_dir, "h1", "r1", status="completed")
    _write_session(base_dir, "s1", "r1", "h1", status="busy")

    report = session_cleanup.build_cleanup_report(
        rooms_filter=["r1"], idle_minutes=1
    )
    cand = _candidate_for(report, "s1")
    assert cand is not None
    assert cand["recommendation_token"] == "needs_cto_review"
    assert cand["related_review_state"] == "pending_review"


def test_completed_approved_busy_yields_leftover_after_complete(
    monkeypatch, tmp_path
):
    base_dir = str(tmp_path)
    _patch_storage(monkeypatch, base_dir)
    _write_handoff(base_dir, "h1", "r1", status="completed",
                   review_outcome="approved")
    _write_session(base_dir, "s1", "r1", "h1", status="busy")

    report = session_cleanup.build_cleanup_report(
        rooms_filter=["r1"], idle_minutes=1
    )
    cand = _candidate_for(report, "s1")
    assert cand is not None
    assert cand["recommendation_token"] == "leftover_after_complete"
    assert cand["related_review_state"] == "approved"


def test_changes_requested_yields_awaiting_review_evidence(
    monkeypatch, tmp_path
):
    base_dir = str(tmp_path)
    _patch_storage(monkeypatch, base_dir)
    _write_handoff(base_dir, "h1", "r1", status="completed",
                   review_outcome="changes_requested")
    _write_session(base_dir, "s1", "r1", "h1", status="busy")

    report = session_cleanup.build_cleanup_report(
        rooms_filter=["r1"], idle_minutes=1
    )
    cand = _candidate_for(report, "s1")
    assert cand is not None
    assert cand["recommendation_token"] == "awaiting_review_evidence"


def test_review_pending_never_gets_kill_implying_token(monkeypatch, tmp_path):
    """Invariant: a CTO-review-pending or rework-pending session must NEVER
    receive a kill-implying token (``leftover_after_complete``)."""
    base_dir = str(tmp_path)
    _patch_storage(monkeypatch, base_dir)
    _write_handoff(base_dir, "h-pending", "r1", status="completed")
    _write_handoff(base_dir, "h-changes", "r1", status="completed",
                   review_outcome="changes_requested")
    _write_session(base_dir, "s-pending", "r1", "h-pending", status="busy")
    _write_session(base_dir, "s-changes", "r1", "h-changes", status="busy")

    report = session_cleanup.build_cleanup_report(
        rooms_filter=["r1"], idle_minutes=1
    )
    by_id = {c["session_id"]: c for c in report["candidates"]}
    assert by_id["s-pending"]["recommendation_token"] == "needs_cto_review"
    assert by_id["s-changes"]["recommendation_token"] == "awaiting_review_evidence"
    for cand in report["candidates"]:
        assert cand["recommendation_token"] != "leftover_after_complete" or \
            cand["related_review_state"] == "approved"


def test_unsafe_handoff_id_yields_parse_error(monkeypatch, tmp_path):
    """A session YAML carrying a non-slug-safe handoff_id must classify as
    parse_error and MUST NOT pass the value into ``storage.handoff_path``."""
    base_dir = str(tmp_path)
    _patch_storage(monkeypatch, base_dir)
    _write_session(base_dir, "s1", "r1", handoff_id="../evil", status="busy")

    calls = []
    real_handoff_path = storage.handoff_path

    def _tracking(hid):
        calls.append(hid)
        return real_handoff_path(hid)

    monkeypatch.setattr(storage, "handoff_path", _tracking)

    report = session_cleanup.build_cleanup_report(
        rooms_filter=["r1"], idle_minutes=1
    )
    cand = _candidate_for(report, "s1")
    assert cand is not None
    assert cand["recommendation_token"] == "parse_error"
    assert "../evil" not in calls


def test_unparseable_session_yaml_yields_parse_error(monkeypatch, tmp_path):
    base_dir = str(tmp_path)
    _patch_storage(monkeypatch, base_dir)
    sessions_dir = os.path.join(
        base_dir, ".orchestrator", "runtime", "sessions"
    )
    with open(os.path.join(sessions_dir, "broken.yaml"), "w") as f:
        f.write("session: [this is: not valid\nyaml: {\n")

    report = session_cleanup.build_cleanup_report(idle_minutes=1)
    cand = _candidate_for(report, "broken")
    assert cand is not None
    assert cand["recommendation_token"] == "parse_error"


def test_busy_session_with_no_checkpoint_yields_needs_session_checkpoint(
    monkeypatch, tmp_path
):
    base_dir = str(tmp_path)
    _patch_storage(monkeypatch, base_dir)
    # Unbound handoff so the open/completed rules don't fire.
    _write_session(base_dir, "s1", "r1", handoff_id=None, status="busy")

    report = session_cleanup.build_cleanup_report(
        rooms_filter=["r1"], idle_minutes=1
    )
    cand = _candidate_for(report, "s1")
    assert cand is not None
    assert cand["recommendation_token"] == "needs_session_checkpoint"


def test_busy_session_with_checkpoint_present_does_not_get_checkpoint_token(
    monkeypatch, tmp_path
):
    base_dir = str(tmp_path)
    _patch_storage(monkeypatch, base_dir)
    _write_session(base_dir, "s1", "r1", handoff_id=None, status="busy")
    _write_checkpoint(base_dir, "s1")

    report = session_cleanup.build_cleanup_report(
        rooms_filter=["r1"], idle_minutes=1
    )
    cand = _candidate_for(report, "s1")
    # No checkpoint rule applies, no other rule matches → not a candidate.
    assert cand is None


def test_recent_session_below_threshold_is_not_a_candidate(
    monkeypatch, tmp_path
):
    base_dir = str(tmp_path)
    _patch_storage(monkeypatch, base_dir)
    _write_handoff(base_dir, "h1", "r1", status="open")
    # Use a far-future timestamp so the session always reads as recent.
    from datetime import datetime, timedelta, timezone
    far_future = (datetime.now(timezone.utc) + timedelta(days=365)).strftime(
        "%Y-%m-%dT%H:%M:%SZ"
    )
    _write_session(
        base_dir, "s1", "r1", "h1", status="busy",
        last_active_at=far_future, heartbeat_at=far_future,
    )

    report = session_cleanup.build_cleanup_report(
        rooms_filter=["r1"], idle_minutes=60
    )
    assert _candidate_for(report, "s1") is None


def test_rooms_filter_excludes_other_rooms(monkeypatch, tmp_path):
    base_dir = str(tmp_path)
    _patch_storage(monkeypatch, base_dir)
    _write_handoff(base_dir, "h-r1", "r1", status="open")
    _write_handoff(base_dir, "h-r2", "r2", status="open")
    _write_session(base_dir, "s-r1", "r1", "h-r1", status="busy")
    _write_session(base_dir, "s-r2", "r2", "h-r2", status="busy")

    report = session_cleanup.build_cleanup_report(
        rooms_filter=["r1"], idle_minutes=1
    )
    sids = {c["session_id"] for c in report["candidates"]}
    assert "s-r1" in sids
    assert "s-r2" not in sids


# ---------------------------------------------------------------------------
# Markdown rendering invariants
# ---------------------------------------------------------------------------

def test_render_markdown_passes_forbidden_words_grep(monkeypatch, tmp_path):
    base_dir = str(tmp_path)
    _patch_storage(monkeypatch, base_dir)
    # Mix of cases to exercise as many recommendation paths as possible.
    _write_handoff(base_dir, "h-open", "r1", status="open")
    _write_handoff(base_dir, "h-completed", "r1", status="completed")
    _write_handoff(base_dir, "h-approved", "r1", status="completed",
                   review_outcome="approved")
    _write_handoff(base_dir, "h-changes", "r1", status="completed",
                   review_outcome="changes_requested")
    _write_session(base_dir, "s-open", "r1", "h-open", status="busy")
    _write_session(base_dir, "s-completed", "r1", "h-completed", status="busy")
    _write_session(base_dir, "s-approved", "r1", "h-approved", status="busy")
    _write_session(base_dir, "s-changes", "r1", "h-changes", status="busy")
    _write_session(base_dir, "s-unbound", "r1", handoff_id=None, status="busy")

    report = session_cleanup.build_cleanup_report(
        rooms_filter=["r1"], idle_minutes=1
    )
    md = session_cleanup.render_markdown(report)

    pattern = re.compile(
        r"auto_kill|safe_to_kill|archive_ready|can_archive|"
        r"green_light|ready_for_archive|auto_archive_eligible"
    )
    assert pattern.search(md) is None, (
        f"forbidden token leaked into rendered markdown:\n{md}"
    )


def test_render_markdown_contains_locked_tokens_only(monkeypatch, tmp_path):
    base_dir = str(tmp_path)
    _patch_storage(monkeypatch, base_dir)
    _write_handoff(base_dir, "h-open", "r1", status="open")
    _write_handoff(base_dir, "h-changes", "r1", status="completed",
                   review_outcome="changes_requested")
    _write_session(base_dir, "s-open", "r1", "h-open", status="busy")
    _write_session(base_dir, "s-changes", "r1", "h-changes", status="busy")

    report = session_cleanup.build_cleanup_report(
        rooms_filter=["r1"], idle_minutes=1
    )
    md = session_cleanup.render_markdown(report)

    # Find every backticked token of the form `lower_snake_case`.
    backtick_tokens = set(re.findall(r"`([a-z_]+)`", md))
    rec_tokens = backtick_tokens & {
        t for t in backtick_tokens if t in session_cleanup.RECOMMENDATION_TOKENS
    }
    # Every recommendation token surfaced must be in the locked enum.
    assert rec_tokens.issubset(session_cleanup.RECOMMENDATION_TOKENS)


def test_render_markdown_emits_well_formed_output_when_empty():
    report = {
        "generated_at": "2026-04-25T00:00:00Z",
        "threshold_minutes": 60,
        "candidates": [],
        "invariants_acknowledged": ["a", "b"],
    }
    md = session_cleanup.render_markdown(report)
    assert md.startswith("# Session Cleanup Report")
    assert "(none)" in md


# ---------------------------------------------------------------------------
# Read-only contract (no mutation, no subprocess)
# ---------------------------------------------------------------------------

def test_build_cleanup_report_does_not_mutate_yaml(monkeypatch, tmp_path):
    base_dir = str(tmp_path)
    _patch_storage(monkeypatch, base_dir)
    _write_handoff(base_dir, "h1", "r1", status="open")
    _write_session(base_dir, "s1", "r1", "h1", status="busy")

    before = _hash_orch_yaml_tree(base_dir)
    session_cleanup.build_cleanup_report(rooms_filter=["r1"], idle_minutes=1)
    after = _hash_orch_yaml_tree(base_dir)

    assert before == after, (
        "build_cleanup_report mutated authoritative YAML "
        "(SHA-256 hashes diverged)"
    )


def test_build_cleanup_report_does_not_spawn_subprocess(monkeypatch, tmp_path):
    base_dir = str(tmp_path)
    _patch_storage(monkeypatch, base_dir)
    _write_handoff(base_dir, "h1", "r1", status="open")
    _write_session(base_dir, "s1", "r1", "h1", status="busy")

    calls = []

    def _trip(*args, **kwargs):
        calls.append(("subprocess", args, kwargs))
        raise RuntimeError("subprocess is forbidden in session_cleanup")

    monkeypatch.setattr(subprocess, "Popen", _trip)
    monkeypatch.setattr(subprocess, "run", _trip)
    monkeypatch.setattr(subprocess, "check_call", _trip)
    monkeypatch.setattr(subprocess, "check_output", _trip)

    session_cleanup.build_cleanup_report(rooms_filter=["r1"], idle_minutes=1)
    assert calls == []


def test_module_source_does_not_reference_tmux_or_subprocess():
    """Static guard: scan the module source for tmux / subprocess imports
    or call signatures, so accidental refactors get caught at the test
    layer. Filter out comments/docstrings — the module legitimately refers
    to "tmux kill" in prose to explain what it must NOT do."""
    src_path = os.path.join(_REPO_ROOT, "lib", "session_cleanup.py")
    with open(src_path) as f:
        source = f.read()

    # Strip docstrings and comments before grepping for forbidden call shapes.
    code = _strip_comments_and_docstrings(source)

    assert "import subprocess" not in code
    assert "from subprocess" not in code
    assert "import tmux" not in code

    # Tmux callouts as actual argv literals (quoted strings of "tmux ...").
    for forbidden in (
        '"tmux"', "'tmux'", '"tmux ', "'tmux ",
    ):
        assert forbidden not in code, (
            f"tmux argv literal {forbidden!r} present in session_cleanup.py"
        )


def _strip_comments_and_docstrings(source: str) -> str:
    """Remove triple-quoted strings (docstrings) and `#` line comments so
    static guards only inspect executable code."""
    import io
    import tokenize

    out_tokens = []
    try:
        tokens = list(tokenize.generate_tokens(io.StringIO(source).readline))
    except tokenize.TokenizeError:
        return source

    for tok in tokens:
        if tok.type == tokenize.COMMENT:
            continue
        if tok.type == tokenize.STRING and (
            tok.string.startswith('"""') or tok.string.startswith("'''")
        ):
            # Drop bare docstring tokens; keep regular string literals.
            continue
        out_tokens.append(tok)

    try:
        return tokenize.untokenize(out_tokens)
    except (ValueError, tokenize.TokenizeError):
        return source


def test_report_shape_has_required_top_level_keys(monkeypatch, tmp_path):
    base_dir = str(tmp_path)
    _patch_storage(monkeypatch, base_dir)
    report = session_cleanup.build_cleanup_report(idle_minutes=1)
    assert "generated_at" in report
    assert "threshold_minutes" in report
    assert "candidates" in report
    assert isinstance(report["candidates"], list)
    assert "invariants_acknowledged" in report
    assert isinstance(report["invariants_acknowledged"], list)
    assert len(report["invariants_acknowledged"]) >= 4


def test_each_candidate_has_locked_field_set(monkeypatch, tmp_path):
    base_dir = str(tmp_path)
    _patch_storage(monkeypatch, base_dir)
    _write_handoff(base_dir, "h1", "r1", status="open")
    _write_session(base_dir, "s1", "r1", "h1", status="busy")

    report = session_cleanup.build_cleanup_report(
        rooms_filter=["r1"], idle_minutes=1
    )
    required_keys = {
        "session_id", "peer_id", "status", "room_id", "handoff_id",
        "idle_minutes", "related_handoff_status", "related_review_state",
        "recommendation_token",
    }
    for cand in report["candidates"]:
        missing = required_keys - set(cand.keys())
        assert not missing, f"candidate missing required keys: {missing}"
        assert cand["recommendation_token"] in session_cleanup.RECOMMENDATION_TOKENS
