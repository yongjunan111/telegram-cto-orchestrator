"""Tests for recent bugfixes: handoff lifecycle, evidence rejection,
config error handling, dispatch config options, and dispatch-plan consistency.

All tests are hermetic: tmp dirs, monkeypatch, no real state.
"""
import os
import sys
import shutil
import tempfile
import pytest
import yaml
from unittest import mock

_REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), os.pardir))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

from lib import storage, dispatch  # noqa: E402
from lib.handoffs import cmd_handoff_claim, cmd_handoff_complete  # noqa: E402
from lib.config import load_config, ConfigError, _DEFAULTS  # noqa: E402


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

class Args:
    def __init__(self, **kwargs):
        self.__dict__.update(kwargs)


def _setup_room_and_handoff(base_dir, room_id="test-room", handoff_id="test-handoff", peer_id="test-worker"):
    """Create minimal room + handoff + peer in base_dir for lifecycle testing."""
    rooms_dir = os.path.join(base_dir, ".orchestrator", "rooms")
    os.makedirs(os.path.join(rooms_dir, room_id), exist_ok=True)

    room_state = {
        "room": {"id": room_id, "name": "Test", "status": "active"},
        "context": {"goal": "test", "execution_cwd": base_dir},
        "lifecycle": {"current_phase": "execution"},
    }
    with open(os.path.join(rooms_dir, room_id, "state.yaml"), "w") as f:
        yaml.dump(room_state, f)
    with open(os.path.join(rooms_dir, room_id, "log.md"), "w") as f:
        f.write("# Log\n")

    handoffs_dir = os.path.join(base_dir, ".orchestrator", "handoffs")
    os.makedirs(handoffs_dir, exist_ok=True)
    handoff_state = {
        "handoff": {
            "id": handoff_id,
            "room_id": room_id,
            "from": "orchestrator",
            "to": peer_id,
            "status": "open",
            "priority": "medium",
            "kind": "implementation",
        },
        "task": {"description": "test task", "validation": [], "acceptance_criteria": []},
        "timestamps": {"created_at": "2026-01-01T00:00:00Z"},
    }
    with open(os.path.join(handoffs_dir, f"{handoff_id}.yaml"), "w") as f:
        yaml.dump(handoff_state, f)

    reg = {
        "peers": [
            {
                "id": peer_id,
                "name": "Test Worker",
                "type": "worker",
                "cwd": base_dir,
                "capabilities": [],
                "status": "available",
            }
        ]
    }
    with open(os.path.join(base_dir, ".orchestrator", "peer_registry.yaml"), "w") as f:
        yaml.dump(reg, f)

    # TEMPLATE dir (needed by validators)
    template_dir = os.path.join(rooms_dir, "TEMPLATE")
    os.makedirs(template_dir, exist_ok=True)
    with open(os.path.join(template_dir, "state.yaml"), "w") as f:
        yaml.dump({"room": {"id": "TEMPLATE"}}, f)

    return rooms_dir, handoffs_dir


def _patch_storage(monkeypatch, base_dir):
    """Redirect all storage paths to base_dir/.orchestrator/."""
    orch_dir = os.path.join(base_dir, ".orchestrator")
    rooms_dir = os.path.join(orch_dir, "rooms")
    handoffs_dir = os.path.join(orch_dir, "handoffs")
    peer_registry = os.path.join(orch_dir, "peer_registry.yaml")
    runtime_dir = os.path.join(orch_dir, "runtime")
    sessions_dir = os.path.join(runtime_dir, "sessions")
    locks_dir = os.path.join(runtime_dir, "locks")

    monkeypatch.setattr(storage, "ORCHESTRATOR_DIR", orch_dir)
    monkeypatch.setattr(storage, "ROOMS_DIR", rooms_dir)
    monkeypatch.setattr(storage, "HANDOFFS_DIR", handoffs_dir)
    monkeypatch.setattr(storage, "PEER_REGISTRY_PATH", peer_registry)
    monkeypatch.setattr(storage, "RUNTIME_DIR", runtime_dir)
    monkeypatch.setattr(storage, "SESSIONS_DIR", sessions_dir)
    monkeypatch.setattr(dispatch, "LOCKS_DIR", locks_dir)

    os.makedirs(sessions_dir, exist_ok=True)
    os.makedirs(locks_dir, exist_ok=True)


# ---------------------------------------------------------------------------
# Group 1: Handoff lifecycle (claim -> complete)
# ---------------------------------------------------------------------------

def test_claim_then_complete_succeeds(monkeypatch, tmp_path):
    """claim then complete should both transition successfully."""
    base_dir = str(tmp_path)
    _setup_room_and_handoff(base_dir)
    _patch_storage(monkeypatch, base_dir)

    # Claim
    claim_args = Args(handoff_id="test-handoff", by="test-worker")
    cmd_handoff_claim(claim_args)

    # Verify claimed
    path = os.path.join(base_dir, ".orchestrator", "handoffs", "test-handoff.yaml")
    state = storage.read_state(path)
    assert state["handoff"]["status"] == "claimed"

    # Complete
    complete_args = Args(
        handoff_id="test-handoff",
        by="test-worker",
        summary="done",
        files=[],
        verifications=[],
        risks=[],
        validation_covers=[],
        task_criterion_covers=[],
        room_criterion_covers=[],
    )
    cmd_handoff_complete(complete_args)

    # Verify completed
    state = storage.read_state(path)
    assert state["handoff"]["status"] == "completed"


def test_complete_without_claim_fails(monkeypatch, tmp_path):
    """Completing an open handoff (never claimed) should sys.exit."""
    base_dir = str(tmp_path)
    _setup_room_and_handoff(base_dir)
    _patch_storage(monkeypatch, base_dir)

    complete_args = Args(
        handoff_id="test-handoff",
        by="test-worker",
        summary="done",
        files=[],
        verifications=[],
        risks=[],
        validation_covers=[],
        task_criterion_covers=[],
        room_criterion_covers=[],
    )
    with pytest.raises(SystemExit):
        cmd_handoff_complete(complete_args)


# ---------------------------------------------------------------------------
# Group 2: Empty evidence rejection
# ---------------------------------------------------------------------------

def test_empty_validation_evidence_rejected(monkeypatch, tmp_path):
    """--validation-cover with empty evidence (colon then nothing) should sys.exit."""
    base_dir = str(tmp_path)
    _setup_room_and_handoff(base_dir)
    _patch_storage(monkeypatch, base_dir)

    # Inject validation step into handoff
    handoff_path = os.path.join(base_dir, ".orchestrator", "handoffs", "test-handoff.yaml")
    state = storage.read_state(handoff_path)
    state["handoff"]["status"] = "claimed"
    state["task"]["validation"] = ["Run pytest"]
    storage.write_state(handoff_path, state)

    complete_args = Args(
        handoff_id="test-handoff",
        by="test-worker",
        summary="done",
        files=[],
        verifications=[],
        risks=[],
        validation_covers=["1:"],  # empty evidence after colon
        task_criterion_covers=[],
        room_criterion_covers=[],
    )
    with pytest.raises(SystemExit):
        cmd_handoff_complete(complete_args)


def test_nonempty_validation_evidence_accepted(monkeypatch, tmp_path):
    """--validation-cover with non-empty evidence should succeed."""
    base_dir = str(tmp_path)
    _setup_room_and_handoff(base_dir)
    _patch_storage(monkeypatch, base_dir)

    handoff_path = os.path.join(base_dir, ".orchestrator", "handoffs", "test-handoff.yaml")
    state = storage.read_state(handoff_path)
    state["handoff"]["status"] = "claimed"
    state["task"]["validation"] = ["Run pytest"]
    storage.write_state(handoff_path, state)

    complete_args = Args(
        handoff_id="test-handoff",
        by="test-worker",
        summary="done",
        files=[],
        verifications=[],
        risks=[],
        validation_covers=["1:test passed"],  # non-empty evidence
        task_criterion_covers=[],
        room_criterion_covers=[],
    )
    # Should not raise
    cmd_handoff_complete(complete_args)

    final = storage.read_state(handoff_path)
    assert final["handoff"]["status"] == "completed"
    coverage = final["resolution"]["validation_coverage"]
    assert len(coverage) == 1
    assert coverage[0]["evidence"] == "test passed"


# ---------------------------------------------------------------------------
# Group 3: Config error handling
# ---------------------------------------------------------------------------

def test_missing_config_returns_defaults(monkeypatch, tmp_path):
    """No config file -> load_config() returns defaults."""
    missing = str(tmp_path / "nonexistent.yaml")
    monkeypatch.setattr("lib.config.CONFIG_PATH", missing)

    config = load_config()
    assert config["worker"]["permissions_mode"] == _DEFAULTS["worker"]["permissions_mode"]
    assert config["dispatch"]["auto_launch_worker"] == _DEFAULTS["dispatch"]["auto_launch_worker"]
    assert config["dispatch"]["auto_register_peer"] == _DEFAULTS["dispatch"]["auto_register_peer"]


def test_malformed_config_raises_error(monkeypatch, tmp_path):
    """Invalid YAML in config file -> ConfigError raised."""
    config_file = str(tmp_path / "config.yaml")
    with open(config_file, "w") as f:
        f.write("key: [unclosed bracket\n")

    monkeypatch.setattr("lib.config.CONFIG_PATH", config_file)

    with pytest.raises(ConfigError):
        load_config()


def test_valid_config_merges(monkeypatch, tmp_path):
    """Valid config overrides specific keys while preserving other defaults."""
    config_file = str(tmp_path / "config.yaml")
    with open(config_file, "w") as f:
        yaml.dump({"dispatch": {"auto_launch_worker": False}}, f)

    monkeypatch.setattr("lib.config.CONFIG_PATH", config_file)

    config = load_config()
    # Overridden value
    assert config["dispatch"]["auto_launch_worker"] is False
    # Other dispatch default preserved
    assert config["dispatch"]["auto_register_peer"] == _DEFAULTS["dispatch"]["auto_register_peer"]
    # Unrelated section preserved
    assert config["worker"]["permissions_mode"] == _DEFAULTS["worker"]["permissions_mode"]


# ---------------------------------------------------------------------------
# Group 4: Dispatch config options
# ---------------------------------------------------------------------------

def test_ensure_peer_skips_when_auto_register_disabled(monkeypatch, tmp_path):
    """auto_register_peer=false -> _ensure_peer returns error for unknown peer."""
    base_dir = str(tmp_path)
    orch_dir = os.path.join(base_dir, ".orchestrator")
    os.makedirs(orch_dir, exist_ok=True)
    peer_registry = os.path.join(orch_dir, "peer_registry.yaml")
    storage.write_state(peer_registry, {"peers": []})

    monkeypatch.setattr(storage, "PEER_REGISTRY_PATH", peer_registry)

    config_no_autoreg = {
        "worker": {"permissions_mode": "normal", "claude_bin": "claude"},
        "dispatch": {"auto_launch_worker": True, "auto_register_peer": False},
    }
    monkeypatch.setattr("lib.dispatch.load_config", lambda: config_no_autoreg)

    room_state = {
        "context": {"execution_cwd": base_dir},
        "lifecycle": {},
    }
    peer, err = dispatch._ensure_peer("unknown-peer", room_state)

    assert peer is None
    assert err is not None
    assert "auto_register_peer is disabled" in err


def test_ensure_peer_auto_registers_when_enabled(monkeypatch, tmp_path):
    """auto_register_peer=true (default) -> _ensure_peer auto-registers missing peer."""
    base_dir = str(tmp_path)
    orch_dir = os.path.join(base_dir, ".orchestrator")
    os.makedirs(orch_dir, exist_ok=True)
    peer_registry = os.path.join(orch_dir, "peer_registry.yaml")
    storage.write_state(peer_registry, {"peers": []})

    monkeypatch.setattr(storage, "PEER_REGISTRY_PATH", peer_registry)

    config_autoreg = {
        "worker": {"permissions_mode": "normal", "claude_bin": "claude"},
        "dispatch": {"auto_launch_worker": True, "auto_register_peer": True},
    }
    monkeypatch.setattr("lib.dispatch.load_config", lambda: config_autoreg)

    room_state = {
        "context": {"execution_cwd": base_dir},
        "lifecycle": {},
    }
    peer, err = dispatch._ensure_peer("new-peer", room_state)

    assert err is None
    assert peer is not None
    assert peer["id"] == "new-peer"
    assert peer["cwd"] == base_dir

    # Verify persisted
    reg = storage.read_state(peer_registry)
    ids = [p["id"] for p in reg.get("peers", [])]
    assert "new-peer" in ids


def test_launch_worker_skips_when_disabled(monkeypatch, tmp_path):
    """auto_launch_worker=false -> _launch_worker returns without sending tmux commands."""
    send_keys_calls = []

    config_no_launch = {
        "worker": {"permissions_mode": "normal", "claude_bin": "claude"},
        "dispatch": {"auto_launch_worker": False, "auto_register_peer": True},
    }
    monkeypatch.setattr("lib.dispatch.load_config", lambda: config_no_launch)
    monkeypatch.setattr(
        dispatch, "_tmux_send_keys",
        lambda target, keys: send_keys_calls.append((target, keys)),
    )
    monkeypatch.setattr(dispatch, "_tmux_target_exists", lambda t: True)
    monkeypatch.setattr(dispatch, "_is_safe_tmux_target", lambda t: True)

    # Create a real bootstrap file so the path check passes
    bootstrap_file = str(tmp_path / "bootstrap.md")
    with open(bootstrap_file, "w") as f:
        f.write("# Bootstrap\n")

    dispatch._launch_worker("%42", "sess-test", bootstrap_file)

    # No send-keys should have been called
    assert send_keys_calls == []


# ---------------------------------------------------------------------------
# Group 5: dispatch-plan consistency
# ---------------------------------------------------------------------------

def test_dispatch_plan_reflects_auto_register(monkeypatch, tmp_path):
    """dispatch-plan output reflects auto_register_peer config correctly."""
    base_dir = str(tmp_path)
    _setup_room_and_handoff(
        base_dir,
        room_id="plan-room",
        handoff_id="plan-handoff",
        peer_id="plan-worker",
    )
    _patch_storage(monkeypatch, base_dir)

    # Remove the peer from registry so it's "unknown"
    peer_registry = os.path.join(base_dir, ".orchestrator", "peer_registry.yaml")
    storage.write_state(peer_registry, {"peers": []})

    # Patch tmux helpers used by dispatch-plan (no real tmux)
    monkeypatch.setattr(dispatch, "_tmux_session_exists", lambda name: False)
    monkeypatch.setattr(dispatch, "_tmux_target_exists", lambda target: False)

    # ---- Case A: auto_register_peer=true => plan mentions auto-registered ----
    config_autoreg = {
        "worker": {"permissions_mode": "normal", "claude_bin": "claude"},
        "dispatch": {"auto_launch_worker": True, "auto_register_peer": True},
    }
    monkeypatch.setattr("lib.dispatch.load_config", lambda: config_autoreg)

    args_plan = Args(handoff_id="plan-handoff")

    import io
    from contextlib import redirect_stdout

    buf = io.StringIO()
    with redirect_stdout(buf):
        from lib.dispatch import cmd_handoff_dispatch_plan
        cmd_handoff_dispatch_plan(args_plan)
    plan_output = buf.getvalue()

    assert "auto-registered" in plan_output.lower() or "auto_registered" in plan_output.lower() or "auto" in plan_output.lower()
    assert "cannot_allocate" not in plan_output

    # ---- Case B: auto_register_peer=false => plan says cannot_allocate ----
    config_no_autoreg = {
        "worker": {"permissions_mode": "normal", "claude_bin": "claude"},
        "dispatch": {"auto_launch_worker": True, "auto_register_peer": False},
    }
    monkeypatch.setattr("lib.dispatch.load_config", lambda: config_no_autoreg)

    buf2 = io.StringIO()
    with redirect_stdout(buf2):
        cmd_handoff_dispatch_plan(args_plan)
    plan_output2 = buf2.getvalue()

    assert "cannot_allocate" in plan_output2
