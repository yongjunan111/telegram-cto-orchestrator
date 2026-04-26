"""V2 session archive-report producer.

Reads session/handoff/room YAML, computes sha256 snapshots, runs git plumbing,
derives audit_verdict, and writes a V2-contract report YAML to:
  .orchestrator/runtime/session-archive-reports/<session-id>/<UTC-iso-ts>.yaml

Public entrypoint: cmd_session_archive_report(args)

No __main__ block. No subprocess against tmux. No writes to room/handoff/session
YAML (read-only on those). No cron/hooks/idle automation.
"""
from __future__ import annotations

import hashlib
import os
import subprocess
import sys
from datetime import datetime, timezone
from typing import Any, Dict, Optional, Tuple

import yaml

from .validators import is_slug_safe
from . import storage  # noqa: F401 — kept for tests/other callers that import storage via this module


# ---------------------------------------------------------------------------
# Repo root helper (mirrors lib/session_archive._repo_root)
# ---------------------------------------------------------------------------

def _repo_root() -> str:
    """Compute repo root from this file's location (lib/ -> parent)."""
    return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


# ---------------------------------------------------------------------------
# Clock helper (isolated for monkeypatching in tests)
# ---------------------------------------------------------------------------

def _utc_now_iso() -> str:
    """Return current UTC time as ISO-8601 second-precision string."""
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


# ---------------------------------------------------------------------------
# SHA-256 helper (matches session_archive_validate._sha256_of_file exactly)
# ---------------------------------------------------------------------------

def _sha256_of_file(path: str) -> Tuple[Optional[str], Optional[str]]:
    try:
        with open(path, "rb") as f:
            h = hashlib.sha256()
            for chunk in iter(lambda: f.read(65536), b""):
                h.update(chunk)
        return h.hexdigest(), None
    except (OSError, ValueError) as exc:
        return None, f"could not hash {path}: {exc}"


# ---------------------------------------------------------------------------
# Git plumbing (matches session_archive_validate._read_git_state exactly)
# ---------------------------------------------------------------------------

_GIT_TIMEOUT_SECONDS = 5


def _read_git_state(repo_root: str) -> Tuple[Optional[Dict[str, Any]], Optional[str]]:
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


# ---------------------------------------------------------------------------
# audit_verdict derivation
# ---------------------------------------------------------------------------

def _derive_audit_verdict(handoff_state: Dict[str, Any]) -> str:
    """Return 'promoted' if handoff is completed+approved, else 'at-risk'.

    Three known layouts for the review block:
    - top-level ``review.outcome`` (canonical, written by `orchctl handoff approve`)
    - ``handoff.review.outcome`` (legacy nested layout)
    - ``resolution.review.outcome`` (alternate nested layout)

    Fail-safe to 'at-risk' on any ambiguity.
    """
    if not isinstance(handoff_state, dict):
        return "at-risk"

    handoff = handoff_state.get("handoff")
    if not isinstance(handoff, dict):
        return "at-risk"

    # Must be completed
    if handoff.get("status") != "completed":
        return "at-risk"

    # Layout 1 (canonical): top-level review.outcome
    top_review = handoff_state.get("review")
    if isinstance(top_review, dict) and top_review.get("outcome") == "approved":
        return "promoted"

    # Layout 2 (legacy): handoff.review.outcome
    review = handoff.get("review")
    if isinstance(review, dict) and review.get("outcome") == "approved":
        return "promoted"

    # Layout 3 (alternate): resolution.review.outcome
    resolution = handoff_state.get("resolution")
    if isinstance(resolution, dict):
        res_review = resolution.get("review")
        if isinstance(res_review, dict) and res_review.get("outcome") == "approved":
            return "promoted"

    return "at-risk"


# ---------------------------------------------------------------------------
# dirfd-based atomic writer (fixes F1 scope-too-broad + F2 TOCTOU)
# ---------------------------------------------------------------------------

def _open_or_create_dir_nofollow(parent_fd: int, name: str) -> int:
    """openat(parent_fd, name, O_DIRECTORY|O_NOFOLLOW).  If ENOENT, mkdirat then retry.

    O_NOFOLLOW makes openat fail with ELOOP (raised as OSError) if ``name`` is
    a symlink.  This is atomic at the syscall level — no TOCTOU window.
    Raises OSError (including ELOOP) on any symlink or filesystem failure.
    """
    flags = os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
    try:
        return os.open(name, flags, dir_fd=parent_fd)
    except FileNotFoundError:
        os.mkdir(name, dir_fd=parent_fd)
        return os.open(name, flags, dir_fd=parent_fd)
    # ELOOP / other OSError from O_NOFOLLOW propagates — caller refuses.


def _write_archive_report_atomic(
    repo_root: str,
    session_id: str,
    timestamp: str,
    content: str,
    max_attempts: int = 10000,
) -> str:
    """Atomically create and write a V2 archive report under
    .orchestrator/runtime/session-archive-reports/<sid>/<ts>[-N].yaml.

    Uses dirfd-based traversal with O_NOFOLLOW from .orchestrator downward —
    refusing any symlink at runtime/, session-archive-reports/, or <sid>/.
    Symlinks ABOVE .orchestrator are out of scope (operator trust boundary).

    File creation uses O_CREAT|O_EXCL|O_WRONLY|O_NOFOLLOW with dir_fd, writing
    content directly (no placeholder + replace, no separate safe_write_text).

    Once we hold an fd to a directory inode, even if the path string on disk is
    swapped to a symlink, all subsequent openat calls land in the original inode
    — defeating the precheck-then-swap TOCTOU race entirely.

    Returns the absolute path of the written file.
    Raises OSError on filesystem failure or refused symlink.
    """
    descend = ("runtime", "session-archive-reports", session_id)
    # Step A: open repo_root WITHOUT O_NOFOLLOW — above-repo symlinks (e.g.,
    # macOS /var -> /private/var) are operator-level and must continue to
    # succeed (rework-4 invariant).
    repo_fd = os.open(repo_root, os.O_DIRECTORY | os.O_CLOEXEC)
    try:
        # Step B: open .orchestrator FROM repo_fd WITH O_NOFOLLOW. If
        # .orchestrator itself is a symlink, openat fails atomically with
        # ELOOP. .orchestrator is the trust boundary — it must point at a real
        # directory inside the repo, not a symlink that redirects the entire
        # write tree. This closes the rework-4 escape hole found by Codex.
        fd = _open_or_create_dir_nofollow(repo_fd, ".orchestrator")
    finally:
        os.close(repo_fd)
    try:
        # Step C: rework-4 dirfd descent for runtime/session-archive-reports/<sid>,
        # with O_NOFOLLOW on each step.
        for part in descend:
            new_fd = _open_or_create_dir_nofollow(fd, part)
            os.close(fd)
            fd = new_fd
        # fd now points to the session_dir inode.  Even if the path string is
        # swapped to a symlink concurrently, all subsequent openat calls land
        # in this inode (not the symlink target).
        for attempt in range(max_attempts):
            name = f"{timestamp}.yaml" if attempt == 0 else f"{timestamp}-{attempt}.yaml"
            flags = (
                os.O_CREAT | os.O_EXCL | os.O_WRONLY
                | os.O_NOFOLLOW | os.O_CLOEXEC
            )
            try:
                file_fd = os.open(name, flags, 0o600, dir_fd=fd)
            except FileExistsError:
                continue
            try:
                data = content.encode("utf-8")
                while data:
                    written = os.write(file_fd, data)
                    if written <= 0:
                        raise OSError("os.write returned non-positive")
                    data = data[written:]
                os.fsync(file_fd)
            finally:
                os.close(file_fd)
            return os.path.join(repo_root, ".orchestrator", *descend, name)
        raise OSError(
            f"could not reserve unique report filename under "
            f"<.orchestrator>/{'/'.join(descend)} after {max_attempts} attempts"
        )
    finally:
        os.close(fd)


# ---------------------------------------------------------------------------
# Public entrypoint
# ---------------------------------------------------------------------------

def cmd_session_archive_report(args: Any) -> None:
    """orchctl session archive-report <session-id>.

    args.session_id: str

    Reads session/handoff/room YAML, computes sha256s, calls git rev-parse +
    status, derives audit_verdict, and writes a V2-contract report to
    .orchestrator/runtime/session-archive-reports/<session-id>/<UTC-iso-ts>.yaml.
    Prints the absolute path on stdout. Exits 0 on success, non-zero on failure
    via sys.exit (matches existing CLI pattern in this codebase for V1).
    """
    session_id = args.session_id

    # ---- slug check --------------------------------------------------------
    if not is_slug_safe(session_id):
        print(
            f"Error: session_id {session_id!r} is not slug-safe "
            f"(lowercase, digits, hyphens, 1-64 chars, no leading/trailing hyphen).",
            file=sys.stderr,
        )
        sys.exit(1)

    repo_root = _repo_root()

    # ---- resolve session YAML ----------------------------------------------
    session_yaml_path = os.path.join(
        repo_root, ".orchestrator", "runtime", "sessions", f"{session_id}.yaml"
    )
    if not os.path.isfile(session_yaml_path):
        print(
            f"Error: session YAML not found: {session_yaml_path!r}",
            file=sys.stderr,
        )
        sys.exit(1)

    # ---- parse session YAML -----------------------------------------------
    try:
        with open(session_yaml_path, "rb") as f:
            session_state = yaml.safe_load(f.read())
    except Exception as exc:
        print(f"Error: could not parse session YAML: {exc}", file=sys.stderr)
        sys.exit(1)

    if not isinstance(session_state, dict):
        print("Error: session YAML is not a mapping.", file=sys.stderr)
        sys.exit(1)

    s = session_state.get("session")
    if not isinstance(s, dict):
        print("Error: session YAML missing 'session' mapping.", file=sys.stderr)
        sys.exit(1)

    handoff_id = s.get("handoff_id")
    room_id = s.get("room_id")

    if not isinstance(handoff_id, str) or not handoff_id:
        print(
            "Error: session YAML missing or invalid 'session.handoff_id'.",
            file=sys.stderr,
        )
        sys.exit(1)
    if not isinstance(room_id, str) or not room_id:
        print(
            "Error: session YAML missing or invalid 'session.room_id'.",
            file=sys.stderr,
        )
        sys.exit(1)

    # ---- slug check handoff_id / room_id -----------------------------------
    if not is_slug_safe(handoff_id):
        print(
            f"Error: session.handoff_id {handoff_id!r} is not slug-safe.",
            file=sys.stderr,
        )
        sys.exit(1)
    if not is_slug_safe(room_id):
        print(
            f"Error: session.room_id {room_id!r} is not slug-safe.",
            file=sys.stderr,
        )
        sys.exit(1)

    # ---- resolve handoff / room YAML paths ---------------------------------
    handoff_yaml_path = os.path.join(
        repo_root, ".orchestrator", "handoffs", f"{handoff_id}.yaml"
    )
    room_yaml_path = os.path.join(
        repo_root, ".orchestrator", "rooms", room_id, "state.yaml"
    )

    if not os.path.isfile(handoff_yaml_path):
        print(
            f"Error: handoff YAML not found: {handoff_yaml_path!r}",
            file=sys.stderr,
        )
        sys.exit(1)
    if not os.path.isfile(room_yaml_path):
        print(
            f"Error: room YAML not found: {room_yaml_path!r}",
            file=sys.stderr,
        )
        sys.exit(1)

    # ---- parse handoff YAML (for audit_verdict) ----------------------------
    try:
        with open(handoff_yaml_path, "rb") as f:
            handoff_state = yaml.safe_load(f.read())
    except Exception as exc:
        print(f"Error: could not parse handoff YAML: {exc}", file=sys.stderr)
        sys.exit(1)

    # ---- sha256 snapshots --------------------------------------------------
    session_sha, err = _sha256_of_file(session_yaml_path)
    if err:
        print(f"Error: {err}", file=sys.stderr)
        sys.exit(1)
    handoff_sha, err = _sha256_of_file(handoff_yaml_path)
    if err:
        print(f"Error: {err}", file=sys.stderr)
        sys.exit(1)
    room_sha, err = _sha256_of_file(room_yaml_path)
    if err:
        print(f"Error: {err}", file=sys.stderr)
        sys.exit(1)

    # ---- git state ---------------------------------------------------------
    git_state, err = _read_git_state(repo_root)
    if err or git_state is None:
        print(f"Error: git state unavailable: {err}", file=sys.stderr)
        sys.exit(1)

    # ---- audit_verdict -----------------------------------------------------
    audit_verdict = _derive_audit_verdict(handoff_state)

    # ---- build report payload ----------------------------------------------
    timestamp = _utc_now_iso()
    report: Dict[str, Any] = {
        "audit_verdict": audit_verdict,
        "generated_at": timestamp,
        "git": {
            "head_sha": git_state["head_sha"],
            "worktree_dirty": git_state["worktree_dirty"],
        },
        "produced_by": "orchctl session archive-report",
        "session_id": session_id,
        "snapshots": {
            "handoff_yaml_sha256": handoff_sha,
            "room_yaml_sha256": room_sha,
            "session_yaml_sha256": session_sha,
        },
    }

    report_text = yaml.safe_dump(
        report,
        default_flow_style=False,
        allow_unicode=True,
        sort_keys=True,
    )

    # ---- write report atomically via dirfd+O_NOFOLLOW ----------------------
    try:
        target_path = _write_archive_report_atomic(
            repo_root, session_id, timestamp, report_text
        )
    except (OSError, ValueError) as exc:
        print(f"Error: could not write report: {exc}", file=sys.stderr)
        sys.exit(1)

    # ---- emit absolute path ------------------------------------------------
    abs_path = os.path.realpath(target_path)
    print(abs_path)
