"""Tests for `orchctl session archive` CLI integration (Child C).

Hermetic: tmp git repos, monkeypatched storage, no real tmux, no network.
Calls cmd_session_archive directly via argparse.Namespace (Approach A).
"""
import argparse
import hashlib
import os
import subprocess
import sys

import pytest
import yaml

_REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), os.pardir))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

from lib import storage, session_archive  # noqa: E402
from lib.session_archive import cmd_session_archive  # noqa: E402


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_SESSION_ID = "session-cli-1"
_HANDOFF_ID = "handoff-cli-1"
_ROOM_ID = "room-cli-1"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _sha256_file(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def _git(repo: str, *args, check: bool = True) -> subprocess.CompletedProcess:
    env = os.environ.copy()
    env.setdefault("GIT_AUTHOR_NAME", "Tester")
    env.setdefault("GIT_AUTHOR_EMAIL", "tester@example.com")
    env.setdefault("GIT_COMMITTER_NAME", "Tester")
    env.setdefault("GIT_COMMITTER_EMAIL", "tester@example.com")
    proc = subprocess.run(
        ["git", "-C", repo, *args],
        capture_output=True,
        text=True,
        env=env,
    )
    if check and proc.returncode != 0:
        raise RuntimeError(f"git {' '.join(args)} failed: {proc.stderr.strip()}")
    return proc


def _make_repo(base: str) -> str:
    """Create tmp git repo with .orchestrator skeleton + initial commit."""
    os.makedirs(os.path.join(base, ".orchestrator", "rooms", _ROOM_ID), exist_ok=True)
    os.makedirs(os.path.join(base, ".orchestrator", "handoffs"), exist_ok=True)
    os.makedirs(os.path.join(base, ".orchestrator", "runtime", "sessions"), exist_ok=True)
    os.makedirs(os.path.join(base, ".orchestrator", "runtime", "gc-audits"), exist_ok=True)

    _git(base, "init", "-q")
    _git(base, "config", "commit.gpgsign", "false")
    _git(base, "config", "user.name", "Tester")
    _git(base, "config", "user.email", "tester@example.com")

    seed = os.path.join(base, "README.md")
    with open(seed, "w") as f:
        f.write("seed\n")
    gitignore = os.path.join(base, ".gitignore")
    with open(gitignore, "w") as f:
        f.write(".orchestrator/runtime/gc-audits/\n")
    _git(base, "add", "README.md", ".gitignore")
    _git(base, "commit", "-q", "-m", "seed")
    return base


def _patch_storage(monkeypatch, base: str) -> None:
    """Redirect all storage module paths into the tmp repo."""
    orch_dir = os.path.join(base, ".orchestrator")
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


def _write_state_files(base: str):
    session_yaml = os.path.join(
        base, ".orchestrator", "runtime", "sessions", f"{_SESSION_ID}.yaml"
    )
    handoff_yaml = os.path.join(
        base, ".orchestrator", "handoffs", f"{_HANDOFF_ID}.yaml"
    )
    room_yaml = os.path.join(
        base, ".orchestrator", "rooms", _ROOM_ID, "state.yaml"
    )
    with open(session_yaml, "w") as f:
        yaml.dump(
            {
                "session": {
                    "id": _SESSION_ID,
                    "handoff_id": _HANDOFF_ID,
                    "room_id": _ROOM_ID,
                    "status": "idle",
                }
            },
            f, sort_keys=False,
        )
    with open(handoff_yaml, "w") as f:
        yaml.dump(
            {"handoff": {"id": _HANDOFF_ID, "status": "completed"}},
            f, sort_keys=False,
        )
    with open(room_yaml, "w") as f:
        yaml.dump(
            {"room": {"id": _ROOM_ID, "status": "active"}},
            f, sort_keys=False,
        )
    return session_yaml, handoff_yaml, room_yaml


def _build_report(
    repo: str,
    session_yaml: str,
    handoff_yaml: str,
    room_yaml: str,
    *,
    session_id: str = _SESSION_ID,
    audit_verdict: str = "promoted",
    head_sha_override: str | None = None,
    worktree_dirty_override: bool | None = None,
    session_sha_override: str | None = None,
    handoff_sha_override: str | None = None,
    room_sha_override: str | None = None,
) -> dict:
    head_proc = _git(repo, "rev-parse", "HEAD")
    head_sha = head_sha_override if head_sha_override is not None else head_proc.stdout.strip()
    status_proc = _git(repo, "status", "--porcelain")
    if worktree_dirty_override is not None:
        worktree_dirty = worktree_dirty_override
    else:
        worktree_dirty = bool(status_proc.stdout.strip())
    return {
        "session_id": session_id,
        "audit_verdict": audit_verdict,
        "snapshots": {
            "session_yaml_sha256": session_sha_override if session_sha_override is not None else _sha256_file(session_yaml),
            "handoff_yaml_sha256": handoff_sha_override if handoff_sha_override is not None else _sha256_file(handoff_yaml),
            "room_yaml_sha256": room_sha_override if room_sha_override is not None else _sha256_file(room_yaml),
        },
        "git": {
            "head_sha": head_sha,
            "worktree_dirty": worktree_dirty,
        },
    }


def _write_report(repo: str, name: str, report: dict) -> str:
    audits_dir = os.path.join(repo, ".orchestrator", "runtime", "gc-audits")
    os.makedirs(audits_dir, exist_ok=True)
    path = os.path.join(audits_dir, name)
    with open(path, "w") as f:
        yaml.dump(report, f, sort_keys=False)
    return path


def _args(session_id: str, from_report: str) -> argparse.Namespace:
    return argparse.Namespace(session_id=session_id, from_report=from_report)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def repo(tmp_path):
    base = str(tmp_path / "repo")
    os.makedirs(base, exist_ok=True)
    return _make_repo(base)


@pytest.fixture
def good_state(repo, monkeypatch):
    """Hermetic repo with a fresh promoted report; storage patched to tmp."""
    _patch_storage(monkeypatch, repo)
    session_yaml, handoff_yaml, room_yaml = _write_state_files(repo)
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "state-yamls")
    report = _build_report(repo, session_yaml, handoff_yaml, room_yaml)
    report_path = _write_report(repo, "good.yaml", report)

    # Monkeypatch session_archive._repo_root to return our tmp repo.
    monkeypatch.setattr(session_archive, "_repo_root", lambda: repo)

    return {
        "repo": repo,
        "session_yaml": session_yaml,
        "handoff_yaml": handoff_yaml,
        "room_yaml": room_yaml,
        "report": report,
        "report_path": report_path,
    }


# ---------------------------------------------------------------------------
# Test 1: missing --from-report raises SystemExit (argparse error)
# ---------------------------------------------------------------------------

def _load_orchctl():
    """Load orchctl (no .py extension) via importlib SourceFileLoader."""
    import importlib.machinery
    import importlib.util
    orchctl_path = os.path.join(_REPO_ROOT, "orchctl")
    loader = importlib.machinery.SourceFileLoader("orchctl", orchctl_path)
    spec = importlib.util.spec_from_loader("orchctl", loader)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_missing_from_report_argparse_error():
    """orchctl session archive <id> with no --from-report must exit non-zero."""
    orchctl = _load_orchctl()
    parser = orchctl.build_parser()
    with pytest.raises(SystemExit) as exc_info:
        parser.parse_args(["session", "archive", _SESSION_ID])
    assert exc_info.value.code != 0


# ---------------------------------------------------------------------------
# Test 2: relative report path -> parse_error, exit 1
# ---------------------------------------------------------------------------

def test_relative_report_path_returns_parse_error_exit_1(good_state, capsys, monkeypatch):
    relative = os.path.relpath(good_state["report_path"])
    assert not os.path.isabs(relative)
    args = _args(_SESSION_ID, relative)
    rc = cmd_session_archive(args)
    captured = capsys.readouterr()
    assert rc == 1
    assert "parse_error" in captured.err


# ---------------------------------------------------------------------------
# Test 3: report path outside repo -> unsafe_to_archive, exit 1
# ---------------------------------------------------------------------------

def test_outside_repo_report_returns_unsafe_to_archive_exit_1(good_state, tmp_path, capsys):
    outside = str(tmp_path / "outside.yaml")
    with open(outside, "w") as f:
        yaml.dump(good_state["report"], f)
    args = _args(_SESSION_ID, outside)
    rc = cmd_session_archive(args)
    captured = capsys.readouterr()
    assert rc == 1
    assert "unsafe_to_archive" in captured.err


# ---------------------------------------------------------------------------
# Test 4: session_id mismatch -> report_mismatch, exit 1
# ---------------------------------------------------------------------------

def test_session_id_mismatch_returns_report_mismatch_exit_1(good_state, capsys):
    args = _args("wrong-session-id", good_state["report_path"])
    rc = cmd_session_archive(args)
    captured = capsys.readouterr()
    assert rc == 1
    assert "report_mismatch" in captured.err


# ---------------------------------------------------------------------------
# Test 5: happy path — exit 0, stdout starts with "archived: ", files exist
# ---------------------------------------------------------------------------

def test_promoted_fresh_report_returns_archived_exit_0(good_state, capsys):
    args = _args(_SESSION_ID, good_state["report_path"])
    rc = cmd_session_archive(args)
    captured = capsys.readouterr()
    assert rc == 0, f"expected exit 0, got 1; stderr={captured.err!r}"

    # stdout must start with "archived: "
    assert captured.out.startswith("archived: "), repr(captured.out)

    # Parse bundle yaml path from stdout
    yaml_path = captured.out.strip().removeprefix("archived: ")
    assert os.path.isfile(yaml_path), f"bundle yaml not on disk: {yaml_path}"

    # Corresponding md file must exist
    md_path = yaml_path[:-5] + ".md"
    assert os.path.isfile(md_path), f"bundle md not on disk: {md_path}"

    # Bundle is under locked archive path
    expected_prefix = os.path.join(
        good_state["repo"], ".orchestrator", "runtime", "session-archives", _SESSION_ID
    )
    assert yaml_path.startswith(os.path.realpath(expected_prefix)), yaml_path

    # Session YAML must have archive block with 4 locked fields
    with open(good_state["session_yaml"]) as f:
        state = yaml.safe_load(f)
    arc = state["session"]["archive"]
    assert arc["status"] == "archived"
    assert "archived_at" in arc
    assert "archive_path" in arc
    assert "from_report" in arc


# ---------------------------------------------------------------------------
# Test 6: re-run on already-archived session -> already_archived, exit 1
# ---------------------------------------------------------------------------

def test_rerun_after_archived_returns_already_archived_exit_1(good_state, capsys):
    args = _args(_SESSION_ID, good_state["report_path"])
    # First run succeeds
    rc1 = cmd_session_archive(args)
    assert rc1 == 0

    # Need to rebuild report with updated session sha (archive block now present)
    repo = good_state["repo"]
    report = _build_report(
        repo,
        good_state["session_yaml"],
        good_state["handoff_yaml"],
        good_state["room_yaml"],
    )
    report_path2 = _write_report(repo, "good2.yaml", report)
    args2 = _args(_SESSION_ID, report_path2)
    # Clear capsys buffer
    capsys.readouterr()
    rc2 = cmd_session_archive(args2)
    captured = capsys.readouterr()
    assert rc2 == 1
    assert "already_archived" in captured.err


# ---------------------------------------------------------------------------
# Test 7: at-risk verdict -> unsafe_to_archive, exit 1
# ---------------------------------------------------------------------------

def test_at_risk_verdict_returns_unsafe_to_archive_exit_1(good_state, capsys):
    repo = good_state["repo"]
    bad_report = dict(good_state["report"])
    bad_report["audit_verdict"] = "at-risk"
    path = _write_report(repo, "at-risk.yaml", bad_report)
    args = _args(_SESSION_ID, path)
    rc = cmd_session_archive(args)
    captured = capsys.readouterr()
    assert rc == 1
    assert "unsafe_to_archive" in captured.err


# ---------------------------------------------------------------------------
# Test 8: yaml drift (stale report) -> stale_report, exit 1
# ---------------------------------------------------------------------------

def test_yaml_drift_returns_stale_report_exit_1(good_state, capsys):
    # Mutate session yaml AFTER the report was built
    with open(good_state["session_yaml"], "a") as f:
        f.write("# drift\n")
    args = _args(_SESSION_ID, good_state["report_path"])
    rc = cmd_session_archive(args)
    captured = capsys.readouterr()
    assert rc == 1
    assert "stale_report" in captured.err


# ---------------------------------------------------------------------------
# Test 9 (hardening): validation failure writes no bundle or marker
# ---------------------------------------------------------------------------

def test_validation_failure_writes_no_bundle_or_marker(good_state, capsys):
    """On any non-archived exit: no files under session-archives, no archive key."""
    repo = good_state["repo"]

    # Use relative path to guarantee parse_error
    relative = os.path.relpath(good_state["report_path"])
    args = _args(_SESSION_ID, relative)
    rc = cmd_session_archive(args)
    assert rc == 1

    # No files under session-archives
    archives_dir = os.path.join(
        repo, ".orchestrator", "runtime", "session-archives"
    )
    leaked = []
    if os.path.isdir(archives_dir):
        for root, _, files in os.walk(archives_dir):
            for fn in files:
                leaked.append(os.path.join(root, fn))
    assert leaked == [], f"unexpected files written: {leaked}"

    # No archive key in session YAML
    with open(good_state["session_yaml"]) as f:
        state = yaml.safe_load(f)
    assert "archive" not in state.get("session", {}), (
        "archive key must not appear in session yaml on validation failure"
    )


# ---------------------------------------------------------------------------
# Test 10: stamp-time CAS — session yaml drift between bundle write and stamp
# ---------------------------------------------------------------------------

def test_stamp_time_cas_session_yaml_drift_returns_stale_report_no_marker(
    good_state, capsys, monkeypatch
):
    from lib import session_archive_bundle

    original_write = session_archive_bundle.write_archive_bundle
    session_yaml = good_state["session_yaml"]

    def write_then_mutate(ctx, repo):
        y, m = original_write(ctx, repo)
        # Race: mutate session yaml between bundle write and marker stamp.
        with open(session_yaml, "a") as f:
            f.write("# concurrent drift\n")
        return y, m

    monkeypatch.setattr(session_archive_bundle, "write_archive_bundle", write_then_mutate)
    args = _args(_SESSION_ID, good_state["report_path"])
    rc = cmd_session_archive(args)
    captured = capsys.readouterr()
    assert rc == 1
    assert "stale_report" in captured.err
    with open(session_yaml) as f:
        state = yaml.safe_load(f)
    assert "archive" not in state.get("session", {}), (
        "archive marker must not be stamped when session yaml drifted"
    )


# ---------------------------------------------------------------------------
# Test 11: stamp-time CAS — handoff yaml drift
# ---------------------------------------------------------------------------

def test_stamp_time_cas_handoff_yaml_drift_returns_stale_report_no_marker(
    good_state, capsys, monkeypatch
):
    from lib import session_archive_bundle

    original_write = session_archive_bundle.write_archive_bundle
    handoff_yaml = good_state["handoff_yaml"]
    session_yaml = good_state["session_yaml"]

    def write_then_mutate(ctx, repo):
        y, m = original_write(ctx, repo)
        with open(handoff_yaml, "a") as f:
            f.write("# concurrent drift\n")
        return y, m

    monkeypatch.setattr(session_archive_bundle, "write_archive_bundle", write_then_mutate)
    args = _args(_SESSION_ID, good_state["report_path"])
    rc = cmd_session_archive(args)
    captured = capsys.readouterr()
    assert rc == 1
    assert "stale_report" in captured.err
    with open(session_yaml) as f:
        state = yaml.safe_load(f)
    assert "archive" not in state.get("session", {}), (
        "archive marker must not be stamped when handoff yaml drifted"
    )


# ---------------------------------------------------------------------------
# Test 12: stamp-time CAS — room yaml drift
# ---------------------------------------------------------------------------

def test_stamp_time_cas_room_yaml_drift_returns_stale_report_no_marker(
    good_state, capsys, monkeypatch
):
    from lib import session_archive_bundle

    original_write = session_archive_bundle.write_archive_bundle
    room_yaml = good_state["room_yaml"]
    session_yaml = good_state["session_yaml"]

    def write_then_mutate(ctx, repo):
        y, m = original_write(ctx, repo)
        with open(room_yaml, "a") as f:
            f.write("# concurrent drift\n")
        return y, m

    monkeypatch.setattr(session_archive_bundle, "write_archive_bundle", write_then_mutate)
    args = _args(_SESSION_ID, good_state["report_path"])
    rc = cmd_session_archive(args)
    captured = capsys.readouterr()
    assert rc == 1
    assert "stale_report" in captured.err
    with open(session_yaml) as f:
        state = yaml.safe_load(f)
    assert "archive" not in state.get("session", {}), (
        "archive marker must not be stamped when room yaml drifted"
    )


# ---------------------------------------------------------------------------
# Test 13: cmd_session_archive returns int on failure (no SystemExit)
# ---------------------------------------------------------------------------

def test_cmd_session_archive_returns_int_not_sys_exit_on_failure(good_state, capsys):
    """After T5 refactor, failures must return 1, not raise SystemExit."""
    relative = os.path.relpath(good_state["report_path"])
    args = _args(_SESSION_ID, relative)
    # Must NOT raise SystemExit; must return 1.
    rc = cmd_session_archive(args)
    assert rc == 1


# ---------------------------------------------------------------------------
# Test 14: cmd_session_archive returns 0 on success (already covered by test 5,
#          included here for explicitness of return-int contract)
# ---------------------------------------------------------------------------

def test_cmd_session_archive_returns_0_on_success(good_state, capsys):
    args = _args(_SESSION_ID, good_state["report_path"])
    rc = cmd_session_archive(args)
    assert rc == 0


# ---------------------------------------------------------------------------
# Test 15: orchctl main propagates return code via sys.exit
# ---------------------------------------------------------------------------

def test_orchctl_main_propagates_return_code(good_state, monkeypatch):
    """main() must call sys.exit(rc) with the handler's return value."""
    orchctl = _load_orchctl()
    relative = os.path.relpath(good_state["report_path"])
    monkeypatch.setattr(
        "sys.argv",
        ["orchctl", "session", "archive", _SESSION_ID, "--from-report", relative],
    )
    with pytest.raises(SystemExit) as exc_info:
        orchctl.main()
    assert exc_info.value.code == 1


# ---------------------------------------------------------------------------
# Test 16: CLI hash invariant — handoff + room yaml unchanged; session yaml changed
# ---------------------------------------------------------------------------

def test_room_handoff_yaml_hash_invariant_after_successful_archive(good_state, capsys):
    """Successful archive must not alter handoff/room YAMLs; session yaml MUST change."""
    handoff_yaml = good_state["handoff_yaml"]
    room_yaml = good_state["room_yaml"]
    session_yaml = good_state["session_yaml"]

    handoff_sha_before = _sha256_file(handoff_yaml)
    room_sha_before = _sha256_file(room_yaml)
    session_sha_before = _sha256_file(session_yaml)

    args = _args(_SESSION_ID, good_state["report_path"])
    rc = cmd_session_archive(args)
    assert rc == 0, f"expected rc=0; stderr={capsys.readouterr().err!r}"

    handoff_sha_after = _sha256_file(handoff_yaml)
    room_sha_after = _sha256_file(room_yaml)
    session_sha_after = _sha256_file(session_yaml)

    assert handoff_sha_after == handoff_sha_before, "handoff yaml must not change"
    assert room_sha_after == room_sha_before, "room yaml must not change"
    assert session_sha_after != session_sha_before, "session yaml must change (archive marker stamped)"
