"""Tests for `orchctl room gc-audit` V1 (read-only promotion audit).

All tests are hermetic: tmp dirs, monkeypatched storage paths, no real tmux,
no real git unless the test explicitly creates a tmp git repo.

Invariants under test:
- V1 report must not include `safe_to_archive` / `archive_ready` / `can_archive`
  at any level.
- `audit_verdict` is computed from Tier 1 signals only. `stale_tmux` / dead
  tmux never produces an at-risk reason; it is reported under
  `runtime_observation` only.
- Running `gc-audit` does not mutate any YAML under `.orchestrator/`.
"""
import json
import os
import subprocess
import sys
import pytest
import yaml

_REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), os.pardir))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

from lib import storage, gc_audit  # noqa: E402


class Args:
    def __init__(self, **kwargs):
        self.__dict__.update(kwargs)


# ---------------------------------------------------------------------------
# Shared setup
# ---------------------------------------------------------------------------

def _patch_storage(monkeypatch, base_dir):
    """Redirect every storage path into base_dir/.orchestrator/."""
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


def _patch_tmux_dead(monkeypatch):
    """Force all tmux observations to report dead without subprocess."""
    def _fake_observe(session, target):
        return {
            "tmux_session": session or None,
            "tmux_target": target or None,
            "tmux_alive": "dead",
            "observation_method": "test_stub",
            "observed_at": "2026-04-24T12:00:00Z",
        }
    monkeypatch.setattr(gc_audit, "_observe_tmux", _fake_observe)


def _patch_tmux_unknown(monkeypatch):
    """Force tmux observations to report unknown (tmux unavailable)."""
    def _fake_observe(session, target):
        return {
            "tmux_session": session or None,
            "tmux_target": target or None,
            "tmux_alive": "unknown",
            "observation_method": "tmux_unavailable",
            "observed_at": "2026-04-24T12:00:00Z",
        }
    monkeypatch.setattr(gc_audit, "_observe_tmux", _fake_observe)


def _write_room(base_dir, room_id, execution_cwd=None):
    room_dir = os.path.join(base_dir, ".orchestrator", "rooms", room_id)
    os.makedirs(room_dir, exist_ok=True)
    state = {
        "room": {"id": room_id, "name": "Test Room", "status": "active"},
        "context": {
            "goal": "test",
            "execution_cwd": execution_cwd or base_dir,
        },
        "lifecycle": {"current_phase": "execution"},
    }
    with open(os.path.join(room_dir, "state.yaml"), "w") as f:
        yaml.dump(state, f)
    with open(os.path.join(room_dir, "log.md"), "w") as f:
        f.write("# log\n")
    # TEMPLATE dir required by validators
    tmpl = os.path.join(base_dir, ".orchestrator", "rooms", "TEMPLATE")
    os.makedirs(tmpl, exist_ok=True)
    with open(os.path.join(tmpl, "state.yaml"), "w") as f:
        yaml.dump({"room": {"id": "TEMPLATE"}}, f)


def _write_handoff(base_dir, handoff_id, room_id, status="open",
                   review_outcome=None, to_peer="test-worker"):
    state = {
        "handoff": {
            "id": handoff_id,
            "room_id": room_id,
            "from": "orchestrator",
            "to": to_peer,
            "status": status,
            "priority": "medium",
            "kind": "implementation",
        },
        "task": {"description": "test", "validation": [], "acceptance_criteria": []},
        "timestamps": {"created_at": "2026-01-01T00:00:00Z"},
    }
    if review_outcome is not None:
        state["review"] = {"outcome": review_outcome, "reviewed_by": "cto"}
    path = os.path.join(base_dir, ".orchestrator", "handoffs", f"{handoff_id}.yaml")
    with open(path, "w") as f:
        yaml.dump(state, f)


def _write_session(base_dir, session_id, room_id=None, handoff_id=None,
                   peer_id="test-worker", cwd=None, tmux_session="sess-1",
                   tmux_target="%42"):
    state = {
        "session": {
            "id": session_id,
            "peer_id": peer_id,
            "room_id": room_id,
            "handoff_id": handoff_id,
            "tmux_session": tmux_session,
            "tmux_target": tmux_target,
            "mode": "ephemeral",
            "status": "idle",
            "cwd": cwd or base_dir,
        }
    }
    path = os.path.join(
        base_dir, ".orchestrator", "runtime", "sessions", f"{session_id}.yaml"
    )
    with open(path, "w") as f:
        yaml.dump(state, f)


def _write_peer_registry(base_dir, peer_ids=("test-worker",)):
    reg = {
        "peers": [
            {"id": pid, "name": pid, "type": "worker", "cwd": base_dir,
             "capabilities": [], "status": "available"}
            for pid in peer_ids
        ]
    }
    path = os.path.join(base_dir, ".orchestrator", "peer_registry.yaml")
    with open(path, "w") as f:
        yaml.dump(reg, f)


def _init_tmp_git_repo(path, dirty=False):
    """Create a real tmp git repo at path. Optionally leave it dirty.

    Adds `.orchestrator/` to .gitignore so the orchestrator state files that
    tests write into the repo root do not leak into `git status --porcelain`
    and spuriously flag sessions as `dirty_git`.
    """
    env = {**os.environ,
           "GIT_AUTHOR_NAME": "T", "GIT_AUTHOR_EMAIL": "t@x",
           "GIT_COMMITTER_NAME": "T", "GIT_COMMITTER_EMAIL": "t@x"}
    subprocess.run(["git", "init", "-q", path], check=True, env=env)
    with open(os.path.join(path, ".gitignore"), "w") as f:
        f.write(".orchestrator/\n")
    readme = os.path.join(path, "README.md")
    with open(readme, "w") as f:
        f.write("r\n")
    subprocess.run(["git", "-C", path, "add", ".gitignore", "README.md"],
                   check=True, env=env)
    subprocess.run(["git", "-C", path, "commit", "-q", "-m", "init"],
                   check=True, env=env)
    if dirty:
        with open(readme, "a") as f:
            f.write("dirty\n")


def _load_latest_report(base_dir, room_id):
    gc_dir = os.path.join(
        base_dir, ".orchestrator", "runtime", "gc-audits", room_id
    )
    assert os.path.isdir(gc_dir), f"gc-audit dir missing: {gc_dir}"
    files = sorted(f for f in os.listdir(gc_dir) if f.endswith(".yaml"))
    assert files, "no report files found"
    with open(os.path.join(gc_dir, files[-1])) as f:
        return yaml.safe_load(f), os.path.join(gc_dir, files[-1])


def _snapshot_orch_yaml(base_dir):
    """Snapshot every YAML under .orchestrator/ except the gc-audits
    output dir. Returns dict of relpath -> bytes."""
    orch = os.path.join(base_dir, ".orchestrator")
    snap = {}
    for root, _dirs, files in os.walk(orch):
        rel_root = os.path.relpath(root, orch)
        # Exclude the gc-audit report output path — it's expected to change.
        if rel_root.split(os.sep)[0:2] == ["runtime", "gc-audits"]:
            continue
        for fn in files:
            if not fn.endswith(".yaml"):
                continue
            p = os.path.join(root, fn)
            with open(p, "rb") as fh:
                snap[os.path.relpath(p, orch)] = fh.read()
    return snap


# ---------------------------------------------------------------------------
# Basic shape / empty room
# ---------------------------------------------------------------------------

def test_empty_room_neutral_verdict(monkeypatch, tmp_path, capsys):
    base_dir = str(tmp_path)
    _patch_storage(monkeypatch, base_dir)
    _patch_tmux_unknown(monkeypatch)
    _write_room(base_dir, "r1")
    _write_peer_registry(base_dir)

    gc_audit.cmd_room_gc_audit(Args(room_id="r1"))

    report, path = _load_latest_report(base_dir, "r1")
    assert report["gc_audit"]["room_id"] == "r1"
    assert report["gc_audit"]["version"] == 1
    assert report["room_summary"]["total_sessions"] == 0
    assert report["room_summary"]["room_verdict"] == "neutral"
    assert report["sessions"] == []


def test_report_forbids_green_light_fields(monkeypatch, tmp_path):
    """V1 invariant: no top-level safe_to_archive / archive_ready / can_archive.

    Also check those strings do not appear anywhere in the serialized YAML.
    """
    base_dir = str(tmp_path)
    _patch_storage(monkeypatch, base_dir)
    _patch_tmux_unknown(monkeypatch)
    _write_room(base_dir, "r1")
    _write_peer_registry(base_dir)
    _init_tmp_git_repo(base_dir)
    _write_handoff(base_dir, "h1", "r1", status="completed", review_outcome="approved")
    _write_session(base_dir, "s1", room_id="r1", handoff_id="h1", cwd=base_dir)

    gc_audit.cmd_room_gc_audit(Args(room_id="r1"))

    report, report_path = _load_latest_report(base_dir, "r1")
    for banned in ("safe_to_archive", "archive_ready", "can_archive"):
        assert banned not in report
        assert banned not in report.get("room_summary", {})
        for sess in report.get("sessions", []):
            assert banned not in sess
    with open(report_path) as f:
        raw = f.read()
    for banned in ("safe_to_archive", "archive_ready", "can_archive"):
        assert banned not in raw


# ---------------------------------------------------------------------------
# Session classification: promoted / at-risk / unbound / parse-error
# ---------------------------------------------------------------------------

def test_promoted_session_approved_clean_git(monkeypatch, tmp_path):
    base_dir = str(tmp_path)
    _patch_storage(monkeypatch, base_dir)
    _patch_tmux_unknown(monkeypatch)
    _write_room(base_dir, "r1")
    _write_peer_registry(base_dir)
    _init_tmp_git_repo(base_dir, dirty=False)
    _write_handoff(base_dir, "h1", "r1", status="completed",
                   review_outcome="approved")
    _write_session(base_dir, "s1", room_id="r1", handoff_id="h1", cwd=base_dir)

    gc_audit.cmd_room_gc_audit(Args(room_id="r1"))

    report, _ = _load_latest_report(base_dir, "r1")
    summary = report["room_summary"]
    assert summary["promoted_count"] == 1
    assert summary["at_risk_count"] == 0
    assert summary["unbound_count"] == 0
    assert summary["room_verdict"] == "coherent"
    sess = report["sessions"][0]
    assert sess["audit_verdict"] == "promoted"
    assert sess["reasons"] == []
    assert sess["handoff_status"] == "completed"
    assert sess["review_state"] == "approved"


def test_pending_review_is_at_risk(monkeypatch, tmp_path):
    base_dir = str(tmp_path)
    _patch_storage(monkeypatch, base_dir)
    _patch_tmux_unknown(monkeypatch)
    _write_room(base_dir, "r1")
    _write_peer_registry(base_dir)
    _init_tmp_git_repo(base_dir, dirty=False)
    # Completed but not yet reviewed
    _write_handoff(base_dir, "h1", "r1", status="completed")
    _write_session(base_dir, "s1", room_id="r1", handoff_id="h1", cwd=base_dir)

    gc_audit.cmd_room_gc_audit(Args(room_id="r1"))

    report, _ = _load_latest_report(base_dir, "r1")
    sess = report["sessions"][0]
    assert sess["audit_verdict"] == "at-risk"
    assert "pending_review" in sess["reasons"]
    assert report["room_summary"]["room_verdict"] == "some-at-risk"


def test_changes_requested_is_at_risk(monkeypatch, tmp_path):
    base_dir = str(tmp_path)
    _patch_storage(monkeypatch, base_dir)
    _patch_tmux_unknown(monkeypatch)
    _write_room(base_dir, "r1")
    _write_peer_registry(base_dir)
    _init_tmp_git_repo(base_dir, dirty=False)
    _write_handoff(base_dir, "h1", "r1", status="completed",
                   review_outcome="changes_requested")
    _write_session(base_dir, "s1", room_id="r1", handoff_id="h1", cwd=base_dir)

    gc_audit.cmd_room_gc_audit(Args(room_id="r1"))

    report, _ = _load_latest_report(base_dir, "r1")
    sess = report["sessions"][0]
    assert sess["audit_verdict"] == "at-risk"
    assert "changes_requested_pending" in sess["reasons"]


def test_dirty_git_is_at_risk(monkeypatch, tmp_path):
    base_dir = str(tmp_path)
    _patch_storage(monkeypatch, base_dir)
    _patch_tmux_unknown(monkeypatch)
    _write_room(base_dir, "r1")
    _write_peer_registry(base_dir)
    _init_tmp_git_repo(base_dir, dirty=True)
    _write_handoff(base_dir, "h1", "r1", status="completed",
                   review_outcome="approved")
    _write_session(base_dir, "s1", room_id="r1", handoff_id="h1", cwd=base_dir)

    gc_audit.cmd_room_gc_audit(Args(room_id="r1"))

    report, _ = _load_latest_report(base_dir, "r1")
    sess = report["sessions"][0]
    assert sess["audit_verdict"] == "at-risk"
    assert "dirty_git" in sess["reasons"]


def test_open_handoff_is_at_risk(monkeypatch, tmp_path):
    """An open (non-completed) handoff collapses into the V2-contracted
    `pending_review` reason. Operators can still read the precise
    `handoff_status` field for detail."""
    base_dir = str(tmp_path)
    _patch_storage(monkeypatch, base_dir)
    _patch_tmux_unknown(monkeypatch)
    _write_room(base_dir, "r1")
    _write_peer_registry(base_dir)
    _init_tmp_git_repo(base_dir, dirty=False)
    _write_handoff(base_dir, "h1", "r1", status="open")
    _write_session(base_dir, "s1", room_id="r1", handoff_id="h1", cwd=base_dir)

    gc_audit.cmd_room_gc_audit(Args(room_id="r1"))

    report, _ = _load_latest_report(base_dir, "r1")
    sess = report["sessions"][0]
    assert sess["audit_verdict"] == "at-risk"
    assert "pending_review" in sess["reasons"]
    assert sess["handoff_status"] == "open"


def test_unbound_no_handoff(monkeypatch, tmp_path):
    base_dir = str(tmp_path)
    _patch_storage(monkeypatch, base_dir)
    _patch_tmux_unknown(monkeypatch)
    _write_room(base_dir, "r1")
    _write_peer_registry(base_dir)
    _write_session(base_dir, "s1", room_id="r1", handoff_id=None, cwd=base_dir)

    gc_audit.cmd_room_gc_audit(Args(room_id="r1"))

    report, _ = _load_latest_report(base_dir, "r1")
    sess = report["sessions"][0]
    assert sess["audit_verdict"] == "unbound"
    assert "no_handoff_binding" in sess["reasons"]
    assert report["room_summary"]["unbound_count"] == 1
    # Unbound-only room is neutral, not some-at-risk.
    assert report["room_summary"]["room_verdict"] == "neutral"


def test_unbound_handoff_in_other_room(monkeypatch, tmp_path):
    base_dir = str(tmp_path)
    _patch_storage(monkeypatch, base_dir)
    _patch_tmux_unknown(monkeypatch)
    _write_room(base_dir, "r1")
    _write_room(base_dir, "r2")
    _write_peer_registry(base_dir)
    _write_handoff(base_dir, "h-other", "r2", status="completed",
                   review_outcome="approved")
    _write_session(base_dir, "s1", room_id="r1", handoff_id="h-other",
                   cwd=base_dir)

    gc_audit.cmd_room_gc_audit(Args(room_id="r1"))

    report, _ = _load_latest_report(base_dir, "r1")
    sess = report["sessions"][0]
    assert sess["audit_verdict"] == "unbound"
    assert "handoff_not_in_room" in sess["reasons"]


def test_missing_handoff_ref_is_unbound(monkeypatch, tmp_path):
    base_dir = str(tmp_path)
    _patch_storage(monkeypatch, base_dir)
    _patch_tmux_unknown(monkeypatch)
    _write_room(base_dir, "r1")
    _write_peer_registry(base_dir)
    # Session references a handoff id that doesn't exist on disk.
    _write_session(base_dir, "s1", room_id="r1", handoff_id="h-ghost",
                   cwd=base_dir)

    gc_audit.cmd_room_gc_audit(Args(room_id="r1"))

    report, _ = _load_latest_report(base_dir, "r1")
    sess = report["sessions"][0]
    assert sess["audit_verdict"] == "unbound"
    assert "missing_handoff_ref" in sess["reasons"]


def test_parse_error_session(monkeypatch, tmp_path):
    base_dir = str(tmp_path)
    _patch_storage(monkeypatch, base_dir)
    _patch_tmux_unknown(monkeypatch)
    _write_room(base_dir, "r1")
    _write_peer_registry(base_dir)
    # Write an intentionally malformed session file.
    sessions_dir = os.path.join(base_dir, ".orchestrator", "runtime", "sessions")
    with open(os.path.join(sessions_dir, "broken.yaml"), "w") as f:
        f.write("session: [this is: not valid\nyaml: {\n")

    gc_audit.cmd_room_gc_audit(Args(room_id="r1"))

    report, _ = _load_latest_report(base_dir, "r1")
    assert report["room_summary"]["parse_error_count"] == 1
    assert report["room_summary"]["room_verdict"] == "unknown"
    parse_err = [s for s in report["sessions"] if s["audit_verdict"] == "parse-error"]
    assert len(parse_err) == 1
    assert parse_err[0]["session_id"] == "broken"


# ---------------------------------------------------------------------------
# Tier 2: tmux liveness is runtime_observation only
# ---------------------------------------------------------------------------

def test_dead_tmux_does_not_cause_at_risk(monkeypatch, tmp_path):
    """Tier 2 invariant: dead tmux pane is runtime_observation only.

    Given a completed+approved handoff on a clean git worktree, the verdict
    must still be `promoted` even when the tmux pane is dead.
    """
    base_dir = str(tmp_path)
    _patch_storage(monkeypatch, base_dir)
    _patch_tmux_dead(monkeypatch)
    _write_room(base_dir, "r1")
    _write_peer_registry(base_dir)
    _init_tmp_git_repo(base_dir, dirty=False)
    _write_handoff(base_dir, "h1", "r1", status="completed",
                   review_outcome="approved")
    _write_session(base_dir, "s1", room_id="r1", handoff_id="h1", cwd=base_dir)

    gc_audit.cmd_room_gc_audit(Args(room_id="r1"))

    report, _ = _load_latest_report(base_dir, "r1")
    sess = report["sessions"][0]
    assert sess["audit_verdict"] == "promoted"
    assert "stale_tmux" not in sess["reasons"]
    assert sess["runtime_observation"]["tmux_alive"] == "dead"


# ---------------------------------------------------------------------------
# Mutation safety: read-only except report file
# ---------------------------------------------------------------------------

def test_audit_does_not_mutate_authoritative_yaml(monkeypatch, tmp_path):
    base_dir = str(tmp_path)
    _patch_storage(monkeypatch, base_dir)
    _patch_tmux_unknown(monkeypatch)
    _write_room(base_dir, "r1")
    _write_peer_registry(base_dir)
    _init_tmp_git_repo(base_dir, dirty=False)
    _write_handoff(base_dir, "h1", "r1", status="completed",
                   review_outcome="approved")
    _write_session(base_dir, "s1", room_id="r1", handoff_id="h1", cwd=base_dir)

    before = _snapshot_orch_yaml(base_dir)
    gc_audit.cmd_room_gc_audit(Args(room_id="r1"))
    after = _snapshot_orch_yaml(base_dir)

    assert before == after, "gc-audit must not mutate authoritative YAML"


def test_report_filename_uses_microsecond_timestamp(monkeypatch, tmp_path):
    base_dir = str(tmp_path)
    _patch_storage(monkeypatch, base_dir)
    _patch_tmux_unknown(monkeypatch)
    _write_room(base_dir, "r1")
    _write_peer_registry(base_dir)

    gc_audit.cmd_room_gc_audit(Args(room_id="r1"))

    gc_dir = os.path.join(base_dir, ".orchestrator", "runtime", "gc-audits", "r1")
    files = [f for f in os.listdir(gc_dir) if f.endswith(".yaml")]
    assert len(files) == 1
    # Expect YYYYMMDDTHHMMSS.ffffff.yaml (optionally with -NNN suffix).
    import re
    assert re.match(
        r"^\d{8}T\d{6}\.\d{6}(-\d{3})?\.yaml$", files[0]
    ), files[0]


def test_collision_suffix_increments(monkeypatch, tmp_path):
    """Pre-seed a file at the next-to-be-chosen timestamp and verify suffix."""
    base_dir = str(tmp_path)
    _patch_storage(monkeypatch, base_dir)
    _patch_tmux_unknown(monkeypatch)
    _write_room(base_dir, "r1")
    _write_peer_registry(base_dir)

    # Force deterministic datetime.now() so we can control the filename.
    class _FixedNow:
        @staticmethod
        def now(tz=None):
            from datetime import datetime, timezone as _tz
            return datetime(2026, 1, 2, 3, 4, 5, 123456, tzinfo=_tz.utc)

    monkeypatch.setattr(gc_audit, "datetime", _FixedNow)

    gc_dir_room = os.path.join(
        base_dir, ".orchestrator", "runtime", "gc-audits", "r1"
    )
    os.makedirs(gc_dir_room, exist_ok=True)
    # Pre-create the unsuffixed file so the audit must pick -001.
    pre_path = os.path.join(gc_dir_room, "20260102T030405.123456.yaml")
    with open(pre_path, "w") as f:
        f.write("stub: true\n")

    gc_audit.cmd_room_gc_audit(Args(room_id="r1"))

    files = sorted(os.listdir(gc_dir_room))
    assert "20260102T030405.123456.yaml" in files
    assert "20260102T030405.123456-001.yaml" in files


def test_missing_peer_reference_flagged(monkeypatch, tmp_path):
    """A session bound to a peer not in the registry is flagged under the
    V2-contracted `foreign_owner` reason."""
    base_dir = str(tmp_path)
    _patch_storage(monkeypatch, base_dir)
    _patch_tmux_unknown(monkeypatch)
    _write_room(base_dir, "r1")
    # Registry has no worker matching the session's peer_id.
    _write_peer_registry(base_dir, peer_ids=("someone-else",))
    _init_tmp_git_repo(base_dir, dirty=False)
    _write_handoff(base_dir, "h1", "r1", status="completed",
                   review_outcome="approved", to_peer="test-worker")
    _write_session(base_dir, "s1", room_id="r1", handoff_id="h1",
                   peer_id="test-worker", cwd=base_dir)

    gc_audit.cmd_room_gc_audit(Args(room_id="r1"))

    report, _ = _load_latest_report(base_dir, "r1")
    sess = report["sessions"][0]
    assert sess["audit_verdict"] == "at-risk"
    assert "foreign_owner" in sess["reasons"]


def test_git_unavailable_for_cwd_flagged(monkeypatch, tmp_path):
    """A cwd that exists but is not a git worktree is flagged under the
    V2-contracted `no_git_dir` reason."""
    base_dir = str(tmp_path)
    _patch_storage(monkeypatch, base_dir)
    _patch_tmux_unknown(monkeypatch)
    _write_room(base_dir, "r1")
    _write_peer_registry(base_dir)
    # Do NOT init a git repo.
    _write_handoff(base_dir, "h1", "r1", status="completed",
                   review_outcome="approved")
    _write_session(base_dir, "s1", room_id="r1", handoff_id="h1", cwd=base_dir)

    gc_audit.cmd_room_gc_audit(Args(room_id="r1"))

    report, _ = _load_latest_report(base_dir, "r1")
    sess = report["sessions"][0]
    assert sess["audit_verdict"] == "at-risk"
    assert "no_git_dir" in sess["reasons"]


# ---------------------------------------------------------------------------
# Reason-code hygiene
# ---------------------------------------------------------------------------

def test_stale_tmux_never_in_at_risk_reasons():
    """Module-level invariant: `stale_tmux` is not a Tier 1 reason code."""
    assert "stale_tmux" not in gc_audit.AT_RISK_REASONS
    assert "stale_tmux" not in gc_audit.UNBOUND_REASONS


def test_at_risk_reasons_match_v2_lock_list():
    """Module-level invariant: the at-risk reason set is exactly the V2
    contracted lock list. Any drift here needs a coordinated contract bump."""
    expected = {
        "pending_review",
        "changes_requested_pending",
        "dirty_git",
        "cwd_missing",
        "cwd_not_absolute",
        "session_busy",
        "ahead_of_remote",
        "behind_remote",
        "detached_head",
        "inside_submodule",
        "no_git_dir",
        "foreign_owner",
        "parse-error",
    }
    assert gc_audit.AT_RISK_REASONS == expected


def test_unsafe_handoff_id_never_reaches_storage_handoff_path(
    monkeypatch, tmp_path
):
    """P2 hardening: a session YAML carrying a non-slug-safe `handoff_id`
    must NOT cause gc-audit to resolve that value through
    `storage.handoff_path` (which would join it into a filesystem path and
    feed it to `read_state`). Same invariant locked for checkpoints.

    Documented classification choice: the session is marked `parse-error`.
    Rationale: the session YAML parses, but the handoff binding field is
    structurally invalid, which is effectively a parse-level failure for the
    audit's purposes — the binding cannot be trusted.
    """
    base_dir = str(tmp_path)
    _patch_storage(monkeypatch, base_dir)
    _patch_tmux_unknown(monkeypatch)
    _write_room(base_dir, "r1")
    _write_peer_registry(base_dir)

    # Session YAML parses, but handoff_id carries a path-traversal payload.
    _write_session(
        base_dir, "s1", room_id="r1", handoff_id="../evil", cwd=base_dir
    )

    # Track every storage.handoff_path invocation to prove the unsafe value
    # never reaches it.
    calls = []
    real_handoff_path = storage.handoff_path

    def _tracking_handoff_path(hid):
        calls.append(hid)
        return real_handoff_path(hid)

    monkeypatch.setattr(storage, "handoff_path", _tracking_handoff_path)

    gc_audit.cmd_room_gc_audit(Args(room_id="r1"))

    assert "../evil" not in calls, (
        "gc-audit leaked unsafe handoff_id into storage.handoff_path; "
        f"observed calls={calls}"
    )

    report, _ = _load_latest_report(base_dir, "r1")
    parse_err_sessions = [
        s for s in report["sessions"] if s["audit_verdict"] == "parse-error"
    ]
    s1_entries = [s for s in parse_err_sessions if s["session_id"] == "s1"]
    assert len(s1_entries) == 1, (
        f"expected s1 classified as parse-error; sessions={report['sessions']}"
    )
    # The tainted binding must not propagate into the report.
    assert s1_entries[0]["handoff_id"] is None
    # The reason code must be the contracted `parse-error` spelling.
    assert s1_entries[0]["reasons"] == ["parse-error"]
    assert report["room_summary"]["parse_error_count"] >= 1
    assert report["room_summary"]["room_verdict"] == "unknown"


# ---------------------------------------------------------------------------
# CLI parser wiring smoke test
# ---------------------------------------------------------------------------

def test_parser_has_room_gc_audit_subcommand():
    """`orchctl room gc-audit <room-id>` must parse without error."""
    # `orchctl` has no .py extension, so the default suffix-based loader
    # misses it — wire up a SourceFileLoader explicitly.
    import importlib.util
    import importlib.machinery
    path = os.path.join(_REPO_ROOT, "orchctl")
    loader = importlib.machinery.SourceFileLoader("orchctl_cli", path)
    spec = importlib.util.spec_from_loader("orchctl_cli", loader)
    orchctl_mod = importlib.util.module_from_spec(spec)
    loader.exec_module(orchctl_mod)
    parser = orchctl_mod.build_parser()
    ns = parser.parse_args(["room", "gc-audit", "my-room"])
    assert ns.room_id == "my-room"
    assert callable(ns.func)
