"""V2 session archive orchestration — wires validation + bundle writing.

Entry point: ``cmd_session_archive(args) -> int``.

Call chain (locked order):
  1. validate_archive_request  (Child A — pure read/hash)
  2. write_archive_bundle       (Child B — bundle write)
  2.5. stamp-time CAS rehash   (re-hash session/handoff/room; fail-closed on drift)
  3. stamp_session_archive_marker (Child B — session YAML marker)

No __main__ block. No module-level side effects. No tmux. No subprocess
against tmux or network. No re-implementation of any validation step.
"""
from __future__ import annotations

import hashlib
import os
import sys
from typing import Any, Optional, Tuple

from lib import session_archive_validate, session_archive_bundle


def _repo_root() -> str:
    """Compute repo root from this file's location (lib/ -> parent)."""
    return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _sha256_file(path: str) -> Tuple[Optional[str], Optional[str]]:
    """Return (hexdigest, None) or (None, error_message)."""
    try:
        h = hashlib.sha256()
        with open(path, "rb") as f:
            for chunk in iter(lambda: f.read(65536), b""):
                h.update(chunk)
        return h.hexdigest(), None
    except (OSError, ValueError) as exc:
        return None, f"could not hash {path}: {exc}"


def cmd_session_archive(args: Any) -> int:
    """Orchestrate the archive of a completed session.

    ``args`` must have:
      - ``args.session_id`` (str)
      - ``args.from_report`` (str) — absolute path to a promoted report YAML

    Returns 0 on full success, 1 on any failure.
    """
    repo_root = _repo_root()

    # ---- (1) Validate -------------------------------------------------------
    validated_context, result_enum, error_message = (
        session_archive_validate.validate_archive_request(
            args.session_id,
            args.from_report,
            repo_root,
        )
    )

    if result_enum != "archived":
        print(f"{result_enum}: {error_message}", file=sys.stderr)
        return 1

    # ---- (2) Write bundle ---------------------------------------------------
    try:
        yaml_path, _md_path = session_archive_bundle.write_archive_bundle(
            validated_context, repo_root
        )
    except Exception as exc:
        print(
            f"archived path: bundle write failed: {type(exc).__name__}: {exc}",
            file=sys.stderr,
        )
        return 1

    # ---- (2.5) Stamp-time CAS rehash ----------------------------------------
    # Re-hash the same three YAMLs that the validator hashed. If any sha256
    # differs from the validated_context snapshots, a concurrent process has
    # modified the file between validation and bundle write. Fail-closed: do
    # NOT stamp the marker. The already-written bundle stays on disk (derived
    # artifact; next archive run will write a fresh one).
    session_id = validated_context["session_id"]
    handoff_id = validated_context["handoff_id"]
    room_id = validated_context["room_id"]

    session_yaml_path = os.path.join(
        repo_root, ".orchestrator", "runtime", "sessions", f"{session_id}.yaml"
    )
    handoff_yaml_path = os.path.join(
        repo_root, ".orchestrator", "handoffs", f"{handoff_id}.yaml"
    )
    room_yaml_path = os.path.join(
        repo_root, ".orchestrator", "rooms", room_id, "state.yaml"
    )

    cas_checks = [
        ("session_yaml", session_yaml_path, validated_context["snapshots"]["session_yaml_sha256"]),
        ("handoff_yaml", handoff_yaml_path, validated_context["snapshots"]["handoff_yaml_sha256"]),
        ("room_yaml", room_yaml_path, validated_context["snapshots"]["room_yaml_sha256"]),
    ]
    for label, path, expected_sha in cas_checks:
        current_sha, err = _sha256_file(path)
        if err is not None or current_sha != expected_sha:
            print(
                f"stale_report: {label} sha256 changed since validation",
                file=sys.stderr,
            )
            return 1

    # ---- (3) Stamp session YAML marker -------------------------------------
    try:
        session_archive_bundle.stamp_session_archive_marker(
            args.session_id, yaml_path, args.from_report
        )
    except Exception as exc:
        print(
            f"archived path: marker write failed AFTER bundle write: "
            f"{type(exc).__name__}: {exc}",
            file=sys.stderr,
        )
        print(f"bundle written to: {yaml_path}", file=sys.stderr)
        return 1

    # ---- Success ------------------------------------------------------------
    print(f"archived: {yaml_path}")
    return 0
