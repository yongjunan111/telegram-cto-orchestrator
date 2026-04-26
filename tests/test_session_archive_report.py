"""Tests for `orchctl session archive-report` CLI producer (T1).

Hermetic: tmp git repos, monkeypatched storage + _repo_root, no real tmux,
no network. Calls cmd_session_archive_report directly via argparse.Namespace.
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

from lib import storage, session_archive_report  # noqa: E402
from lib.session_archive_report import cmd_session_archive_report  # noqa: E402
from lib.session_archive import cmd_session_archive  # noqa: E402


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_SESSION_ID = "report-session-1"
_HANDOFF_ID = "report-handoff-1"
_ROOM_ID = "report-room-1"


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
    os.makedirs(os.path.join(base, ".orchestrator", "runtime", "session-archive-reports"), exist_ok=True)

    _git(base, "init", "-q")
    _git(base, "config", "commit.gpgsign", "false")
    _git(base, "config", "user.name", "Tester")
    _git(base, "config", "user.email", "tester@example.com")

    seed = os.path.join(base, "README.md")
    with open(seed, "w") as f:
        f.write("seed\n")
    # Gitignore runtime dirs so produced reports/archives don't dirty the worktree
    gitignore = os.path.join(base, ".gitignore")
    with open(gitignore, "w") as f:
        f.write(".orchestrator/runtime/\n")
    _git(base, "add", "README.md", ".gitignore")
    _git(base, "commit", "-q", "-m", "seed")
    return base


def _patch_storage(monkeypatch, base: str) -> None:
    """Redirect storage module paths into the tmp repo."""
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


def _write_state_files(
    base: str,
    *,
    handoff_status: str = "completed",
    review_outcome: str = "approved",
) -> tuple:
    session_yaml = os.path.join(
        base, ".orchestrator", "runtime", "sessions", f"{_SESSION_ID}.yaml"
    )
    handoff_yaml = os.path.join(
        base, ".orchestrator", "handoffs", f"{_HANDOFF_ID}.yaml"
    )
    room_yaml = os.path.join(
        base, ".orchestrator", "rooms", _ROOM_ID, "state.yaml"
    )
    os.makedirs(os.path.dirname(room_yaml), exist_ok=True)

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

    handoff_data: dict = {
        "handoff": {
            "id": _HANDOFF_ID,
            "status": handoff_status,
        }
    }
    if handoff_status == "completed" and review_outcome:
        handoff_data["handoff"]["review"] = {"outcome": review_outcome}

    with open(handoff_yaml, "w") as f:
        yaml.dump(handoff_data, f, sort_keys=False)

    with open(room_yaml, "w") as f:
        yaml.dump(
            {"room": {"id": _ROOM_ID, "status": "active"}},
            f, sort_keys=False,
        )

    return session_yaml, handoff_yaml, room_yaml


def _args(session_id: str) -> argparse.Namespace:
    return argparse.Namespace(session_id=session_id)


def _archive_args(session_id: str, from_report: str) -> argparse.Namespace:
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
    """Hermetic repo, promoted handoff, storage + _repo_root patched."""
    _patch_storage(monkeypatch, repo)
    session_yaml, handoff_yaml, room_yaml = _write_state_files(repo)
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "state-yamls")

    monkeypatch.setattr(session_archive_report, "_repo_root", lambda: repo)

    return {
        "repo": repo,
        "session_yaml": session_yaml,
        "handoff_yaml": handoff_yaml,
        "room_yaml": room_yaml,
    }


# ---------------------------------------------------------------------------
# Test 1: happy path — promoted verdict, file written, stdout is abs path
# ---------------------------------------------------------------------------

def test_happy_path_promoted_report_written(good_state, capsys):
    args = _args(_SESSION_ID)
    cmd_session_archive_report(args)
    captured = capsys.readouterr()

    abs_path = captured.out.strip()
    assert os.path.isabs(abs_path), f"stdout must be absolute path, got: {abs_path!r}"
    assert os.path.isfile(abs_path), f"report file not on disk: {abs_path}"

    # File under expected reports dir
    expected_prefix = os.path.join(
        good_state["repo"],
        ".orchestrator", "runtime", "session-archive-reports", _SESSION_ID,
    )
    assert abs_path.startswith(os.path.realpath(expected_prefix)), abs_path


# ---------------------------------------------------------------------------
# Test 2: report YAML has all required V2-contract keys
# ---------------------------------------------------------------------------

def test_report_yaml_has_required_keys(good_state, capsys):
    cmd_session_archive_report(_args(_SESSION_ID))
    abs_path = capsys.readouterr().out.strip()

    with open(abs_path) as f:
        report = yaml.safe_load(f)

    assert isinstance(report, dict)
    assert report["session_id"] == _SESSION_ID
    assert report["audit_verdict"] == "promoted"
    assert "snapshots" in report
    assert "session_yaml_sha256" in report["snapshots"]
    assert "handoff_yaml_sha256" in report["snapshots"]
    assert "room_yaml_sha256" in report["snapshots"]
    assert "git" in report
    assert "head_sha" in report["git"]
    assert "worktree_dirty" in report["git"]


# ---------------------------------------------------------------------------
# Test 3: sha256s match the on-disk YAML files at producer run time
# ---------------------------------------------------------------------------

def test_sha256_snapshots_match_on_disk_files(good_state, capsys):
    cmd_session_archive_report(_args(_SESSION_ID))
    abs_path = capsys.readouterr().out.strip()

    with open(abs_path) as f:
        report = yaml.safe_load(f)

    snaps = report["snapshots"]
    assert snaps["session_yaml_sha256"] == _sha256_file(good_state["session_yaml"])
    assert snaps["handoff_yaml_sha256"] == _sha256_file(good_state["handoff_yaml"])
    assert snaps["room_yaml_sha256"] == _sha256_file(good_state["room_yaml"])


# ---------------------------------------------------------------------------
# Test 4: git.head_sha matches repo HEAD; clean repo -> worktree_dirty=False
# ---------------------------------------------------------------------------

def test_git_fields_match_repo(good_state, capsys):
    repo = good_state["repo"]
    cmd_session_archive_report(_args(_SESSION_ID))
    abs_path = capsys.readouterr().out.strip()

    with open(abs_path) as f:
        report = yaml.safe_load(f)

    expected_head = _git(repo, "rev-parse", "HEAD").stdout.strip()
    assert report["git"]["head_sha"] == expected_head
    assert report["git"]["worktree_dirty"] is False


# ---------------------------------------------------------------------------
# Test 5: dirty worktree -> worktree_dirty=True
# ---------------------------------------------------------------------------

def test_dirty_worktree_reflected(good_state, capsys):
    repo = good_state["repo"]
    # Touch an untracked file to make the worktree dirty
    dirty_file = os.path.join(repo, "untracked.txt")
    with open(dirty_file, "w") as f:
        f.write("dirty\n")

    cmd_session_archive_report(_args(_SESSION_ID))
    abs_path = capsys.readouterr().out.strip()

    with open(abs_path) as f:
        report = yaml.safe_load(f)

    assert report["git"]["worktree_dirty"] is True


# ---------------------------------------------------------------------------
# Test 6: non-promoted handoff (open status) -> audit_verdict='at-risk'
# ---------------------------------------------------------------------------

def test_open_handoff_produces_at_risk(repo, monkeypatch, capsys):
    _patch_storage(monkeypatch, repo)
    _write_state_files(repo, handoff_status="open", review_outcome="")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "state-yamls")
    monkeypatch.setattr(session_archive_report, "_repo_root", lambda: repo)

    cmd_session_archive_report(_args(_SESSION_ID))
    abs_path = capsys.readouterr().out.strip()

    with open(abs_path) as f:
        report = yaml.safe_load(f)

    assert report["audit_verdict"] == "at-risk"


# ---------------------------------------------------------------------------
# Test 7: completed but review not approved -> audit_verdict='at-risk'
# ---------------------------------------------------------------------------

def test_completed_but_changes_requested_produces_at_risk(repo, monkeypatch, capsys):
    _patch_storage(monkeypatch, repo)
    _write_state_files(repo, handoff_status="completed", review_outcome="changes_requested")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "state-yamls")
    monkeypatch.setattr(session_archive_report, "_repo_root", lambda: repo)

    cmd_session_archive_report(_args(_SESSION_ID))
    abs_path = capsys.readouterr().out.strip()

    with open(abs_path) as f:
        report = yaml.safe_load(f)

    assert report["audit_verdict"] == "at-risk"


# ---------------------------------------------------------------------------
# Test 8: missing session YAML -> sys.exit(1) with stderr
# ---------------------------------------------------------------------------

def test_missing_session_yaml_exits_1(repo, monkeypatch, capsys):
    _patch_storage(monkeypatch, repo)
    monkeypatch.setattr(session_archive_report, "_repo_root", lambda: repo)
    # Do NOT write state files

    with pytest.raises(SystemExit) as exc_info:
        cmd_session_archive_report(_args(_SESSION_ID))
    captured = capsys.readouterr()
    assert exc_info.value.code == 1
    assert "Error" in captured.err


# ---------------------------------------------------------------------------
# Test 9: unsafe slug session_id -> sys.exit(1) with stderr
# ---------------------------------------------------------------------------

def test_unsafe_slug_session_id_exits_1(repo, monkeypatch, capsys):
    _patch_storage(monkeypatch, repo)
    monkeypatch.setattr(session_archive_report, "_repo_root", lambda: repo)

    with pytest.raises(SystemExit) as exc_info:
        cmd_session_archive_report(_args("INVALID SLUG!"))
    captured = capsys.readouterr()
    assert exc_info.value.code == 1
    assert "slug" in captured.err.lower() or "Error" in captured.err


# ---------------------------------------------------------------------------
# Test 10: chained e2e — producer -> archive consumer -> archived
# ---------------------------------------------------------------------------

def test_chained_e2e_producer_then_archive(repo, monkeypatch, capsys):
    """archive-report output piped into session archive -> archived (exit 0)."""
    from lib import session_archive

    _patch_storage(monkeypatch, repo)
    _write_state_files(repo)
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "state-yamls")

    monkeypatch.setattr(session_archive_report, "_repo_root", lambda: repo)
    monkeypatch.setattr(session_archive, "_repo_root", lambda: repo)

    # Step 1: produce report
    # Runtime dir is gitignored so the produced report won't dirty the worktree.
    cmd_session_archive_report(_args(_SESSION_ID))
    report_path = capsys.readouterr().out.strip()
    assert os.path.isabs(report_path)
    assert os.path.isfile(report_path)

    # Step 2: archive using produced report
    arc_args = _archive_args(_SESSION_ID, report_path)
    rc = cmd_session_archive(arc_args)
    captured = capsys.readouterr()
    assert rc == 0, f"expected 0, got {rc}; stderr={captured.err!r}"
    assert captured.out.startswith("archived: ")

    bundle_yaml_path = captured.out.strip().removeprefix("archived: ")
    assert os.path.isfile(bundle_yaml_path)
    assert os.path.isfile(bundle_yaml_path[:-5] + ".md")

    # Session YAML must be stamped
    with open(good_state_session_yaml_path(repo)) as f:
        state = yaml.safe_load(f)
    arc = state["session"]["archive"]
    assert arc["status"] == "archived"

    # Step 3: re-run archive -> already_archived
    # Need fresh report with updated session sha
    cmd_session_archive_report(_args(_SESSION_ID))
    report_path2 = capsys.readouterr().out.strip()
    arc_args2 = _archive_args(_SESSION_ID, report_path2)
    rc2 = cmd_session_archive(arc_args2)
    assert rc2 == 1
    err = capsys.readouterr().err
    assert "already_archived" in err


def good_state_session_yaml_path(repo: str) -> str:
    return os.path.join(
        repo, ".orchestrator", "runtime", "sessions", f"{_SESSION_ID}.yaml"
    )


# ---------------------------------------------------------------------------
# Test 11: collision suffix — same UTC-second produces <ts>-1.yaml
# ---------------------------------------------------------------------------

def test_collision_suffix_on_same_second(good_state, monkeypatch, capsys):
    """If same timestamp fires twice, second file gets -1 suffix."""
    _FIXED_TS = "2026-04-26T00:00:00Z"
    call_count = [0]

    def _fixed_ts():
        call_count[0] += 1
        return _FIXED_TS

    monkeypatch.setattr(session_archive_report, "_utc_now_iso", _fixed_ts)

    # First call
    cmd_session_archive_report(_args(_SESSION_ID))
    path1 = capsys.readouterr().out.strip()
    assert path1.endswith(f"{_FIXED_TS}.yaml"), path1

    # Second call (same timestamp forced)
    cmd_session_archive_report(_args(_SESSION_ID))
    path2 = capsys.readouterr().out.strip()
    assert path2.endswith(f"{_FIXED_TS}-1.yaml"), path2

    # Both files must exist and be distinct
    assert os.path.isfile(path1)
    assert os.path.isfile(path2)
    assert path1 != path2


# ---------------------------------------------------------------------------
# Test 12: top-level review.outcome (canonical layout) -> 'promoted'
# Regression: real handoff YAMLs written by `orchctl handoff approve` store
# review at the top level (sibling to handoff/resolution), not nested under
# handoff. Producer must recognize that layout.
# ---------------------------------------------------------------------------

def test_top_level_review_block_produces_promoted(repo, monkeypatch, capsys):
    _patch_storage(monkeypatch, repo)
    session_yaml = os.path.join(
        repo, ".orchestrator", "runtime", "sessions", f"{_SESSION_ID}.yaml"
    )
    handoff_yaml = os.path.join(
        repo, ".orchestrator", "handoffs", f"{_HANDOFF_ID}.yaml"
    )
    room_yaml = os.path.join(
        repo, ".orchestrator", "rooms", _ROOM_ID, "state.yaml"
    )
    os.makedirs(os.path.dirname(room_yaml), exist_ok=True)
    with open(session_yaml, "w") as f:
        yaml.dump({"session": {"id": _SESSION_ID, "handoff_id": _HANDOFF_ID,
                               "room_id": _ROOM_ID, "status": "idle"}},
                  f, sort_keys=False)
    # Top-level review block (canonical layout)
    with open(handoff_yaml, "w") as f:
        yaml.dump(
            {"handoff": {"id": _HANDOFF_ID, "status": "completed"},
             "review": {"outcome": "approved", "reviewed_by": "cto"}},
            f, sort_keys=False,
        )
    with open(room_yaml, "w") as f:
        yaml.dump({"room": {"id": _ROOM_ID, "status": "active"}},
                  f, sort_keys=False)
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "state-yamls-canonical")

    monkeypatch.setattr(session_archive_report, "_repo_root", lambda: repo)
    cmd_session_archive_report(_args(_SESSION_ID))
    abs_path = capsys.readouterr().out.strip()
    with open(abs_path) as f:
        report = yaml.safe_load(f)
    assert report["audit_verdict"] == "promoted"


# ---------------------------------------------------------------------------
# Test 13: symlink in parent chain before session dir is refused
# Regression: producer's safe_write_text base_dir is reports-root, not the
# session-specific subdir. A pre-existing symlink at <reports-root>/<sid>
# pointing outside the repo (or anywhere) must be refused before write.
# ---------------------------------------------------------------------------

def test_symlink_in_parent_chain_before_session_dir_is_refused(repo, monkeypatch, capsys):
    """Regression: producer's safe_write_text base_dir is reports-root, not the
    session-specific subdir. A pre-existing symlink at <reports-root>/<sid>
    pointing outside the repo (or anywhere) must be refused before write."""
    _patch_storage(monkeypatch, repo)
    _write_state_files(repo)
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "state-yamls")
    monkeypatch.setattr(session_archive_report, "_repo_root", lambda: repo)

    # Pre-plant a symlink at <reports-root>/<sid> pointing outside the repo,
    # BEFORE the producer creates the session dir. The safe_write_text chain
    # walk from target_parent (=<sid>) up to base_dir (=<reports-root>) must
    # detect this symlink and refuse the write.
    reports_root = os.path.join(
        repo, ".orchestrator", "runtime", "session-archive-reports"
    )
    os.makedirs(reports_root, exist_ok=True)
    sid_path = os.path.join(reports_root, _SESSION_ID)
    # Symlink target is outside the repo entirely.
    outside_dir = os.path.join(os.path.dirname(repo), "outside-target")
    os.makedirs(outside_dir, exist_ok=True)
    os.symlink(outside_dir, sid_path)

    args = _args(_SESSION_ID)
    with pytest.raises(SystemExit) as exc_info:
        cmd_session_archive_report(args)
    assert exc_info.value.code == 1
    captured = capsys.readouterr()
    assert "could not write report" in captured.err or "symlink" in captured.err.lower()

    # Outside dir must remain empty (no escape happened).
    assert os.listdir(outside_dir) == []


# ---------------------------------------------------------------------------
# Test A: Symlink ABOVE reports_root is refused
# Regression: base_dir is now .orchestrator, so the chain walk catches symlinks
# at .orchestrator/runtime or .orchestrator/runtime/session-archive-reports.
# ---------------------------------------------------------------------------

def test_symlink_above_reports_root_is_refused(repo, monkeypatch, capsys, tmp_path):
    """Regression: symlink at .orchestrator/runtime/session-archive-reports
    (i.e. ABOVE the per-session subdir) must be refused. base_dir for
    safe_write_text is now .orchestrator, so the chain walk catches this."""
    _patch_storage(monkeypatch, repo)
    _write_state_files(repo)
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "state-yamls")
    monkeypatch.setattr(session_archive_report, "_repo_root", lambda: repo)

    runtime_dir = os.path.join(repo, ".orchestrator", "runtime")
    os.makedirs(runtime_dir, exist_ok=True)
    reports_root = os.path.join(runtime_dir, "session-archive-reports")
    # Pre-plant symlink at reports_root pointing outside the repo entirely.
    # _make_repo creates reports_root as a real dir; remove it first so we
    # can plant the symlink in its place.
    import shutil
    if os.path.isdir(reports_root) and not os.path.islink(reports_root):
        shutil.rmtree(reports_root)
    outside_dir = str(tmp_path / "outside-escape")
    os.makedirs(outside_dir, exist_ok=True)
    os.symlink(outside_dir, reports_root)

    args = _args(_SESSION_ID)
    with pytest.raises(SystemExit) as exc_info:
        cmd_session_archive_report(args)
    assert exc_info.value.code == 1
    captured = capsys.readouterr()
    assert "could not write report" in captured.err or "symlink" in captured.err.lower()
    # No escape: the outside dir must remain empty
    assert os.listdir(outside_dir) == []


# ---------------------------------------------------------------------------
# Test B: Atomic reservation — pre-planted file at first candidate path is
# preserved (simulates concurrent producer occupying the same timestamp path)
# ---------------------------------------------------------------------------

def test_atomic_reservation_does_not_overwrite_concurrent_producer_file(
    good_state, monkeypatch, capsys
):
    """Simulate the race: another producer has already written at <ts>.yaml.
    Our producer (forced to observe the same timestamp) must reserve a
    DIFFERENT path AND leave the pre-planted content untouched."""
    _FIXED_TS = "2026-04-26T08:00:00Z"
    monkeypatch.setattr(session_archive_report, "_utc_now_iso", lambda: _FIXED_TS)

    sid_dir = os.path.join(
        good_state["repo"], ".orchestrator", "runtime",
        "session-archive-reports", _SESSION_ID,
    )
    os.makedirs(sid_dir, exist_ok=True)
    pre_planted = os.path.join(sid_dir, f"{_FIXED_TS}.yaml")
    SENTINEL = "SENTINEL_FROM_OTHER_PRODUCER\nuntouched: true\n"
    with open(pre_planted, "w") as f:
        f.write(SENTINEL)

    cmd_session_archive_report(_args(_SESSION_ID))
    new_path = capsys.readouterr().out.strip()

    # Distinct paths
    assert new_path != pre_planted
    assert new_path.endswith(f"{_FIXED_TS}-1.yaml")
    assert os.path.isfile(new_path)

    # Pre-planted content untouched
    assert open(pre_planted).read() == SENTINEL

    # New path has valid V2 report content
    new_content = yaml.safe_load(open(new_path))
    assert new_content["session_id"] == _SESSION_ID
    assert new_content["audit_verdict"] == "promoted"


# ---------------------------------------------------------------------------
# Test C: O_EXCL race — pre-planted file at both base and -1 path forces -2
# ---------------------------------------------------------------------------

def test_atomic_reservation_skips_multiple_existing_paths(
    good_state, monkeypatch, capsys
):
    _FIXED_TS = "2026-04-26T09:00:00Z"
    monkeypatch.setattr(session_archive_report, "_utc_now_iso", lambda: _FIXED_TS)

    sid_dir = os.path.join(
        good_state["repo"], ".orchestrator", "runtime",
        "session-archive-reports", _SESSION_ID,
    )
    os.makedirs(sid_dir, exist_ok=True)
    p0 = os.path.join(sid_dir, f"{_FIXED_TS}.yaml")
    p1 = os.path.join(sid_dir, f"{_FIXED_TS}-1.yaml")
    with open(p0, "w") as f:
        f.write("S0\n")
    with open(p1, "w") as f:
        f.write("S1\n")

    cmd_session_archive_report(_args(_SESSION_ID))
    new_path = capsys.readouterr().out.strip()
    assert new_path.endswith(f"{_FIXED_TS}-2.yaml")
    assert os.path.isfile(new_path)
    assert open(p0).read() == "S0\n"
    assert open(p1).read() == "S1\n"


# ---------------------------------------------------------------------------
# Test F1A: repo under symlinked ancestor — must succeed
# Finding 1: macOS /var -> /private/var or any above-repo symlink must NOT
# cause false rejection.
# ---------------------------------------------------------------------------

def test_archive_report_succeeds_under_symlinked_ancestor(tmp_path, monkeypatch, capsys):
    """Finding 1: macOS /var -> /private/var or any above-repo symlink must NOT
    cause false rejection. Repo is at <symlinked-parent>/<actual-repo>, with a
    real symlink at the parent level. Producer must exit 0 and write correctly."""
    real_root = tmp_path / "real-root"
    real_root.mkdir()
    repo = real_root / "repo"
    os.makedirs(str(repo))
    # Create the symlink to real_root and use the symlinked path as the apparent repo root
    sym_root = tmp_path / "sym-root"
    os.symlink(str(real_root), str(sym_root))
    apparent_repo = str(sym_root / "repo")
    # Sanity: apparent_repo path contains a symlinked ancestor
    assert os.path.islink(str(sym_root))

    _make_repo(apparent_repo)
    _patch_storage(monkeypatch, apparent_repo)
    _write_state_files(apparent_repo)
    _git(apparent_repo, "add", "-A")
    _git(apparent_repo, "commit", "-q", "-m", "state-yamls")
    monkeypatch.setattr(session_archive_report, "_repo_root", lambda: apparent_repo)

    cmd_session_archive_report(_args(_SESSION_ID))
    abs_path = capsys.readouterr().out.strip()
    assert os.path.isfile(abs_path), f"expected report file, got {abs_path!r}"
    # Content must be valid V2 report
    report = yaml.safe_load(open(abs_path))
    assert report["session_id"] == _SESSION_ID
    assert report["audit_verdict"] == "promoted"


# ---------------------------------------------------------------------------
# Test F2A: dirfd is immune to post-open path swap (helper-level race regression)
# Finding 2: TOCTOU regression. Proves that even if a parent directory is
# swapped to a symlink AFTER fd acquisition, content lands in the original
# inode (not the symlink target).
# ---------------------------------------------------------------------------

def test_dirfd_write_is_immune_to_post_open_path_swap(tmp_path, monkeypatch):
    """Finding 2: TOCTOU regression. Open dirfd to a real session_dir, then
    swap the path on disk: rename real dir aside and symlink the original path
    to an outside-repo target. The next openat(file, dir_fd=fd) must land in
    the original real dir (where fd's inode points), NOT the symlink target.

    This proves the dirfd-based primitive defeats the precheck-then-swap race
    even when the attacker wins the race window."""
    real_sid_dir = tmp_path / "real-sid"
    real_sid_dir.mkdir()
    outside = tmp_path / "outside-escape"
    outside.mkdir()

    # Acquire fd to the real directory (analogous to what _write_archive_report_atomic does)
    fd = os.open(str(real_sid_dir), os.O_DIRECTORY | os.O_CLOEXEC)
    try:
        # Attacker swaps the path on disk: rename real_sid_dir aside, replace
        # with a symlink pointing outside.
        moved = tmp_path / "real-sid-moved"
        os.rename(str(real_sid_dir), str(moved))
        os.symlink(str(outside), str(real_sid_dir))
        # The fd still points to the original inode (now at `moved`).
        # Atomic exclusive create + content write through fd:
        flags = os.O_CREAT | os.O_EXCL | os.O_WRONLY | os.O_NOFOLLOW | os.O_CLOEXEC
        file_fd = os.open("name.yaml", flags, 0o600, dir_fd=fd)
        try:
            os.write(file_fd, b"content-from-original-inode\n")
            os.fsync(file_fd)
        finally:
            os.close(file_fd)
    finally:
        os.close(fd)

    # Content lands in `moved` (the original real dir's inode), NOT in `outside`.
    landed = moved / "name.yaml"
    assert landed.is_file(), f"file should be at original inode location: {landed}"
    assert landed.read_bytes() == b"content-from-original-inode\n"
    # Outside dir must be empty: zero escape.
    assert os.listdir(str(outside)) == []


# ---------------------------------------------------------------------------
# Test F2B: producer-level smoke — symlinked .orchestrator/runtime refused
# with no escape. Preserved invariant from rework-3; verify dirfd refactor
# didn't regress.
# ---------------------------------------------------------------------------

def test_symlink_at_runtime_inside_orchestrator_is_refused(repo, monkeypatch, capsys, tmp_path):
    """Preserved invariant: a symlink AT or BELOW .orchestrator must be refused
    via O_NOFOLLOW. Pre-plant runtime/ -> outside, run producer, exit 1, no escape."""
    _patch_storage(monkeypatch, repo)
    _write_state_files(repo)
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "state-yamls")
    monkeypatch.setattr(session_archive_report, "_repo_root", lambda: repo)

    runtime_dir = os.path.join(repo, ".orchestrator", "runtime")
    # Wipe pre-existing runtime dir from _patch_storage / _make_repo and replace with symlink
    if os.path.exists(runtime_dir) and not os.path.islink(runtime_dir):
        import shutil
        shutil.rmtree(runtime_dir)
    outside = str(tmp_path / "outside-runtime-escape")
    os.makedirs(outside, exist_ok=True)
    os.symlink(outside, runtime_dir)

    args = _args(_SESSION_ID)
    with pytest.raises(SystemExit) as exc_info:
        cmd_session_archive_report(args)
    assert exc_info.value.code == 1
    assert os.listdir(outside) == []


# ---------------------------------------------------------------------------
# Test rework-5: .orchestrator itself being a symlink is refused (Codex P2)
# rework-4 opened .orchestrator with O_DIRECTORY|O_CLOEXEC (no O_NOFOLLOW),
# so a symlinked .orchestrator escaped. rework-5 opens repo_root first (no
# O_NOFOLLOW — above-repo symlinks allowed), then openat('.orchestrator',
# O_NOFOLLOW, dir_fd=repo_fd) to catch the symlink atomically.
# ---------------------------------------------------------------------------

def test_symlink_at_orchestrator_root_is_refused(tmp_path, monkeypatch, capsys):
    """Codex finding (post rework-4): .orchestrator itself being a symlink to
    outside-repo must be refused. rework-4 opened .orchestrator with
    O_DIRECTORY|O_CLOEXEC (no O_NOFOLLOW), so this previously escaped.

    rework-5 fix: open repo_root first (no O_NOFOLLOW so above-repo symlinks
    still succeed), then openat('.orchestrator', O_NOFOLLOW, dir_fd=repo_fd) so
    a symlinked .orchestrator fails atomically with ELOOP."""
    repo = str(tmp_path / "repo")
    os.makedirs(repo)
    # Set up a minimal git repo so producer can run git commands
    _git(repo, "init", "-q")
    _git(repo, "config", "commit.gpgsign", "false")
    _git(repo, "config", "user.name", "Tester")
    _git(repo, "config", "user.email", "tester@example.com")
    seed = os.path.join(repo, "README.md")
    with open(seed, "w") as f:
        f.write("seed\n")
    _git(repo, "add", "README.md")
    _git(repo, "commit", "-q", "-m", "seed")

    # Pre-plant .orchestrator as a symlink to outside-repo dir.
    outside = str(tmp_path / "outside-orchestrator-escape")
    os.makedirs(outside, exist_ok=True)
    # Need session/handoff/room yaml inside outside (so producer can read them
    # via the symlinked path) — but the symlink-refusal must happen at WRITE time
    # before any escape occurs. Set up enough state files inside outside that
    # the producer reaches the write step.
    os.makedirs(os.path.join(outside, "rooms", _ROOM_ID), exist_ok=True)
    os.makedirs(os.path.join(outside, "handoffs"), exist_ok=True)
    os.makedirs(os.path.join(outside, "runtime", "sessions"), exist_ok=True)
    with open(os.path.join(outside, "runtime", "sessions", f"{_SESSION_ID}.yaml"), "w") as f:
        yaml.dump({"session": {"id": _SESSION_ID, "handoff_id": _HANDOFF_ID,
                               "room_id": _ROOM_ID, "status": "idle"}},
                  f, sort_keys=False)
    with open(os.path.join(outside, "handoffs", f"{_HANDOFF_ID}.yaml"), "w") as f:
        yaml.dump({"handoff": {"id": _HANDOFF_ID, "status": "completed",
                               "review": {"outcome": "approved"}}},
                  f, sort_keys=False)
    with open(os.path.join(outside, "rooms", _ROOM_ID, "state.yaml"), "w") as f:
        yaml.dump({"room": {"id": _ROOM_ID, "status": "active"}}, f, sort_keys=False)

    os.symlink(outside, os.path.join(repo, ".orchestrator"))

    # Patch storage and _repo_root so producer treats `repo` as the root.
    _patch_storage(monkeypatch, repo)
    monkeypatch.setattr(session_archive_report, "_repo_root", lambda: repo)

    args = _args(_SESSION_ID)
    with pytest.raises(SystemExit) as exc_info:
        cmd_session_archive_report(args)
    assert exc_info.value.code == 1
    captured = capsys.readouterr()
    assert "could not write report" in captured.err or "symlink" in captured.err.lower()

    # Critical invariant: nothing was written under the outside symlink target.
    # The session-archive-reports tree must NOT have been created in `outside`.
    leaked = []
    reports_in_outside = os.path.join(outside, "runtime", "session-archive-reports")
    if os.path.isdir(reports_in_outside):
        for root, _, files in os.walk(reports_in_outside):
            for fn in files:
                leaked.append(os.path.join(root, fn))
    assert leaked == [], f"unexpected files written to outside symlink target: {leaked}"
