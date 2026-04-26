"""Tests for `lib/session_archive_validate.py` (V2 session archive validation).

All tests are hermetic: tmp dirs, no real tmux, real git only inside an
isolated tmp git repo. The module under test is purely read-only — these
tests verify that invariant explicitly via source grep + behavioral checks.
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

from lib import session_archive_validate as sav  # noqa: E402


# ---------------------------------------------------------------------------
# Fixture / helpers
# ---------------------------------------------------------------------------

_SESSION_ID = "session-foo-1"
_HANDOFF_ID = "handoff-foo-1"
_ROOM_ID = "room-foo-1"


def _sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


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
        raise RuntimeError(
            f"git {' '.join(args)} failed: {proc.stderr.strip()}"
        )
    return proc


def _make_repo(base: str) -> str:
    """Create a tmp git repo with .orchestrator skeleton + initial commit.

    The runtime gc-audits dir is gitignored so report files written by tests
    do not perturb ``git status --porcelain``.
    """
    repo = base
    os.makedirs(os.path.join(repo, ".orchestrator", "rooms", _ROOM_ID), exist_ok=True)
    os.makedirs(os.path.join(repo, ".orchestrator", "handoffs"), exist_ok=True)
    os.makedirs(
        os.path.join(repo, ".orchestrator", "runtime", "sessions"),
        exist_ok=True,
    )
    os.makedirs(
        os.path.join(repo, ".orchestrator", "runtime", "gc-audits"),
        exist_ok=True,
    )

    _git(repo, "init", "-q")
    _git(repo, "config", "commit.gpgsign", "false")
    _git(repo, "config", "user.name", "Tester")
    _git(repo, "config", "user.email", "tester@example.com")

    # Seed an initial commit so HEAD exists.
    seed = os.path.join(repo, "README.md")
    with open(seed, "w") as f:
        f.write("seed\n")
    gitignore = os.path.join(repo, ".gitignore")
    with open(gitignore, "w") as f:
        f.write(".orchestrator/runtime/gc-audits/\n")
    _git(repo, "add", "README.md", ".gitignore")
    _git(repo, "commit", "-q", "-m", "seed")
    return repo


def _write_state_files(repo: str):
    session_yaml = os.path.join(
        repo, ".orchestrator", "runtime", "sessions", f"{_SESSION_ID}.yaml",
    )
    handoff_yaml = os.path.join(
        repo, ".orchestrator", "handoffs", f"{_HANDOFF_ID}.yaml",
    )
    room_yaml = os.path.join(
        repo, ".orchestrator", "rooms", _ROOM_ID, "state.yaml",
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
            f,
            sort_keys=False,
        )
    with open(handoff_yaml, "w") as f:
        yaml.dump(
            {"handoff": {"id": _HANDOFF_ID, "status": "completed"}},
            f,
            sort_keys=False,
        )
    with open(room_yaml, "w") as f:
        yaml.dump(
            {"room": {"id": _ROOM_ID, "status": "active"}},
            f,
            sort_keys=False,
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
    head_sha = (head_sha_override
                if head_sha_override is not None
                else head_proc.stdout.strip())

    status_proc = _git(repo, "status", "--porcelain")
    if worktree_dirty_override is not None:
        worktree_dirty = worktree_dirty_override
    else:
        worktree_dirty = bool(status_proc.stdout.strip())

    return {
        "session_id": session_id,
        "audit_verdict": audit_verdict,
        "snapshots": {
            "session_yaml_sha256": (session_sha_override
                                    if session_sha_override is not None
                                    else _sha256_file(session_yaml)),
            "handoff_yaml_sha256": (handoff_sha_override
                                    if handoff_sha_override is not None
                                    else _sha256_file(handoff_yaml)),
            "room_yaml_sha256": (room_sha_override
                                 if room_sha_override is not None
                                 else _sha256_file(room_yaml)),
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


@pytest.fixture
def repo(tmp_path):
    base = str(tmp_path / "repo")
    os.makedirs(base, exist_ok=True)
    _make_repo(base)
    return base


@pytest.fixture
def good_state(repo):
    """Repo with a fresh, well-formed report at gc-audits/good.yaml.

    State YAMLs are committed so the worktree is clean at the moment the
    report is built; the gc-audits dir is gitignored so the report file
    itself does not dirty the worktree.
    """
    session_yaml, handoff_yaml, room_yaml = _write_state_files(repo)
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "state-yamls")
    report = _build_report(repo, session_yaml, handoff_yaml, room_yaml)
    report_path = _write_report(repo, "good.yaml", report)
    return {
        "repo": repo,
        "session_yaml": session_yaml,
        "handoff_yaml": handoff_yaml,
        "room_yaml": room_yaml,
        "report": report,
        "report_path": report_path,
    }


# ---------------------------------------------------------------------------
# Step 1 — relative report_path -> parse_error
# ---------------------------------------------------------------------------

def test_relative_report_path_returns_parse_error(good_state):
    relative = os.path.relpath(good_state["report_path"])
    assert not os.path.isabs(relative)
    ctx, result, msg = sav.validate_archive_request(
        _SESSION_ID, relative, good_state["repo"],
    )
    assert ctx is None
    assert result == sav.RESULT_PARSE_ERROR
    assert msg


def test_empty_report_path_returns_parse_error(good_state):
    ctx, result, _ = sav.validate_archive_request(
        _SESSION_ID, "", good_state["repo"],
    )
    assert ctx is None
    assert result == sav.RESULT_PARSE_ERROR


# ---------------------------------------------------------------------------
# Step 2 — repo containment + symlink escape -> unsafe_to_archive
# ---------------------------------------------------------------------------

def test_report_outside_repo_returns_unsafe_to_archive(good_state, tmp_path):
    outside = tmp_path / "outside-report.yaml"
    with open(outside, "w") as f:
        yaml.dump(good_state["report"], f)
    ctx, result, msg = sav.validate_archive_request(
        _SESSION_ID, str(outside), good_state["repo"],
    )
    assert ctx is None
    assert result == sav.RESULT_UNSAFE_TO_ARCHIVE
    assert "escapes" in msg or "symlink" in msg


def test_symlink_escape_outside_repo_returns_unsafe_to_archive(
    good_state, tmp_path,
):
    """Cited symlink-escape test (REQUIRED by reporting contract)."""
    secret = tmp_path / "outside-secret.yaml"
    with open(secret, "w") as f:
        yaml.dump(good_state["report"], f)

    link = os.path.join(good_state["repo"], "evil-link.yaml")
    os.symlink(str(secret), link)

    ctx, result, msg = sav.validate_archive_request(
        _SESSION_ID, link, good_state["repo"],
    )
    assert ctx is None
    assert result == sav.RESULT_UNSAFE_TO_ARCHIVE
    assert "symlink" in msg.lower()


# ---------------------------------------------------------------------------
# Step 3 — session_id arg vs report.session_id -> report_mismatch
# ---------------------------------------------------------------------------

def test_session_id_mismatch_returns_report_mismatch(good_state):
    ctx, result, _ = sav.validate_archive_request(
        "session-bar-9", good_state["report_path"], good_state["repo"],
    )
    assert ctx is None
    assert result == sav.RESULT_REPORT_MISMATCH


# ---------------------------------------------------------------------------
# Step 4 — unsafe slug refs -> parse_error
# ---------------------------------------------------------------------------

def test_unsafe_session_id_slug_returns_parse_error(good_state):
    """session_id with path traversal must be rejected as parse_error.

    The matching report carries the same unsafe session_id so the function
    progresses past the mismatch step and trips the slug-safety guard.
    """
    bad_session_id = "../etc/passwd"
    bad_report = dict(good_state["report"])
    bad_report["session_id"] = bad_session_id
    bad_report_path = _write_report(good_state["repo"], "bad-slug.yaml", bad_report)

    ctx, result, _ = sav.validate_archive_request(
        bad_session_id, bad_report_path, good_state["repo"],
    )
    assert ctx is None
    assert result == sav.RESULT_PARSE_ERROR


def test_unsafe_handoff_id_slug_returns_parse_error(good_state):
    """A tampered session yaml carrying an unsafe handoff_id slug."""
    with open(good_state["session_yaml"], "w") as f:
        yaml.dump(
            {
                "session": {
                    "id": _SESSION_ID,
                    "handoff_id": "../../../etc/passwd",
                    "room_id": _ROOM_ID,
                }
            },
            f,
            sort_keys=False,
        )
    # Rebuild the report so its snapshot.session_yaml_sha256 reflects new file.
    report = _build_report(
        good_state["repo"],
        good_state["session_yaml"],
        good_state["handoff_yaml"],
        good_state["room_yaml"],
    )
    report_path = _write_report(good_state["repo"], "bad-handoff.yaml", report)

    ctx, result, _ = sav.validate_archive_request(
        _SESSION_ID, report_path, good_state["repo"],
    )
    assert ctx is None
    assert result == sav.RESULT_PARSE_ERROR


# ---------------------------------------------------------------------------
# Step 5 — audit_verdict gate -> unsafe_to_archive
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("verdict", ["at-risk", "unbound", "parse-error"])
def test_non_promoted_audit_verdict_returns_unsafe_to_archive(good_state, verdict):
    bad_report = dict(good_state["report"])
    bad_report["audit_verdict"] = verdict
    path = _write_report(good_state["repo"], f"bad-verdict-{verdict}.yaml", bad_report)
    ctx, result, _ = sav.validate_archive_request(
        _SESSION_ID, path, good_state["repo"],
    )
    assert ctx is None
    assert result == sav.RESULT_UNSAFE_TO_ARCHIVE


# ---------------------------------------------------------------------------
# Step 6 — already archived
# ---------------------------------------------------------------------------

def test_already_archived_session_returns_already_archived(good_state):
    # Stamp the session YAML with archive.status='archived' and rebuild report.
    with open(good_state["session_yaml"], "w") as f:
        yaml.dump(
            {
                "session": {
                    "id": _SESSION_ID,
                    "handoff_id": _HANDOFF_ID,
                    "room_id": _ROOM_ID,
                    "archive": {"status": "archived"},
                }
            },
            f,
            sort_keys=False,
        )
    report = _build_report(
        good_state["repo"],
        good_state["session_yaml"],
        good_state["handoff_yaml"],
        good_state["room_yaml"],
    )
    path = _write_report(good_state["repo"], "already-archived.yaml", report)
    ctx, result, _ = sav.validate_archive_request(
        _SESSION_ID, path, good_state["repo"],
    )
    assert ctx is None
    assert result == sav.RESULT_ALREADY_ARCHIVED


# ---------------------------------------------------------------------------
# Step 7 — yaml hash drift -> stale_report
# ---------------------------------------------------------------------------

def test_session_yaml_hash_drift_returns_stale_report(good_state):
    # Mutate session yaml after the report was built.
    with open(good_state["session_yaml"], "a") as f:
        f.write("# drift\n")
    ctx, result, _ = sav.validate_archive_request(
        _SESSION_ID, good_state["report_path"], good_state["repo"],
    )
    assert ctx is None
    assert result == sav.RESULT_STALE_REPORT


def test_handoff_yaml_hash_drift_returns_stale_report(good_state):
    with open(good_state["handoff_yaml"], "a") as f:
        f.write("# drift\n")
    ctx, result, _ = sav.validate_archive_request(
        _SESSION_ID, good_state["report_path"], good_state["repo"],
    )
    assert ctx is None
    assert result == sav.RESULT_STALE_REPORT


def test_room_yaml_hash_drift_returns_stale_report(good_state):
    with open(good_state["room_yaml"], "a") as f:
        f.write("# drift\n")
    ctx, result, _ = sav.validate_archive_request(
        _SESSION_ID, good_state["report_path"], good_state["repo"],
    )
    assert ctx is None
    assert result == sav.RESULT_STALE_REPORT


# ---------------------------------------------------------------------------
# Step 8 — git drift -> stale_report
# ---------------------------------------------------------------------------

def test_git_head_drift_returns_stale_report(good_state):
    # Add a new commit so HEAD moves after the report was built.
    extra = os.path.join(good_state["repo"], "extra.txt")
    with open(extra, "w") as f:
        f.write("post-report\n")
    _git(good_state["repo"], "add", "extra.txt")
    _git(good_state["repo"], "commit", "-q", "-m", "post-report")
    ctx, result, _ = sav.validate_archive_request(
        _SESSION_ID, good_state["report_path"], good_state["repo"],
    )
    assert ctx is None
    assert result == sav.RESULT_STALE_REPORT


def test_git_worktree_dirty_drift_returns_stale_report(good_state):
    # Build a report claiming clean, then dirty the worktree.
    extra = os.path.join(good_state["repo"], "dirty.txt")
    with open(extra, "w") as f:
        f.write("dirty\n")
    ctx, result, _ = sav.validate_archive_request(
        _SESSION_ID, good_state["report_path"], good_state["repo"],
    )
    assert ctx is None
    assert result == sav.RESULT_STALE_REPORT


# ---------------------------------------------------------------------------
# Parse-error edges
# ---------------------------------------------------------------------------

def test_malformed_yaml_report_returns_parse_error(good_state):
    bad_path = os.path.join(
        good_state["repo"], ".orchestrator", "runtime", "gc-audits", "broken.yaml",
    )
    with open(bad_path, "w") as f:
        f.write("not: a: valid: yaml: : :\n  -\n  -\n :\n")
    ctx, result, msg = sav.validate_archive_request(
        _SESSION_ID, bad_path, good_state["repo"],
    )
    assert ctx is None
    assert result == sav.RESULT_PARSE_ERROR


def test_missing_required_key_returns_parse_error(good_state):
    bad = dict(good_state["report"])
    del bad["audit_verdict"]
    path = _write_report(good_state["repo"], "missing-key.yaml", bad)
    ctx, result, msg = sav.validate_archive_request(
        _SESSION_ID, path, good_state["repo"],
    )
    assert ctx is None
    assert result == sav.RESULT_PARSE_ERROR
    assert "audit_verdict" in msg


def test_missing_snapshots_block_returns_parse_error(good_state):
    bad = dict(good_state["report"])
    bad.pop("snapshots", None)
    path = _write_report(good_state["repo"], "missing-snapshots.yaml", bad)
    ctx, result, _ = sav.validate_archive_request(
        _SESSION_ID, path, good_state["repo"],
    )
    assert ctx is None
    assert result == sav.RESULT_PARSE_ERROR


def test_missing_git_block_returns_parse_error(good_state):
    bad = dict(good_state["report"])
    bad.pop("git", None)
    path = _write_report(good_state["repo"], "missing-git.yaml", bad)
    ctx, result, _ = sav.validate_archive_request(
        _SESSION_ID, path, good_state["repo"],
    )
    assert ctx is None
    assert result == sav.RESULT_PARSE_ERROR


# ---------------------------------------------------------------------------
# Success path
# ---------------------------------------------------------------------------

def test_fresh_promoted_report_returns_archived(good_state):
    ctx, result, msg = sav.validate_archive_request(
        _SESSION_ID, good_state["report_path"], good_state["repo"],
    )
    assert result == sav.RESULT_ARCHIVED
    assert msg is None
    assert isinstance(ctx, dict)
    expected_keys = {
        "report", "report_path", "session_id", "handoff_id", "room_id",
        "session_state", "handoff_state", "room_state", "snapshots", "git",
    }
    assert expected_keys.issubset(ctx.keys())
    assert ctx["session_id"] == _SESSION_ID
    assert ctx["handoff_id"] == _HANDOFF_ID
    assert ctx["room_id"] == _ROOM_ID
    # report_path is a normalized realpath inside the repo.
    assert os.path.isabs(ctx["report_path"])
    assert ctx["report_path"] == os.path.realpath(good_state["report_path"])
    # snapshots and git fields populated from current state, not from report
    # (so callers can compare them if they want).
    assert isinstance(ctx["snapshots"]["session_yaml_sha256"], str)
    assert isinstance(ctx["git"]["head_sha"], str)
    assert ctx["git"]["worktree_dirty"] is False


# ---------------------------------------------------------------------------
# Result-enum coverage
# ---------------------------------------------------------------------------

def test_result_enum_lock():
    # The locked enum is immutable across V2.
    assert sav.VALID_RESULTS == frozenset({
        "archived",
        "stale_report",
        "unsafe_to_archive",
        "report_mismatch",
        "parse_error",
        "already_archived",
    })


# ---------------------------------------------------------------------------
# Source-grep invariants
# ---------------------------------------------------------------------------

def _module_code_only(src: str) -> str:
    """Return module source with comments stripped via tokenize.

    Docstrings (string literals) are kept because string-literal arguments
    to ``subprocess.run([..., "git", ...])`` need to be visible to the
    grep — but the natural-language docstring at module top is also kept,
    so callers that grep for free-text words must scope around the code
    region. Use ``_module_executable_strings`` for argv-only scope.
    """
    import io
    import tokenize
    out = []
    g = tokenize.generate_tokens(io.StringIO(src).readline)
    for tok in g:
        if tok.type == tokenize.COMMENT:
            continue
        out.append(tok.string)
    return " ".join(out)


def _module_executable_text(src: str) -> str:
    """Return module text with both comments and string literals removed.

    This is the strictest view: only Python identifiers, operators, and
    keywords. It is the right scope for free-text fingerprints like
    ``tmux`` and ``send-keys`` that must never appear as code.
    """
    import io
    import tokenize
    out = []
    g = tokenize.generate_tokens(io.StringIO(src).readline)
    for tok in g:
        if tok.type in (tokenize.COMMENT, tokenize.STRING):
            continue
        out.append(tok.string)
    return " ".join(out)


def test_module_source_has_no_disallowed_io_calls():
    """No file writes, no tmux, no shell, no commit/push in executable code."""
    src_path = os.path.join(_REPO_ROOT, "lib", "session_archive_validate.py")
    with open(src_path) as f:
        src = f.read()

    code = _module_executable_text(src)

    forbidden_in_code = [
        r"\bos\.write\b",
        r"\bsafe_write_text\b",
        r"\batomic_write\b",
        r"\btmux\b",
        r"\bsend_keys\b",
        r"\bkill_session\b",
        r"\brespawn_pane\b",
        r"\bos\.system\b",
        r"\bsubprocess\.Popen\b",
        r"\bsubprocess\.call\b",
        r"\bsubprocess\.check_call\b",
        r"\bsubprocess\.check_output\b",
    ]
    for pat in forbidden_in_code:
        assert not re.search(pat, code), (
            f"forbidden pattern matched in executable code: {pat!r}"
        )

    # String-literal scan: any open(...) with a write/append/exclusive mode.
    write_open_re = re.compile(
        r'open\s*\([^)]*?["\']([rwabx+t]*[wax+][rwabx+t]*)["\']'
    )
    assert not write_open_re.search(src), (
        "module appears to call open() with a write-mode literal"
    )

    # Disallowed git verbs anywhere (string literals included).
    for verb in ("commit", "push", "fetch"):
        assert not re.search(rf'["\']{verb}["\']', src), (
            f"module references disallowed git verb {verb!r}"
        )

    # __main__ entry point must NOT exist (pure module, never invokable).
    assert "__main__" not in src


def test_module_only_uses_allowed_git_subcommands():
    """Allowed git argvs: ('rev-parse', 'HEAD') and ('status', '--porcelain')."""
    src_path = os.path.join(_REPO_ROOT, "lib", "session_archive_validate.py")
    with open(src_path) as f:
        src = f.read()
    # An argv list always contains a `"-C"` token or a verb adjacent to "git".
    # Restrict to lines that look like argv elements (start with leading
    # whitespace + bracket or comma-leading element).
    argv_lines = [
        ln for ln in src.splitlines()
        if re.search(r'\[\s*["\']git["\']', ln)
    ]
    assert len(argv_lines) == 2, argv_lines
    assert any("rev-parse" in ln for ln in argv_lines) or any(
        "rev-parse" in nxt
        for ln, nxt in zip(src.splitlines(), src.splitlines()[1:])
        if re.search(r'\[\s*["\']git["\']', ln)
    )
    # Verbs in subprocess argvs (next token after "-C", repo).
    assert "rev-parse" in src
    assert "--porcelain" in src
