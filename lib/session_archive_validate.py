"""V2 session archive request validation (pure functions).

This module is the gate that V2 archive workflows consult before writing any
archive bundle or session YAML marker. It is **read-only**: no file writes,
no tmux, no stdout, and the only allowed subprocesses are
``git rev-parse HEAD`` and ``git status --porcelain`` for revalidation.

The locked result enum (immutable across V2) is:

    archived | stale_report | unsafe_to_archive | report_mismatch
    | parse_error | already_archived

``validate_archive_request`` returns ``(validated_context, result_enum,
error_message)``. On success ``result_enum == 'archived'``,
``error_message is None``, and ``validated_context`` is a dict with parsed
report, session/handoff/room state snapshots, git info, and the realpath of
the report file. On failure the dict is ``None``.

The validation order is locked. The function returns on the first failure
without aggregating errors.

Contract notes:
- The report YAML schema this module enforces is the V2 contract
  (snapshots.{session,handoff,room}_yaml_sha256 and git.{head_sha,
  worktree_dirty}). Existing gc-audit V1 reports do **not** carry these
  snapshot fields, so they will fail with ``parse_error`` until a separate
  follow-up extends the writer. Modifying gc_audit is out of scope for this
  module.
"""
from __future__ import annotations

import hashlib
import os
import subprocess
from typing import Any, Dict, Optional, Tuple

import yaml

from .validators import is_slug_safe


# Locked result enum --------------------------------------------------------

RESULT_ARCHIVED = "archived"
RESULT_STALE_REPORT = "stale_report"
RESULT_UNSAFE_TO_ARCHIVE = "unsafe_to_archive"
RESULT_REPORT_MISMATCH = "report_mismatch"
RESULT_PARSE_ERROR = "parse_error"
RESULT_ALREADY_ARCHIVED = "already_archived"

VALID_RESULTS = frozenset({
    RESULT_ARCHIVED,
    RESULT_STALE_REPORT,
    RESULT_UNSAFE_TO_ARCHIVE,
    RESULT_REPORT_MISMATCH,
    RESULT_PARSE_ERROR,
    RESULT_ALREADY_ARCHIVED,
})


# Subprocess timeout for git revalidation. Locked per contract.
_GIT_TIMEOUT_SECONDS = 5


_REQUIRED_REPORT_KEYS_TOP = ("session_id", "audit_verdict")
_REQUIRED_REPORT_SNAPSHOT_KEYS = (
    "session_yaml_sha256",
    "handoff_yaml_sha256",
    "room_yaml_sha256",
)
_REQUIRED_REPORT_GIT_KEYS = ("head_sha", "worktree_dirty")


# Failure helper ------------------------------------------------------------

def _fail(result: str, message: str) -> Tuple[Optional[Dict[str, Any]], str, Optional[str]]:
    return (None, result, message)


# Path / containment --------------------------------------------------------

def _is_contained(child_real: str, parent_real: str) -> bool:
    if child_real == parent_real:
        return True
    return child_real.startswith(parent_real + os.sep)


def _parent_chain_has_symlink(repo_real: str, target_path: str) -> bool:
    """Return True if any directory between target_path and repo_real is a
    symlink. Walks logical parents (no realpath) so we detect symlinks before
    they have been resolved away.
    """
    current = os.path.abspath(target_path)
    seen = set()
    while True:
        if current in seen:
            # Defensive loop guard; treat as an unsafe layout.
            return True
        seen.add(current)
        if os.path.islink(current):
            return True
        parent = os.path.dirname(current)
        if parent == current:
            return False
        # Stop walking once we have escaped above the repo root realpath.
        # Compare via realpath of parent so we tolerate the repo itself
        # being mounted via a symlink.
        try:
            if os.path.realpath(parent) == repo_real:
                return False
        except OSError:
            return True
        current = parent


# YAML / hash helpers -------------------------------------------------------

def _safe_load_yaml(path: str) -> Tuple[Optional[Any], Optional[str]]:
    try:
        with open(path, "rb") as f:
            data = f.read()
    except (OSError, ValueError) as exc:
        return None, f"could not read {path}: {exc}"
    try:
        loaded = yaml.safe_load(data)
    except yaml.YAMLError as exc:
        return None, f"yaml could not be parsed: {exc}"
    return loaded, None


def _sha256_of_file(path: str) -> Tuple[Optional[str], Optional[str]]:
    try:
        with open(path, "rb") as f:
            h = hashlib.sha256()
            for chunk in iter(lambda: f.read(65536), b""):
                h.update(chunk)
        return h.hexdigest(), None
    except (OSError, ValueError) as exc:
        return None, f"could not hash {path}: {exc}"


# Git revalidation ----------------------------------------------------------

def _read_git_state(repo_root: str) -> Tuple[Optional[Dict[str, Any]], Optional[str]]:
    """Re-read ``git rev-parse HEAD`` and ``git status --porcelain``.

    Returns (state_dict, error). On any failure (binary missing, timeout,
    non-zero exit) returns (None, error_message). State dict is
    ``{"head_sha": str, "worktree_dirty": bool}``.
    """
    try:
        head_proc = subprocess.run(
            ["git", "-C", repo_root, "rev-parse", "HEAD"],
            capture_output=True,
            text=True,
            timeout=_GIT_TIMEOUT_SECONDS,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired, OSError) as exc:
        return None, f"git rev-parse HEAD failed: {exc}"
    if head_proc.returncode != 0:
        return None, (
            f"git rev-parse HEAD non-zero exit "
            f"({head_proc.returncode}): {head_proc.stderr.strip()}"
        )
    head_sha = head_proc.stdout.strip()

    try:
        status_proc = subprocess.run(
            ["git", "-C", repo_root, "status", "--porcelain"],
            capture_output=True,
            text=True,
            timeout=_GIT_TIMEOUT_SECONDS,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired, OSError) as exc:
        return None, f"git status --porcelain failed: {exc}"
    if status_proc.returncode != 0:
        return None, (
            f"git status --porcelain non-zero exit "
            f"({status_proc.returncode}): {status_proc.stderr.strip()}"
        )
    worktree_dirty = bool(status_proc.stdout.strip())

    return {"head_sha": head_sha, "worktree_dirty": worktree_dirty}, None


# Public entry point --------------------------------------------------------

def validate_archive_request(
    session_id: str,
    report_path: str,
    repo_root: str,
) -> Tuple[Optional[Dict[str, Any]], str, Optional[str]]:
    """Validate and revalidate a V2 session archive request.

    See module docstring for the locked result enum and validation order.
    """
    # ---- (1) report_path must be absolute -------------------------------
    if not isinstance(report_path, str) or not report_path:
        return _fail(RESULT_PARSE_ERROR, "report_path must be a non-empty string")
    if not os.path.isabs(report_path):
        return _fail(RESULT_PARSE_ERROR, f"report_path is not absolute: {report_path!r}")

    if not isinstance(repo_root, str) or not repo_root:
        return _fail(RESULT_PARSE_ERROR, "repo_root must be a non-empty string")

    # ---- (2) containment + symlink escape -------------------------------
    try:
        repo_real = os.path.realpath(repo_root)
    except OSError as exc:
        return _fail(RESULT_UNSAFE_TO_ARCHIVE, f"repo_root realpath failed: {exc}")
    if not os.path.isdir(repo_real):
        return _fail(RESULT_UNSAFE_TO_ARCHIVE, f"repo_root is not a directory: {repo_root!r}")

    # If report_path is itself a symlink we refuse before realpath resolves it.
    if os.path.islink(report_path):
        return _fail(
            RESULT_UNSAFE_TO_ARCHIVE,
            f"report_path is a symlink: {report_path!r}",
        )
    try:
        report_real = os.path.realpath(report_path)
    except OSError as exc:
        return _fail(RESULT_UNSAFE_TO_ARCHIVE, f"report_path realpath failed: {exc}")
    if not _is_contained(report_real, repo_real):
        return _fail(
            RESULT_UNSAFE_TO_ARCHIVE,
            f"report_path escapes repo_root: {report_path!r} -> {report_real!r}",
        )
    # Reject any symlink in the parent chain inside the repo.
    if _parent_chain_has_symlink(repo_real, report_path):
        return _fail(
            RESULT_UNSAFE_TO_ARCHIVE,
            f"report_path parent chain contains a symlink: {report_path!r}",
        )
    if not os.path.isfile(report_real):
        return _fail(
            RESULT_PARSE_ERROR,
            f"report_path does not point at a regular file: {report_path!r}",
        )

    # ---- parse report YAML (parse_error class) --------------------------
    report, err = _safe_load_yaml(report_real)
    if err is not None:
        return _fail(RESULT_PARSE_ERROR, "report yaml could not be parsed")
    if not isinstance(report, dict):
        return _fail(RESULT_PARSE_ERROR, "report yaml is not a mapping")

    # required top-level keys
    for key in _REQUIRED_REPORT_KEYS_TOP:
        if key not in report:
            return _fail(RESULT_PARSE_ERROR, f"report missing required key: {key}")
    snapshots = report.get("snapshots")
    if not isinstance(snapshots, dict):
        return _fail(RESULT_PARSE_ERROR, "report missing snapshots mapping")
    for key in _REQUIRED_REPORT_SNAPSHOT_KEYS:
        if key not in snapshots:
            return _fail(
                RESULT_PARSE_ERROR,
                f"report missing required snapshots.{key}",
            )
    git_info = report.get("git")
    if not isinstance(git_info, dict):
        return _fail(RESULT_PARSE_ERROR, "report missing git mapping")
    for key in _REQUIRED_REPORT_GIT_KEYS:
        if key not in git_info:
            return _fail(
                RESULT_PARSE_ERROR,
                f"report missing required git.{key}",
            )

    report_session_id = report.get("session_id")
    if not isinstance(report_session_id, str) or not report_session_id:
        return _fail(RESULT_PARSE_ERROR, "report.session_id is not a string")

    # ---- (3) session_id arg vs report's session_id ----------------------
    if not isinstance(session_id, str) or not session_id:
        return _fail(RESULT_REPORT_MISMATCH, "session_id argument is empty")
    if session_id != report_session_id:
        return _fail(
            RESULT_REPORT_MISMATCH,
            f"session_id arg {session_id!r} != report.session_id {report_session_id!r}",
        )

    # ---- (4) unsafe slug refs ------------------------------------------
    if not is_slug_safe(session_id):
        return _fail(RESULT_PARSE_ERROR, f"session_id is not slug-safe: {session_id!r}")

    # session YAML must be inside repo at the canonical location.
    session_yaml_path = os.path.join(
        repo_real, ".orchestrator", "runtime", "sessions", f"{session_id}.yaml",
    )
    if not os.path.isfile(session_yaml_path):
        return _fail(
            RESULT_PARSE_ERROR,
            f"session yaml not found at {session_yaml_path!r}",
        )
    session_state, err = _safe_load_yaml(session_yaml_path)
    if err is not None or not isinstance(session_state, dict):
        return _fail(RESULT_PARSE_ERROR, "session yaml could not be parsed")
    s = session_state.get("session")
    if not isinstance(s, dict):
        return _fail(RESULT_PARSE_ERROR, "session yaml missing 'session' mapping")
    handoff_id = s.get("handoff_id")
    room_id = s.get("room_id")
    if not isinstance(handoff_id, str) or not handoff_id:
        return _fail(
            RESULT_PARSE_ERROR,
            "session.handoff_id missing or not a string",
        )
    if not isinstance(room_id, str) or not room_id:
        return _fail(
            RESULT_PARSE_ERROR,
            "session.room_id missing or not a string",
        )
    if not is_slug_safe(handoff_id):
        return _fail(
            RESULT_PARSE_ERROR,
            f"session.handoff_id is not slug-safe: {handoff_id!r}",
        )
    if not is_slug_safe(room_id):
        return _fail(
            RESULT_PARSE_ERROR,
            f"session.room_id is not slug-safe: {room_id!r}",
        )

    # ---- (5) audit_verdict must be 'promoted' --------------------------
    audit_verdict = report.get("audit_verdict")
    if audit_verdict != "promoted":
        return _fail(
            RESULT_UNSAFE_TO_ARCHIVE,
            f"report.audit_verdict is {audit_verdict!r}, not 'promoted'",
        )

    # ---- (6) already archived ------------------------------------------
    archive_block = s.get("archive")
    if isinstance(archive_block, dict) and archive_block.get("status") == "archived":
        return _fail(
            RESULT_ALREADY_ARCHIVED,
            f"session {session_id!r} already has archive.status='archived'",
        )

    # ---- (7) re-hash session/handoff/room YAML --------------------------
    handoff_yaml_path = os.path.join(
        repo_real, ".orchestrator", "handoffs", f"{handoff_id}.yaml",
    )
    room_yaml_path = os.path.join(
        repo_real, ".orchestrator", "rooms", room_id, "state.yaml",
    )
    if not os.path.isfile(handoff_yaml_path):
        return _fail(
            RESULT_STALE_REPORT,
            f"handoff yaml not found at {handoff_yaml_path!r}",
        )
    if not os.path.isfile(room_yaml_path):
        return _fail(
            RESULT_STALE_REPORT,
            f"room yaml not found at {room_yaml_path!r}",
        )

    current_session_sha, err = _sha256_of_file(session_yaml_path)
    if err is not None:
        return _fail(RESULT_STALE_REPORT, err)
    current_handoff_sha, err = _sha256_of_file(handoff_yaml_path)
    if err is not None:
        return _fail(RESULT_STALE_REPORT, err)
    current_room_sha, err = _sha256_of_file(room_yaml_path)
    if err is not None:
        return _fail(RESULT_STALE_REPORT, err)

    if current_session_sha != snapshots.get("session_yaml_sha256"):
        return _fail(
            RESULT_STALE_REPORT,
            "session yaml sha256 differs from report snapshot",
        )
    if current_handoff_sha != snapshots.get("handoff_yaml_sha256"):
        return _fail(
            RESULT_STALE_REPORT,
            "handoff yaml sha256 differs from report snapshot",
        )
    if current_room_sha != snapshots.get("room_yaml_sha256"):
        return _fail(
            RESULT_STALE_REPORT,
            "room yaml sha256 differs from report snapshot",
        )

    # Parse handoff/room YAML for the validated_context. Any read/parse
    # failure here is a stale_report (the snapshots matched, but the file
    # cannot be read into the context — treat as not-fresh).
    handoff_state, err = _safe_load_yaml(handoff_yaml_path)
    if err is not None or not isinstance(handoff_state, dict):
        return _fail(RESULT_STALE_REPORT, "handoff yaml could not be parsed")
    room_state, err = _safe_load_yaml(room_yaml_path)
    if err is not None or not isinstance(room_state, dict):
        return _fail(RESULT_STALE_REPORT, "room yaml could not be parsed")

    # ---- (8) git revalidation ------------------------------------------
    git_state, err = _read_git_state(repo_real)
    if err is not None or git_state is None:
        return _fail(RESULT_STALE_REPORT, err or "git state unavailable")
    expected_head_sha = git_info.get("head_sha")
    expected_dirty = git_info.get("worktree_dirty")
    if not isinstance(expected_head_sha, str) or not expected_head_sha:
        return _fail(RESULT_PARSE_ERROR, "report.git.head_sha is not a string")
    if not isinstance(expected_dirty, bool):
        return _fail(RESULT_PARSE_ERROR, "report.git.worktree_dirty is not a bool")
    if git_state["head_sha"] != expected_head_sha:
        return _fail(
            RESULT_STALE_REPORT,
            f"git HEAD drifted: {git_state['head_sha']!r} != {expected_head_sha!r}",
        )
    if git_state["worktree_dirty"] != expected_dirty:
        return _fail(
            RESULT_STALE_REPORT,
            (
                f"git worktree dirty-state drifted: "
                f"{git_state['worktree_dirty']!r} != {expected_dirty!r}"
            ),
        )

    # ---- success --------------------------------------------------------
    validated_context: Dict[str, Any] = {
        "report": report,
        "report_path": report_real,
        "session_id": session_id,
        "handoff_id": handoff_id,
        "room_id": room_id,
        "session_state": session_state,
        "handoff_state": handoff_state,
        "room_state": room_state,
        "snapshots": {
            "session_yaml_sha256": current_session_sha,
            "handoff_yaml_sha256": current_handoff_sha,
            "room_yaml_sha256": current_room_sha,
        },
        "git": {
            "head_sha": git_state["head_sha"],
            "worktree_dirty": git_state["worktree_dirty"],
        },
    }
    return (validated_context, RESULT_ARCHIVED, None)
