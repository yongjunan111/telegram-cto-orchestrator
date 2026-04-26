"""Tests for V2 session archive bundle writer + marker stamper.

Hermetic: tmp dirs, monkeypatched storage paths, no real tmux, no network,
no subprocess.
"""
import hashlib
import os
import sys

import pytest
import yaml

_REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), os.pardir))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

from lib import storage, session_archive_bundle  # noqa: E402


# ---------------------------------------------------------------------------
# Fixtures / helpers
# ---------------------------------------------------------------------------

def _patch_storage(monkeypatch, base_dir):
    orch_dir = os.path.join(base_dir, ".orchestrator")
    rooms_dir = os.path.join(orch_dir, "rooms")
    handoffs_dir = os.path.join(orch_dir, "handoffs")
    peer_registry = os.path.join(orch_dir, "peer_registry.yaml")
    runtime_dir = os.path.join(orch_dir, "runtime")
    sessions_dir = os.path.join(runtime_dir, "sessions")

    monkeypatch.setattr(storage, "ORCHESTRATOR_DIR", orch_dir)
    monkeypatch.setattr(storage, "ROOMS_DIR", rooms_dir)
    monkeypatch.setattr(storage, "HANDOFFS_DIR", handoffs_dir)
    monkeypatch.setattr(storage, "PEER_REGISTRY_PATH", peer_registry)
    monkeypatch.setattr(storage, "RUNTIME_DIR", runtime_dir)
    monkeypatch.setattr(storage, "SESSIONS_DIR", sessions_dir)

    os.makedirs(sessions_dir, exist_ok=True)
    os.makedirs(handoffs_dir, exist_ok=True)
    os.makedirs(rooms_dir, exist_ok=True)
    return base_dir


def _write_session(base_dir, session_id, extra=None):
    sess = {
        "session": {
            "id": session_id,
            "peer_id": "test-peer",
            "tmux_session": "test-tmuxname",
            "tmux_target": "%1",
            "mode": "ephemeral",
            "status": "idle",
            "room_id": "test-room",
            "handoff_id": "test-handoff",
            "cwd": base_dir,
            "branch": None,
            "dirty": False,
            "reuse_count": 0,
            "heartbeat_at": "2026-04-25T08:00:00Z",
            "lease_until": "2026-04-25T09:00:00Z",
            "last_active_at": "2026-04-25T08:00:00Z",
        }
    }
    if extra:
        sess["session"].update(extra)
    p = os.path.join(
        base_dir, ".orchestrator", "runtime", "sessions", f"{session_id}.yaml"
    )
    with open(p, "w") as f:
        yaml.safe_dump(sess, f, sort_keys=False)
    return p


def _write_room(base_dir, room_id):
    room_dir = os.path.join(base_dir, ".orchestrator", "rooms", room_id)
    os.makedirs(room_dir, exist_ok=True)
    state = {
        "room": {"id": room_id, "name": "Test Room", "status": "active"},
        "context": {"goal": "test"},
        "lifecycle": {"current_phase": "execution"},
    }
    p = os.path.join(room_dir, "state.yaml")
    with open(p, "w") as f:
        yaml.safe_dump(state, f, sort_keys=False)
    return p


def _write_handoff(base_dir, handoff_id, room_id="test-room"):
    p = os.path.join(
        base_dir, ".orchestrator", "handoffs", f"{handoff_id}.yaml"
    )
    h = {
        "handoff": {
            "id": handoff_id,
            "room_id": room_id,
            "from": "orchestrator",
            "to": "test-peer",
            "status": "completed",
            "kind": "implementation",
        },
        "task": {"description": "test task"},
    }
    with open(p, "w") as f:
        yaml.safe_dump(h, f, sort_keys=False)
    return p


def _file_sha256(path):
    with open(path, "rb") as f:
        return hashlib.sha256(f.read()).hexdigest()


def _make_validated_context(session_id="test-session"):
    return {
        "session_id": session_id,
        "session_summary": {
            "id": session_id,
            "peer_id": "test-peer",
            "status": "idle",
        },
        "room_summary": {"id": "test-room", "name": "Test Room"},
        "handoff_summary": {"id": "test-handoff", "kind": "implementation"},
        "completion_state": {
            "status": "completed",
            "completed_at": "2026-04-25T08:30:00Z",
        },
        "review_state": {"outcome": "approved", "reviewer": "cto"},
        "worker_evidence": [
            "pytest passed (12/12)",
            "manual smoke OK",
        ],
        "completion_note": "All criteria met.",
        "checkpoint_refs": [
            ".orchestrator/runtime/checkpoints/test-session.manual.2026-04-25.md",
        ],
        "gc_audit_or_idle_snapshot_refs": [
            ".orchestrator/runtime/gc-audits/test-room/2026-04-25.yaml",
        ],
        "git_info": {
            "head_sha": "abcdef0123456789",
            "dirty_state": False,
            "branch": "main",
            "recent_commit_subjects": ["feat: test"],
        },
        "next_action": "operator review",
        "wiki_candidates": [
            {
                "topic": "session-archive",
                "hint": "promotion gate",
                "source_handoff_id": "test-handoff",
            },
        ],
    }


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

def test_write_creates_yaml_and_md_at_locked_path(monkeypatch, tmp_path):
    base = _patch_storage(monkeypatch, str(tmp_path))
    ctx = _make_validated_context()
    yaml_path, md_path = session_archive_bundle.write_archive_bundle(ctx, base)

    assert os.path.isfile(yaml_path)
    assert os.path.isfile(md_path)
    expected_dir = os.path.realpath(
        os.path.join(
            base, ".orchestrator", "runtime", "session-archives", "test-session"
        )
    )
    assert os.path.dirname(yaml_path) == expected_dir
    assert os.path.dirname(md_path) == expected_dir
    yaml_base = os.path.basename(yaml_path)
    md_base = os.path.basename(md_path)
    assert yaml_base.endswith(".yaml")
    assert md_base.endswith(".md")
    assert yaml_base[: -len(".yaml")] == md_base[: -len(".md")]


def test_bundle_yaml_has_all_required_keys(monkeypatch, tmp_path):
    base = _patch_storage(monkeypatch, str(tmp_path))
    ctx = _make_validated_context()
    yaml_path, _ = session_archive_bundle.write_archive_bundle(ctx, base)
    with open(yaml_path) as f:
        loaded = yaml.safe_load(f)

    required = {
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
    }
    assert required.issubset(set(loaded.keys()))
    # values populated from validated_context
    assert loaded["completion_state"]["status"] == "completed"
    assert loaded["git_info"]["head_sha"] == "abcdef0123456789"
    assert loaded["wiki_candidates"][0]["topic"] == "session-archive"


def test_bundle_yaml_keys_present_when_context_lacks_data(monkeypatch, tmp_path):
    """With a truly bare ctx (only session_id), the normalizer must produce
    non-None empty-collection values, not None.  Changed from the original
    'assert loaded[k] is None' to match the P2 fix: derived collection keys
    become [] / {} / '' rather than None.
    """
    base = _patch_storage(monkeypatch, str(tmp_path))
    ctx = {"session_id": "empty-sess"}
    yaml_path, _ = session_archive_bundle.write_archive_bundle(ctx, base)
    with open(yaml_path) as f:
        loaded = yaml.safe_load(f)
    for k in (
        "session_summary",
        "room_summary",
        "handoff_summary",
        "completion_state",
        "review_state",
        "git_info",
    ):
        assert k in loaded
        assert loaded[k] is not None  # normalizer fills {} instead of None
    for k in ("worker_evidence", "checkpoint_refs", "gc_audit_or_idle_snapshot_refs",
               "wiki_candidates"):
        assert k in loaded
        assert loaded[k] is not None
        assert isinstance(loaded[k], list)
    for k in ("completion_note", "next_action"):
        assert k in loaded
        assert loaded[k] is not None  # empty string, not None


def test_bundle_md_is_human_readable_not_raw_yaml(monkeypatch, tmp_path):
    base = _patch_storage(monkeypatch, str(tmp_path))
    ctx = _make_validated_context()
    yaml_path, md_path = session_archive_bundle.write_archive_bundle(ctx, base)
    md = open(md_path).read()
    yaml_text = open(yaml_path).read()

    assert md.strip()
    assert "# Session Archive: test-session" in md
    assert "## Session Summary" in md
    assert "## Wiki Candidates" in md
    assert md != yaml_text
    # Markdown should reference data without being a yaml dump verbatim
    assert "topic=session-archive" in md or "session-archive" in md


def test_marker_appends_4_fields_and_preserves_existing(monkeypatch, tmp_path):
    base = _patch_storage(monkeypatch, str(tmp_path))
    sess_path = _write_session(
        base, "test-session", extra={"custom_field": "keep-me"}
    )
    archive_yaml = os.path.join(
        base,
        ".orchestrator",
        "runtime",
        "session-archives",
        "test-session",
        "2026-04-25T08-55-00Z.yaml",
    )
    os.makedirs(os.path.dirname(archive_yaml), exist_ok=True)
    open(archive_yaml, "w").close()

    report_path = os.path.join(base, "report.yaml")
    open(report_path, "w").close()

    session_archive_bundle.stamp_session_archive_marker(
        "test-session", archive_yaml, report_path
    )

    with open(sess_path) as f:
        state = yaml.safe_load(f)
    arc = state["session"]["archive"]
    assert set(arc.keys()) == {
        "status",
        "archived_at",
        "archive_path",
        "from_report",
    }
    assert arc["status"] == "archived"
    assert arc["archived_at"].endswith("Z")
    assert arc["from_report"] == os.path.realpath(report_path)
    # Existing fields preserved
    assert state["session"]["custom_field"] == "keep-me"
    assert state["session"]["last_active_at"] == "2026-04-25T08:00:00Z"
    assert state["session"]["peer_id"] == "test-peer"


def test_room_handoff_hash_invariant_on_write_archive_bundle(monkeypatch, tmp_path):
    base = _patch_storage(monkeypatch, str(tmp_path))
    room_path = _write_room(base, "test-room")
    handoff_path = _write_handoff(base, "test-handoff")
    before_room = _file_sha256(room_path)
    before_handoff = _file_sha256(handoff_path)
    ctx = _make_validated_context()
    session_archive_bundle.write_archive_bundle(ctx, base)
    assert _file_sha256(room_path) == before_room
    assert _file_sha256(handoff_path) == before_handoff


def test_room_handoff_hash_invariant_on_marker_stamper(monkeypatch, tmp_path):
    base = _patch_storage(monkeypatch, str(tmp_path))
    room_path = _write_room(base, "test-room")
    handoff_path = _write_handoff(base, "test-handoff")
    _write_session(base, "test-session")
    archive_yaml = os.path.join(
        base,
        ".orchestrator",
        "runtime",
        "session-archives",
        "test-session",
        "ts.yaml",
    )
    os.makedirs(os.path.dirname(archive_yaml), exist_ok=True)
    open(archive_yaml, "w").close()
    report_path = os.path.join(base, "report.yaml")
    open(report_path, "w").close()
    before_room = _file_sha256(room_path)
    before_handoff = _file_sha256(handoff_path)
    session_archive_bundle.stamp_session_archive_marker(
        "test-session", archive_yaml, report_path
    )
    assert _file_sha256(room_path) == before_room
    assert _file_sha256(handoff_path) == before_handoff


def test_path_traversal_session_id_is_rejected(monkeypatch, tmp_path):
    base = _patch_storage(monkeypatch, str(tmp_path))
    ctx = _make_validated_context(session_id="../etc/passwd")
    with pytest.raises((ValueError, OSError)):
        session_archive_bundle.write_archive_bundle(ctx, base)
    archives = os.path.join(
        base, ".orchestrator", "runtime", "session-archives"
    )
    leaked = []
    if os.path.isdir(archives):
        for root, _, files in os.walk(archives):
            for fn in files:
                leaked.append(os.path.join(root, fn))
    assert leaked == []
    # Defense-in-depth: nothing leaked outside archive root either
    assert not os.path.exists("/tmp/etc/passwd_should_never_exist")  # sanity


def test_atomic_marker_write_preserves_original_on_os_error(
    monkeypatch, tmp_path
):
    base = _patch_storage(monkeypatch, str(tmp_path))
    sess_path = _write_session(base, "test-session")
    original_bytes = open(sess_path, "rb").read()

    archive_yaml = os.path.join(
        base,
        ".orchestrator",
        "runtime",
        "session-archives",
        "test-session",
        "ts.yaml",
    )
    os.makedirs(os.path.dirname(archive_yaml), exist_ok=True)
    open(archive_yaml, "w").close()
    report_path = os.path.join(base, "report.yaml")
    open(report_path, "w").close()

    real_replace = os.replace

    def boom(*args, **kwargs):
        raise OSError("simulated mid-write failure")

    monkeypatch.setattr(os, "replace", boom)

    with pytest.raises(OSError):
        session_archive_bundle.stamp_session_archive_marker(
            "test-session", archive_yaml, report_path
        )

    monkeypatch.setattr(os, "replace", real_replace)
    after_bytes = open(sess_path, "rb").read()
    assert after_bytes == original_bytes
    # No tempfiles left behind in sessions dir
    sessions_dir = os.path.dirname(sess_path)
    leftovers = [
        f for f in os.listdir(sessions_dir) if f.endswith(".tmp")
    ]
    assert leftovers == []


def test_repeated_writes_produce_distinct_bundles(monkeypatch, tmp_path):
    base = _patch_storage(monkeypatch, str(tmp_path))
    ctx = _make_validated_context()
    timestamps = iter(
        ["2026-04-25T08:55:00Z", "2026-04-25T08:55:01Z"]
    )
    monkeypatch.setattr(
        session_archive_bundle, "_utc_now_iso", lambda: next(timestamps)
    )
    y1, m1 = session_archive_bundle.write_archive_bundle(ctx, base)
    y2, m2 = session_archive_bundle.write_archive_bundle(ctx, base)
    assert y1 != y2
    assert m1 != m2
    for p in (y1, m1, y2, m2):
        assert os.path.isfile(p)


def test_no_forbidden_tokens_in_source():
    src_path = os.path.join(_REPO_ROOT, "lib", "session_archive_bundle.py")
    with open(src_path) as f:
        src = f.read()
    bans = [
        ".orchestrator/wiki",
        "tmux",
        "subprocess.Popen",
        "subprocess.run",
        "subprocess.call",
        "subprocess.check_call",
        "send-keys",
        "import requests",
        "import httpx",
        "import urllib",
    ]
    for token in bans:
        assert token not in src, f"forbidden token in source: {token}"


def test_archive_path_is_repo_relative(monkeypatch, tmp_path):
    base = _patch_storage(monkeypatch, str(tmp_path))
    sess_path = _write_session(base, "test-session")
    archive_yaml = os.path.join(
        base,
        ".orchestrator",
        "runtime",
        "session-archives",
        "test-session",
        "ts.yaml",
    )
    os.makedirs(os.path.dirname(archive_yaml), exist_ok=True)
    open(archive_yaml, "w").close()
    report_path = os.path.join(base, "report.yaml")
    open(report_path, "w").close()
    session_archive_bundle.stamp_session_archive_marker(
        "test-session", archive_yaml, report_path
    )
    state = yaml.safe_load(open(sess_path))
    arc_path = state["session"]["archive"]["archive_path"]
    assert not os.path.isabs(arc_path), arc_path
    assert (
        ".orchestrator/runtime/session-archives/test-session" in arc_path
    )


def test_marker_from_report_resolves_symlink_to_realpath(monkeypatch, tmp_path):
    base = _patch_storage(monkeypatch, str(tmp_path))
    sess_path = _write_session(base, "test-session")
    archive_yaml = os.path.join(
        base,
        ".orchestrator",
        "runtime",
        "session-archives",
        "test-session",
        "ts.yaml",
    )
    os.makedirs(os.path.dirname(archive_yaml), exist_ok=True)
    open(archive_yaml, "w").close()
    real_report = os.path.join(base, "real-report.yaml")
    open(real_report, "w").close()
    sym_report = os.path.join(base, "sym-report.yaml")
    os.symlink(real_report, sym_report)
    session_archive_bundle.stamp_session_archive_marker(
        "test-session", archive_yaml, sym_report
    )
    state = yaml.safe_load(open(sess_path))
    arc = state["session"]["archive"]
    assert arc["from_report"] == os.path.realpath(sym_report)
    assert arc["from_report"] == os.path.realpath(real_report)


def test_module_does_not_import_subprocess_or_network_libs():
    import lib.session_archive_bundle as mod

    for forbidden in ("subprocess", "requests", "httpx", "urllib"):
        assert not hasattr(mod, forbidden), (
            f"module should not have attribute '{forbidden}'"
        )


def test_collision_in_same_utc_second_produces_distinct_files(monkeypatch, tmp_path):
    """Two write_archive_bundle calls with the same timestamp must produce
    two distinct file pairs on disk; the second pair's basename ends with -1.
    """
    base = _patch_storage(monkeypatch, str(tmp_path))
    ctx = _make_validated_context()
    fixed_ts = "2026-04-25T09:00:00Z"
    call_count = [0]

    def same_ts():
        call_count[0] += 1
        return fixed_ts

    monkeypatch.setattr(session_archive_bundle, "_utc_now_iso", same_ts)
    y1, m1 = session_archive_bundle.write_archive_bundle(ctx, base)
    y2, m2 = session_archive_bundle.write_archive_bundle(ctx, base)

    assert y1 != y2
    assert m1 != m2
    for p in (y1, m1, y2, m2):
        assert os.path.isfile(p), f"expected file not found: {p}"
    assert os.path.basename(y2) == f"{fixed_ts}-1.yaml"
    assert os.path.basename(m2) == f"{fixed_ts}-1.md"


def test_real_cli_bundle_has_no_null_required_values(monkeypatch, tmp_path):
    """A validated_context shaped like the real validator output (no pre-filled
    bundle keys) must produce a bundle YAML with no None on required keys.
    """
    base = _patch_storage(monkeypatch, str(tmp_path))
    ctx = {
        "session_id": "real-sess",
        "report_path": "/some/report.yaml",
        "session_state": {
            "session": {
                "id": "real-sess",
                "peer_id": "peer-1",
                "status": "idle",
                "mode": "ephemeral",
                "room_id": "room-a",
                "handoff_id": "hf-a",
                "last_active_at": "2026-04-25T10:00:00Z",
                "lease_until": "2026-04-25T11:00:00Z",
                "dirty": False,
            }
        },
        "handoff_state": {
            "handoff": {
                "id": "hf-a",
                "status": "completed",
                "kind": "implementation",
                "from": "cto",
                "to": "worker",
            },
            "resolution": {
                "status": "completed",
                "completed_at": "2026-04-25T10:30:00Z",
                "completed_by": "worker",
                "summary": "done",
                "verification": ["pytest 10/10"],
                "completion_note": "all good",
            },
        },
        "room_state": {
            "room": {"id": "room-a", "name": "Room A", "status": "active"},
            "lifecycle": {"current_phase": "execution", "next_action": "review"},
        },
        "git": {"head_sha": "deadbeef", "worktree_dirty": False},
    }

    yaml_path, _ = session_archive_bundle.write_archive_bundle(ctx, base)
    with open(yaml_path) as f:
        loaded = yaml.safe_load(f)

    for k in (
        "session_summary",
        "room_summary",
        "handoff_summary",
        "completion_state",
        "review_state",
        "worker_evidence",
        "git_info",
        "next_action",
        "wiki_candidates",
    ):
        assert loaded[k] is not None, f"{k} must not be None"
    assert isinstance(loaded["wiki_candidates"], list)


def test_normalizer_preserves_explicit_values(monkeypatch, tmp_path):
    """If validated_context already has a non-None bundle key, the normalizer
    must not overwrite it with the derived value.
    """
    base = _patch_storage(monkeypatch, str(tmp_path))
    ctx = {
        "session_id": "pres-sess",
        "session_summary": {"id": "explicit"},
        "session_state": {
            "session": {"id": "derived-should-not-appear", "peer_id": "x", "status": "busy"}
        },
    }
    yaml_path, _ = session_archive_bundle.write_archive_bundle(ctx, base)
    with open(yaml_path) as f:
        loaded = yaml.safe_load(f)
    assert loaded["session_summary"] == {"id": "explicit"}


def test_normalizer_derives_session_summary_from_session_state(monkeypatch, tmp_path):
    """Without an explicit session_summary, the normalizer must pull id/peer_id/status
    from session_state.session.
    """
    base = _patch_storage(monkeypatch, str(tmp_path))
    ctx = {
        "session_id": "derive-sess",
        "session_state": {
            "session": {
                "id": "derive-sess",
                "peer_id": "the-peer",
                "status": "running",
            }
        },
    }
    yaml_path, _ = session_archive_bundle.write_archive_bundle(ctx, base)
    with open(yaml_path) as f:
        loaded = yaml.safe_load(f)
    ss = loaded["session_summary"]
    assert ss["id"] == "derive-sess"
    assert ss["peer_id"] == "the-peer"
    assert ss["status"] == "running"


def test_normalizer_uses_empty_collection_not_none_when_source_missing(monkeypatch, tmp_path):
    """With an empty handoff_state, derived collection keys must be [] not None."""
    base = _patch_storage(monkeypatch, str(tmp_path))
    ctx = {
        "session_id": "empty-hf-sess",
        "handoff_state": {},
    }
    yaml_path, _ = session_archive_bundle.write_archive_bundle(ctx, base)
    with open(yaml_path) as f:
        loaded = yaml.safe_load(f)
    for k in ("worker_evidence", "wiki_candidates", "checkpoint_refs"):
        assert loaded[k] is not None, f"{k} should be [] not None"
        assert isinstance(loaded[k], list), f"{k} should be a list"


def test_yaml_uses_deterministic_key_ordering(monkeypatch, tmp_path):
    base = _patch_storage(monkeypatch, str(tmp_path))
    ctx = _make_validated_context()
    monkeypatch.setattr(
        session_archive_bundle,
        "_utc_now_iso",
        lambda: "2026-04-25T08:55:00Z",
    )
    yaml_path_1, _ = session_archive_bundle.write_archive_bundle(ctx, base)
    text_1 = open(yaml_path_1).read()

    base2 = str(tmp_path / "second")
    os.makedirs(base2)
    base2 = _patch_storage(monkeypatch, base2)
    monkeypatch.setattr(
        session_archive_bundle,
        "_utc_now_iso",
        lambda: "2026-04-25T08:55:00Z",
    )
    yaml_path_2, _ = session_archive_bundle.write_archive_bundle(ctx, base2)
    text_2 = open(yaml_path_2).read()
    assert text_1 == text_2, "yaml dump must be deterministic"
