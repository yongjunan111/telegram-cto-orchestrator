"""Tests for bootstrap delegation rendering (V1).

Locked by handoff ``orch-session-cleanup-policy-v1``:

- ``execution.mode == 'delegate_required'`` MUST render a hard
  "DELEGATION REQUIRED — DO NOT IMPLEMENT DIRECTLY" block prepended to the
  team lead protocol, with the literal sentence "Direct file edits will NOT
  pass the completion gate."
- ``execution.mode in (direct, delegate_optional)`` and legacy handoffs
  (no ``execution`` block) MUST NOT render that block.
- All three render variants (delegate_required, delegate_optional, legacy)
  MUST contain the ``POST-COMPLETE CLEANUP`` heading and four bullets
  enumerated in the contract.
- Legacy and delegate_optional render output is IDENTICAL — guard against
  mode-detection silently changing legacy bootstrap because the
  ``execution`` field is treated as required.

The ``cmd_session_bootstrap`` integration test confirms the rendering path
that operators actually exercise (handoff YAML → bootstrap markdown).
"""
import os
import sys

import pytest
import yaml

_REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), os.pardir))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

from lib import bootstrap, storage  # noqa: E402


HARD_HEADING = "DELEGATION REQUIRED — DO NOT IMPLEMENT DIRECTLY"
HARD_GATE_SENTENCE = "Direct file edits will NOT pass the completion gate."
CLEANUP_HEADING = "POST-COMPLETE CLEANUP"


# ---------------------------------------------------------------------------
# Unit tests against _render_team_lead_protocol
# ---------------------------------------------------------------------------

def test_delegate_required_renders_hard_heading():
    out = bootstrap._render_team_lead_protocol(
        "test-peer", execution_mode="delegate_required"
    )
    assert HARD_HEADING in out
    assert HARD_GATE_SENTENCE in out


def test_delegate_optional_does_not_render_hard_heading():
    out = bootstrap._render_team_lead_protocol(
        "test-peer", execution_mode="delegate_optional"
    )
    assert HARD_HEADING not in out
    assert HARD_GATE_SENTENCE not in out


def test_direct_does_not_render_hard_heading():
    out = bootstrap._render_team_lead_protocol(
        "test-peer", execution_mode="direct"
    )
    assert HARD_HEADING not in out
    assert HARD_GATE_SENTENCE not in out


def test_legacy_no_execution_mode_does_not_render_hard_heading():
    """Legacy handoffs (no execution block at all) reach this function with
    execution_mode='' from _extract_execution_mode. Behavior must match
    delegate_optional / direct — no hard block."""
    out = bootstrap._render_team_lead_protocol(
        "test-peer", execution_mode=""
    )
    assert HARD_HEADING not in out
    assert HARD_GATE_SENTENCE not in out


def test_post_complete_cleanup_present_in_delegate_required():
    out = bootstrap._render_team_lead_protocol(
        "test-peer", execution_mode="delegate_required"
    )
    assert CLEANUP_HEADING in out
    _assert_cleanup_bullets_present(out)


def test_post_complete_cleanup_present_in_delegate_optional():
    out = bootstrap._render_team_lead_protocol(
        "test-peer", execution_mode="delegate_optional"
    )
    assert CLEANUP_HEADING in out
    _assert_cleanup_bullets_present(out)


def test_post_complete_cleanup_present_in_legacy():
    out = bootstrap._render_team_lead_protocol(
        "test-peer", execution_mode=""
    )
    assert CLEANUP_HEADING in out
    _assert_cleanup_bullets_present(out)


def test_legacy_matches_delegate_optional_byte_for_byte():
    """Regression: the only difference between legacy (no execution block)
    and delegate_optional is the YAML field. Rendering MUST treat them the
    same — otherwise mode-detection has silently moved legacy handoffs to a
    new render path. Same expectation applies to direct vs legacy."""
    legacy = bootstrap._render_team_lead_protocol(
        "test-peer", execution_mode=""
    )
    delegate_optional = bootstrap._render_team_lead_protocol(
        "test-peer", execution_mode="delegate_optional"
    )
    direct = bootstrap._render_team_lead_protocol(
        "test-peer", execution_mode="direct"
    )
    assert legacy == delegate_optional
    assert legacy == direct


def _assert_cleanup_bullets_present(rendered: str) -> None:
    """The contract enumerates four post-complete-cleanup bullets."""
    # Bullet (a): orchctl session checkpoint command.
    assert "orchctl session checkpoint" in rendered
    assert "manual" in rendered  # event flag
    # Bullet (b): pane marker line so an operator can confirm completion.
    assert "WORK_DONE" in rendered
    # Bullet (c): forbid self-kill of own tmux session.
    assert "DO NOT kill your own tmux session" in rendered
    # Bullet (d): if review/rework pending, leave pane idle rather than ending.
    assert "leave the pane idle" in rendered


# ---------------------------------------------------------------------------
# Extraction of execution.mode from handoff state
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("mode", ["direct", "delegate_optional", "delegate_required"])
def test_extract_execution_mode_returns_literal(mode):
    handoff_state = {
        "handoff": {"id": "h1", "status": "open"},
        "execution": {"mode": mode, "child_handoffs": []},
    }
    assert bootstrap._extract_execution_mode(handoff_state) == mode


def test_extract_execution_mode_legacy_returns_empty():
    handoff_state = {"handoff": {"id": "h1", "status": "open"}}
    assert bootstrap._extract_execution_mode(handoff_state) == ""


def test_extract_execution_mode_handles_none():
    assert bootstrap._extract_execution_mode(None) == ""


def test_extract_execution_mode_handles_non_dict_execution():
    handoff_state = {"handoff": {}, "execution": "not-a-dict"}
    assert bootstrap._extract_execution_mode(handoff_state) == ""


def test_extract_execution_mode_handles_non_string_mode():
    handoff_state = {"handoff": {}, "execution": {"mode": 42}}
    assert bootstrap._extract_execution_mode(handoff_state) == ""


# ---------------------------------------------------------------------------
# End-to-end via cmd_session_bootstrap with hermetic storage
# ---------------------------------------------------------------------------

class _Args:
    def __init__(self, **kwargs):
        self.__dict__.update(kwargs)


def _patch_storage(monkeypatch, base_dir):
    orch_dir = os.path.join(base_dir, ".orchestrator")
    rooms_dir = os.path.join(orch_dir, "rooms")
    handoffs_dir = os.path.join(orch_dir, "handoffs")
    runtime_dir = os.path.join(orch_dir, "runtime")
    sessions_dir = os.path.join(runtime_dir, "sessions")
    bootstrap_dir = os.path.join(runtime_dir, "bootstrap")
    checkpoints_dir = os.path.join(runtime_dir, "checkpoints")
    dispatches_dir = os.path.join(runtime_dir, "dispatches")
    wiki_path = os.path.join(orch_dir, "wiki", "current-state.md")

    monkeypatch.setattr(storage, "ORCHESTRATOR_DIR", orch_dir)
    monkeypatch.setattr(storage, "ROOMS_DIR", rooms_dir)
    monkeypatch.setattr(storage, "HANDOFFS_DIR", handoffs_dir)
    monkeypatch.setattr(storage, "RUNTIME_DIR", runtime_dir)
    monkeypatch.setattr(storage, "SESSIONS_DIR", sessions_dir)
    monkeypatch.setattr(bootstrap, "BOOTSTRAP_DIR", bootstrap_dir)
    monkeypatch.setattr(bootstrap, "CHECKPOINTS_DIR", checkpoints_dir)
    monkeypatch.setattr(bootstrap, "DISPATCHES_DIR", dispatches_dir)
    monkeypatch.setattr(bootstrap, "WIKI_CURRENT_STATE", wiki_path)

    for d in (sessions_dir, handoffs_dir, rooms_dir, bootstrap_dir,
              checkpoints_dir, dispatches_dir):
        os.makedirs(d, exist_ok=True)


def _write_room(base_dir, room_id):
    room_dir = os.path.join(base_dir, ".orchestrator", "rooms", room_id)
    os.makedirs(room_dir, exist_ok=True)
    state = {
        "room": {"id": room_id, "name": room_id, "status": "active"},
        "context": {"goal": "test"},
        "lifecycle": {"current_phase": "execution"},
    }
    with open(os.path.join(room_dir, "state.yaml"), "w") as f:
        yaml.dump(state, f)


def _write_handoff(base_dir, handoff_id, room_id, execution_block=None):
    state = {
        "handoff": {
            "id": handoff_id,
            "room_id": room_id,
            "from": "orchestrator",
            "to": "test-peer",
            "status": "open",
            "priority": "medium",
            "kind": "implementation",
        },
        "task": {"description": "x"},
        "timestamps": {"created_at": "2026-01-01T00:00:00Z"},
    }
    if execution_block is not None:
        state["execution"] = execution_block
    path = os.path.join(
        base_dir, ".orchestrator", "handoffs", f"{handoff_id}.yaml"
    )
    with open(path, "w") as f:
        yaml.dump(state, f)


def _write_session(base_dir, session_id, room_id, handoff_id):
    state = {
        "session": {
            "id": session_id,
            "peer_id": "test-peer",
            "room_id": room_id,
            "handoff_id": handoff_id,
            "tmux_session": "sess-1",
            "tmux_target": "%1",
            "mode": "ephemeral",
            "status": "busy",
            "cwd": base_dir,
        }
    }
    path = os.path.join(
        base_dir, ".orchestrator", "runtime", "sessions",
        f"{session_id}.yaml",
    )
    with open(path, "w") as f:
        yaml.dump(state, f)


def _read_bootstrap_md(base_dir, session_id):
    p = os.path.join(
        base_dir, ".orchestrator", "runtime", "bootstrap",
        f"{session_id}.md",
    )
    with open(p) as f:
        return f.read()


def test_cmd_session_bootstrap_delegate_required_renders_hard_block(
    monkeypatch, tmp_path
):
    base_dir = str(tmp_path)
    _patch_storage(monkeypatch, base_dir)
    _write_room(base_dir, "r1")
    _write_handoff(
        base_dir, "h1", "r1",
        execution_block={"mode": "delegate_required", "child_handoffs": []},
    )
    _write_session(base_dir, "s1", "r1", "h1")

    bootstrap.cmd_session_bootstrap(_Args(session_id="s1"))

    content = _read_bootstrap_md(base_dir, "s1")
    assert HARD_HEADING in content
    assert HARD_GATE_SENTENCE in content
    assert CLEANUP_HEADING in content


def test_cmd_session_bootstrap_legacy_handoff_does_not_render_hard_block(
    monkeypatch, tmp_path
):
    """Legacy handoffs (no execution block) MUST NOT crash and MUST NOT
    silently gain the delegate_required block."""
    base_dir = str(tmp_path)
    _patch_storage(monkeypatch, base_dir)
    _write_room(base_dir, "r1")
    _write_handoff(base_dir, "h1", "r1", execution_block=None)
    _write_session(base_dir, "s1", "r1", "h1")

    bootstrap.cmd_session_bootstrap(_Args(session_id="s1"))

    content = _read_bootstrap_md(base_dir, "s1")
    assert HARD_HEADING not in content
    assert HARD_GATE_SENTENCE not in content
    # ...but POST-COMPLETE CLEANUP is still applied universally.
    assert CLEANUP_HEADING in content


def test_cmd_session_bootstrap_delegate_optional_does_not_render_hard_block(
    monkeypatch, tmp_path
):
    base_dir = str(tmp_path)
    _patch_storage(monkeypatch, base_dir)
    _write_room(base_dir, "r1")
    _write_handoff(
        base_dir, "h1", "r1",
        execution_block={"mode": "delegate_optional", "child_handoffs": []},
    )
    _write_session(base_dir, "s1", "r1", "h1")

    bootstrap.cmd_session_bootstrap(_Args(session_id="s1"))

    content = _read_bootstrap_md(base_dir, "s1")
    assert HARD_HEADING not in content
    assert CLEANUP_HEADING in content
