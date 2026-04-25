"""Tests for `orchctl room idle-snapshot` V1 (read-only operational snapshot).

All tests are hermetic: tmp dirs, monkeypatched storage paths, no real tmux.
The fixture freezes datetime.now() inside the module so idle-threshold logic
is deterministic regardless of wall-clock drift during the suite.

Invariants under test:
- Report header contains the literal authority-disclaimer line.
- Recommendations only use the locked token set.
- open/claimed handoff + idle session yields `needs_worker_complete`.
- completed + pending_review yields `needs_cto_review`.
- Malformed YAML / unsafe `handoff_id` refs yield `parse_error` or
  `repair_needed` and never feed into `storage.handoff_path`.
- Report path stays under `.orchestrator/runtime/idle-snapshots/<room-id>/`.
- The command does NOT mutate any authoritative YAML (SHA-256 hash
  equality before/after).
- Banned green-light fields never appear (string scan).
"""
import hashlib
import os
import sys
from datetime import datetime, timezone

import pytest
import yaml

_REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), os.pardir))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

from lib import storage, idle_snapshot  # noqa: E402


class Args:
    def __init__(self, **kwargs):
        self.__dict__.update(kwargs)


# ---------------------------------------------------------------------------
# Fixture helpers
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


def _freeze_now(monkeypatch, now_iso="2026-04-25T12:00:00Z"):
    """Pin idle_snapshot.datetime.now() to a fixed UTC moment.

    The module imports `datetime` from `datetime`, so we shadow that name in
    the module namespace with a thin wrapper that delegates everything else
    to the real class.
    """
    fixed = datetime.fromisoformat(now_iso.replace("Z", "+00:00"))

    real_datetime = datetime

    class _FrozenDatetime(real_datetime):
        @classmethod
        def now(cls, tz=None):  # pragma: no cover - trivial
            if tz is None:
                return fixed.replace(tzinfo=None)
            return fixed.astimezone(tz)

    monkeypatch.setattr(idle_snapshot, "datetime", _FrozenDatetime)
    return fixed


def _write_room(base_dir, room_id, name="Test Room",
                current_summary="", phase="execution"):
    room_dir = os.path.join(base_dir, ".orchestrator", "rooms", room_id)
    os.makedirs(room_dir, exist_ok=True)
    state = {
        "room": {"id": room_id, "name": name, "status": "active"},
        "context": {
            "goal": "test",
            "current_summary": current_summary,
            "execution_cwd": base_dir,
        },
        "lifecycle": {"current_phase": phase, "next_action": ""},
    }
    with open(os.path.join(room_dir, "state.yaml"), "w") as f:
        yaml.dump(state, f)
    with open(os.path.join(room_dir, "log.md"), "w") as f:
        f.write("# log\n")
    tmpl = os.path.join(base_dir, ".orchestrator", "rooms", "TEMPLATE")
    os.makedirs(tmpl, exist_ok=True)
    with open(os.path.join(tmpl, "state.yaml"), "w") as f:
        yaml.dump({"room": {"id": "TEMPLATE"}}, f)


def _write_handoff(base_dir, handoff_id, room_id, status="open",
                   review_outcome=None, to_peer="test-worker",
                   resolution_summary=None):
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
        "task": {"description": "test", "validation": [],
                 "acceptance_criteria": []},
        "timestamps": {"created_at": "2026-01-01T00:00:00Z"},
    }
    if review_outcome is not None:
        state["review"] = {"outcome": review_outcome, "reviewed_by": "cto"}
    if resolution_summary is not None:
        state["resolution"] = {
            "completed_by": to_peer,
            "summary": resolution_summary,
        }
    path = os.path.join(
        base_dir, ".orchestrator", "handoffs", f"{handoff_id}.yaml"
    )
    with open(path, "w") as f:
        yaml.dump(state, f)


def _write_session(base_dir, session_id, room_id=None, handoff_id=None,
                   peer_id="test-worker", status="busy", mode="ephemeral",
                   last_active_at=None, heartbeat_at=None,
                   last_launch_status=None, raw_overrides=None):
    state = {
        "session": {
            "id": session_id,
            "peer_id": peer_id,
            "room_id": room_id,
            "handoff_id": handoff_id,
            "tmux_session": "sess-1",
            "tmux_target": "%42",
            "mode": mode,
            "status": status,
            "cwd": base_dir,
        }
    }
    if last_active_at is not None:
        state["session"]["last_active_at"] = last_active_at
    if heartbeat_at is not None:
        state["session"]["heartbeat_at"] = heartbeat_at
    if last_launch_status is not None:
        state["session"]["last_launch_status"] = last_launch_status
    if raw_overrides:
        state["session"].update(raw_overrides)
    path = os.path.join(
        base_dir, ".orchestrator", "runtime", "sessions",
        f"{session_id}.yaml",
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


def _load_latest_report(base_dir, room_id):
    snap_dir = os.path.join(
        base_dir, ".orchestrator", "runtime", "idle-snapshots", room_id
    )
    assert os.path.isdir(snap_dir), f"snapshot dir missing: {snap_dir}"
    files = sorted(f for f in os.listdir(snap_dir) if f.endswith(".md"))
    assert files, "no report files found"
    target = os.path.join(snap_dir, files[-1])
    with open(target) as f:
        return f.read(), target


def _snapshot_orch_yaml_hashes(base_dir):
    """SHA-256 hash every YAML under .orchestrator/ except idle-snapshots
    output. Used to enforce the read-only invariant."""
    orch = os.path.join(base_dir, ".orchestrator")
    snap = {}
    for root, _dirs, files in os.walk(orch):
        rel_root = os.path.relpath(root, orch)
        if rel_root.split(os.sep)[0:2] == ["runtime", "idle-snapshots"]:
            continue
        for fn in files:
            if not fn.endswith(".yaml"):
                continue
            p = os.path.join(root, fn)
            with open(p, "rb") as fh:
                snap[os.path.relpath(p, orch)] = hashlib.sha256(
                    fh.read()
                ).hexdigest()
    return snap


# ---------------------------------------------------------------------------
# Header / shape
# ---------------------------------------------------------------------------

def test_header_contains_authority_disclaimer(monkeypatch, tmp_path, capsys):
    base_dir = str(tmp_path)
    _patch_storage(monkeypatch, base_dir)
    _freeze_now(monkeypatch)
    _write_room(base_dir, "r1")
    _write_peer_registry(base_dir)

    idle_snapshot.cmd_room_idle_snapshot(Args(room_id="r1", idle_minutes=1))

    content, path = _load_latest_report(base_dir, "r1")
    assert idle_snapshot.HEADER_DISCLAIMER in content
    # The exact required literal — guard against accidental rewording.
    assert (
        "> This is a read-only operational snapshot. "
        "Not authoritative state. "
        "Does not authorize any archive/compact/kill action."
    ) in content
    # Path is printed for downstream pipelines.
    out = capsys.readouterr().out.strip().splitlines()[-1]
    assert out == path


def test_report_contains_room_and_handoff_summary(monkeypatch, tmp_path):
    base_dir = str(tmp_path)
    _patch_storage(monkeypatch, base_dir)
    _freeze_now(monkeypatch)
    _write_room(base_dir, "r1", name="Cool Room",
                current_summary="midway", phase="execution")
    _write_peer_registry(base_dir)
    _write_handoff(base_dir, "h-open", "r1", status="open")
    _write_handoff(base_dir, "h-claimed", "r1", status="claimed")
    _write_handoff(base_dir, "h-completed", "r1", status="completed",
                   review_outcome="approved",
                   resolution_summary="all good")
    _write_handoff(base_dir, "h-blocked", "r1", status="blocked")

    idle_snapshot.cmd_room_idle_snapshot(Args(room_id="r1", idle_minutes=1))
    content, _ = _load_latest_report(base_dir, "r1")

    assert "## Room" in content
    assert "Cool Room" in content
    assert "midway" in content
    assert "## Handoff Summary" in content
    assert "### Open (1)" in content
    assert "### Claimed (1)" in content
    assert "### Completed (1)" in content
    assert "### Blocked (1)" in content
    assert "h-open" in content
    assert "review: approved" in content


# ---------------------------------------------------------------------------
# Recommendations and idle classification
# ---------------------------------------------------------------------------

def test_open_handoff_idle_session_recommends_needs_worker_complete(
    monkeypatch, tmp_path
):
    base_dir = str(tmp_path)
    _patch_storage(monkeypatch, base_dir)
    _freeze_now(monkeypatch, "2026-04-25T12:00:00Z")
    _write_room(base_dir, "r1")
    _write_peer_registry(base_dir)
    _write_handoff(base_dir, "h1", "r1", status="open")
    # Both timestamps 2 hours old — well past --idle-minutes=10
    _write_session(
        base_dir, "s1", room_id="r1", handoff_id="h1",
        last_active_at="2026-04-25T10:00:00Z",
        heartbeat_at="2026-04-25T10:00:00Z",
    )

    idle_snapshot.cmd_room_idle_snapshot(Args(room_id="r1", idle_minutes=10))
    content, _ = _load_latest_report(base_dir, "r1")

    assert "needs_worker_complete" in content
    assert "session `s1`" in content
    assert "Idle Candidate Sessions" in content
    assert "### s1" in content


def test_claimed_handoff_idle_session_recommends_needs_worker_complete(
    monkeypatch, tmp_path
):
    base_dir = str(tmp_path)
    _patch_storage(monkeypatch, base_dir)
    _freeze_now(monkeypatch)
    _write_room(base_dir, "r1")
    _write_peer_registry(base_dir)
    _write_handoff(base_dir, "h1", "r1", status="claimed")
    _write_session(
        base_dir, "s1", room_id="r1", handoff_id="h1",
        last_active_at="2026-04-25T10:00:00Z",
        heartbeat_at="2026-04-25T10:00:00Z",
    )

    idle_snapshot.cmd_room_idle_snapshot(Args(room_id="r1", idle_minutes=10))
    content, _ = _load_latest_report(base_dir, "r1")
    assert "needs_worker_complete" in content


def test_completed_pending_review_recommends_needs_cto_review(
    monkeypatch, tmp_path
):
    base_dir = str(tmp_path)
    _patch_storage(monkeypatch, base_dir)
    _freeze_now(monkeypatch)
    _write_room(base_dir, "r1")
    _write_peer_registry(base_dir)
    # Completed but not yet reviewed → review state == 'pending_review'.
    _write_handoff(base_dir, "h1", "r1", status="completed")
    _write_session(
        base_dir, "s1", room_id="r1", handoff_id="h1",
        last_active_at="2026-04-25T10:00:00Z",
        heartbeat_at="2026-04-25T10:00:00Z",
    )

    idle_snapshot.cmd_room_idle_snapshot(Args(room_id="r1", idle_minutes=10))
    content, _ = _load_latest_report(base_dir, "r1")
    assert "needs_cto_review" in content
    assert "needs_worker_complete" not in content


def test_recent_activity_session_is_not_idle(monkeypatch, tmp_path):
    """A session whose newest activity signal is within the threshold MUST
    NOT appear in the Idle Candidate Sessions block."""
    base_dir = str(tmp_path)
    _patch_storage(monkeypatch, base_dir)
    _freeze_now(monkeypatch, "2026-04-25T12:00:00Z")
    _write_room(base_dir, "r1")
    _write_peer_registry(base_dir)
    _write_handoff(base_dir, "h1", "r1", status="open")
    _write_session(
        base_dir, "s1", room_id="r1", handoff_id="h1",
        # 30 seconds ago — well below 10-minute threshold
        last_active_at="2026-04-25T11:59:30Z",
        heartbeat_at="2026-04-25T11:59:30Z",
    )

    idle_snapshot.cmd_room_idle_snapshot(Args(room_id="r1", idle_minutes=10))
    content, _ = _load_latest_report(base_dir, "r1")
    assert "### s1" not in content
    # The recommendations list either omits the session or shows "(none)".
    rec_section = content.split("## Recommendations", 1)[1]
    assert "needs_worker_complete" not in rec_section
    assert "needs_cto_review" not in rec_section


def test_newest_signal_wins_over_older_one(monkeypatch, tmp_path):
    """If only one of last_active_at/heartbeat_at is recent, session is NOT
    idle. We use whichever signal is the most recent — the operator-correct
    interpretation of 'last activity'."""
    base_dir = str(tmp_path)
    _patch_storage(monkeypatch, base_dir)
    _freeze_now(monkeypatch, "2026-04-25T12:00:00Z")
    _write_room(base_dir, "r1")
    _write_peer_registry(base_dir)
    _write_handoff(base_dir, "h1", "r1", status="open")
    _write_session(
        base_dir, "s1", room_id="r1", handoff_id="h1",
        last_active_at="2026-04-25T05:00:00Z",  # 7 hours old
        heartbeat_at="2026-04-25T11:59:30Z",     # 30 seconds old
    )

    idle_snapshot.cmd_room_idle_snapshot(Args(room_id="r1", idle_minutes=10))
    content, _ = _load_latest_report(base_dir, "r1")
    assert "### s1" not in content


# ---------------------------------------------------------------------------
# parse_error / repair_needed boundary
# ---------------------------------------------------------------------------

def test_session_with_no_timestamps_is_repair_needed(monkeypatch, tmp_path):
    """YAML otherwise readable, but neither last_active_at nor heartbeat_at
    is present. Documented decision: classify as `repair_needed` (key
    missing — recoverable). `parse_error` is reserved for malformed YAML
    or unsafe handoff_id refs."""
    base_dir = str(tmp_path)
    _patch_storage(monkeypatch, base_dir)
    _freeze_now(monkeypatch)
    _write_room(base_dir, "r1")
    _write_peer_registry(base_dir)
    _write_handoff(base_dir, "h1", "r1", status="open")
    _write_session(
        base_dir, "s1", room_id="r1", handoff_id="h1",
        # Both timestamps omitted on purpose.
    )

    idle_snapshot.cmd_room_idle_snapshot(Args(room_id="r1", idle_minutes=10))
    content, _ = _load_latest_report(base_dir, "r1")
    assert "repair_needed" in content
    assert "### s1" in content


def test_malformed_session_yaml_is_parse_error(monkeypatch, tmp_path):
    base_dir = str(tmp_path)
    _patch_storage(monkeypatch, base_dir)
    _freeze_now(monkeypatch)
    _write_room(base_dir, "r1")
    _write_peer_registry(base_dir)
    sessions_dir = os.path.join(
        base_dir, ".orchestrator", "runtime", "sessions"
    )
    with open(os.path.join(sessions_dir, "broken.yaml"), "w") as f:
        f.write("session: [this is: not valid\nyaml: {\n")

    idle_snapshot.cmd_room_idle_snapshot(Args(room_id="r1", idle_minutes=10))
    content, _ = _load_latest_report(base_dir, "r1")
    assert "parse_error" in content
    assert "broken" in content


def test_unsafe_handoff_id_classifies_parse_error_no_path_traversal(
    monkeypatch, tmp_path
):
    """A session YAML carrying a non-slug-safe `handoff_id` must NOT cause
    `idle-snapshot` to resolve that value through `storage.handoff_path()`.
    Same invariant locked for gc-audit and checkpoints."""
    base_dir = str(tmp_path)
    _patch_storage(monkeypatch, base_dir)
    _freeze_now(monkeypatch)
    _write_room(base_dir, "r1")
    _write_peer_registry(base_dir)

    # YAML parses, but handoff_id carries a path-traversal payload.
    _write_session(
        base_dir, "s1", room_id="r1", handoff_id="../evil",
        last_active_at="2026-04-25T10:00:00Z",
        heartbeat_at="2026-04-25T10:00:00Z",
    )

    calls = []
    real_handoff_path = storage.handoff_path

    def _tracking(hid):
        calls.append(hid)
        return real_handoff_path(hid)

    monkeypatch.setattr(storage, "handoff_path", _tracking)

    idle_snapshot.cmd_room_idle_snapshot(Args(room_id="r1", idle_minutes=10))

    assert "../evil" not in calls, (
        f"unsafe handoff_id leaked into storage.handoff_path; calls={calls}"
    )
    content, _ = _load_latest_report(base_dir, "r1")
    assert "parse_error" in content


# ---------------------------------------------------------------------------
# Recommendation token hygiene
# ---------------------------------------------------------------------------

def test_recommendation_token_set_is_locked():
    """Module-level invariant: locked token list cannot drift without a
    coordinated handoff contract bump."""
    expected = {
        "needs_worker_complete",
        "needs_cto_review",
        "at_risk",
        "unbound",
        "parse_error",
        "repair_needed",
    }
    assert idle_snapshot.RECOMMENDATION_TOKENS == expected


def test_report_only_uses_locked_recommendation_tokens(monkeypatch, tmp_path):
    """No string outside the locked set may appear as a backticked
    recommendation in the rendered report."""
    base_dir = str(tmp_path)
    _patch_storage(monkeypatch, base_dir)
    _freeze_now(monkeypatch)
    _write_room(base_dir, "r1")
    _write_peer_registry(base_dir)
    # Mix of states to exercise as many recommendation paths as possible.
    _write_handoff(base_dir, "h-open", "r1", status="open")
    _write_handoff(base_dir, "h-completed", "r1", status="completed")
    _write_handoff(base_dir, "h-blocked", "r1", status="blocked")
    _write_handoff(base_dir, "h-changes", "r1", status="completed",
                   review_outcome="changes_requested")

    _write_session(base_dir, "s-open", room_id="r1", handoff_id="h-open",
                   last_active_at="2026-04-25T10:00:00Z",
                   heartbeat_at="2026-04-25T10:00:00Z")
    _write_session(base_dir, "s-completed", room_id="r1",
                   handoff_id="h-completed",
                   last_active_at="2026-04-25T10:00:00Z",
                   heartbeat_at="2026-04-25T10:00:00Z")
    _write_session(base_dir, "s-blocked", room_id="r1",
                   handoff_id="h-blocked",
                   last_active_at="2026-04-25T10:00:00Z",
                   heartbeat_at="2026-04-25T10:00:00Z")
    _write_session(base_dir, "s-changes", room_id="r1",
                   handoff_id="h-changes",
                   last_active_at="2026-04-25T10:00:00Z",
                   heartbeat_at="2026-04-25T10:00:00Z")
    _write_session(base_dir, "s-unbound", room_id="r1", handoff_id=None,
                   last_active_at="2026-04-25T10:00:00Z",
                   heartbeat_at="2026-04-25T10:00:00Z")
    _write_session(base_dir, "s-orphan", room_id="r1",
                   handoff_id="h-not-in-room",
                   last_active_at="2026-04-25T10:00:00Z",
                   heartbeat_at="2026-04-25T10:00:00Z")

    idle_snapshot.cmd_room_idle_snapshot(Args(room_id="r1", idle_minutes=10))
    content, _ = _load_latest_report(base_dir, "r1")

    # Extract the recommendations section and scan for backticked tokens.
    rec_section = content.split("## Recommendations", 1)[1]
    import re
    seen = set(re.findall(r"`([a-z_]+)`", rec_section))
    # Filter to known recommendation-shaped tokens (others are session ids
    # or handoff status/review labels).
    candidates = {t for t in seen if "_" in t or t in idle_snapshot.RECOMMENDATION_TOKENS}
    # Drop status/review tokens that the recommendation line carries as
    # context — they're not *recommendation* tokens themselves.
    context_tokens = {
        "open", "claimed", "completed", "blocked",
        "pending_review", "approved", "changes_requested", "n/a",
    }
    candidates -= context_tokens
    bad = candidates - idle_snapshot.RECOMMENDATION_TOKENS
    assert not bad, f"non-locked recommendation tokens emitted: {bad}"


# ---------------------------------------------------------------------------
# Mutation safety + banned fields
# ---------------------------------------------------------------------------

def test_idle_snapshot_does_not_mutate_authoritative_yaml(
    monkeypatch, tmp_path
):
    base_dir = str(tmp_path)
    _patch_storage(monkeypatch, base_dir)
    _freeze_now(monkeypatch)
    _write_room(base_dir, "r1")
    _write_peer_registry(base_dir)
    _write_handoff(base_dir, "h1", "r1", status="completed",
                   review_outcome="approved",
                   resolution_summary="done")
    _write_session(
        base_dir, "s1", room_id="r1", handoff_id="h1",
        last_active_at="2026-04-25T10:00:00Z",
        heartbeat_at="2026-04-25T10:00:00Z",
    )

    before = _snapshot_orch_yaml_hashes(base_dir)
    idle_snapshot.cmd_room_idle_snapshot(Args(room_id="r1", idle_minutes=10))
    after = _snapshot_orch_yaml_hashes(base_dir)

    assert before == after, (
        "idle-snapshot must not mutate authoritative YAML "
        "(SHA-256 hashes diverged)"
    )


def test_report_forbids_green_light_fields(monkeypatch, tmp_path):
    """Hard invariant: the report MUST NOT include archive/green-light
    fields — those belong to gc-audit V2, not idle-snapshot."""
    base_dir = str(tmp_path)
    _patch_storage(monkeypatch, base_dir)
    _freeze_now(monkeypatch)
    _write_room(base_dir, "r1")
    _write_peer_registry(base_dir)
    _write_handoff(base_dir, "h1", "r1", status="completed",
                   review_outcome="approved")
    _write_session(
        base_dir, "s1", room_id="r1", handoff_id="h1",
        last_active_at="2026-04-25T10:00:00Z",
        heartbeat_at="2026-04-25T10:00:00Z",
    )

    idle_snapshot.cmd_room_idle_snapshot(Args(room_id="r1", idle_minutes=10))
    content, _ = _load_latest_report(base_dir, "r1")
    for banned in (
        "archive_ready", "safe_to_archive", "can_archive", "green_light",
        "ready_for_archive", "auto_archive_eligible",
    ):
        assert banned not in content, (
            f"banned green-light field '{banned}' leaked into report"
        )


def test_report_path_contained_under_idle_snapshots_dir(
    monkeypatch, tmp_path
):
    base_dir = str(tmp_path)
    _patch_storage(monkeypatch, base_dir)
    _freeze_now(monkeypatch)
    _write_room(base_dir, "r1")
    _write_peer_registry(base_dir)

    idle_snapshot.cmd_room_idle_snapshot(Args(room_id="r1", idle_minutes=10))

    snap_root = os.path.realpath(os.path.join(
        base_dir, ".orchestrator", "runtime", "idle-snapshots"
    ))
    room_root = os.path.join(snap_root, "r1")
    files = os.listdir(room_root)
    assert files, "no snapshot file written"
    for fn in files:
        full = os.path.realpath(os.path.join(room_root, fn))
        assert full.startswith(snap_root + os.sep), (
            f"snapshot escaped containment: {full}"
        )


# ---------------------------------------------------------------------------
# CLI parser smoke test
# ---------------------------------------------------------------------------

def test_parser_has_room_idle_snapshot_subcommand():
    """`orchctl room idle-snapshot <room-id> --idle-minutes N` parses."""
    import importlib.util
    import importlib.machinery
    path = os.path.join(_REPO_ROOT, "orchctl")
    loader = importlib.machinery.SourceFileLoader("orchctl_cli", path)
    spec = importlib.util.spec_from_loader("orchctl_cli", loader)
    orchctl_mod = importlib.util.module_from_spec(spec)
    loader.exec_module(orchctl_mod)
    parser = orchctl_mod.build_parser()
    ns = parser.parse_args(
        ["room", "idle-snapshot", "my-room", "--idle-minutes", "30"]
    )
    assert ns.room_id == "my-room"
    assert ns.idle_minutes == "30"
    assert callable(ns.func)


def test_negative_idle_minutes_rejected(monkeypatch, tmp_path, capsys):
    base_dir = str(tmp_path)
    _patch_storage(monkeypatch, base_dir)
    _freeze_now(monkeypatch)
    _write_room(base_dir, "r1")
    _write_peer_registry(base_dir)

    with pytest.raises(SystemExit):
        idle_snapshot.cmd_room_idle_snapshot(Args(room_id="r1", idle_minutes="-5"))
    err = capsys.readouterr().err
    assert "idle-minutes" in err.lower()
