"""Tests for the delegate_required execution mode + add-subtask CLI + hard gate.

Covers:
- execution.mode YAML round-trip across all three enum values + legacy absent.
- 'handoff create' default execution.mode and explicit --execution-mode flag.
- 'handoff add-subtask' append semantics, slug validation, byte-level invariant.
- 'handoff complete' delegate_required gate (fails closed without children;
  succeeds with one well-formed completed child).
- 'handoff complete' bypasses gate for direct/delegate_optional/legacy.
- _render_brief surfaces 'Execution mode:' and 'Subtask Ledger' correctly.

All tests are hermetic: tmp dirs, monkeypatch, no real state.
"""
import hashlib
import os
import sys

import pytest
import yaml

_REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), os.pardir))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

from lib import storage, dispatch  # noqa: E402
from lib.handoffs import (  # noqa: E402
    cmd_handoff_create,
    cmd_handoff_add_subtask,
    cmd_handoff_claim,
    cmd_handoff_complete,
    _render_brief,
    _get_execution_mode,
    _get_child_handoffs,
    VALID_EXECUTION_MODES,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

class Args:
    def __init__(self, **kwargs):
        self.__dict__.update(kwargs)


def _patch_storage(monkeypatch, base_dir):
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

    os.makedirs(rooms_dir, exist_ok=True)
    os.makedirs(handoffs_dir, exist_ok=True)
    os.makedirs(sessions_dir, exist_ok=True)
    os.makedirs(locks_dir, exist_ok=True)


def _setup_world(base_dir, room_id="test-room", peer_id="test-worker"):
    """Lay down a minimal room + peer registry + TEMPLATE so create/complete work."""
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

    template_dir = os.path.join(rooms_dir, "TEMPLATE")
    os.makedirs(template_dir, exist_ok=True)
    with open(os.path.join(template_dir, "state.yaml"), "w") as f:
        yaml.dump({"room": {"id": "TEMPLATE"}}, f)

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


def _create_args(handoff_id, room_id="test-room", peer_id="test-worker", execution_mode=None):
    return Args(
        handoff_id=handoff_id,
        room=room_id,
        to=peer_id,
        task="dummy task",
        priority="medium",
        scope="",
        report_back="",
        non_goals=None,
        invariants=None,
        failure_examples=None,
        validation=None,
        constraints=None,
        acceptance_criteria=None,
        kind="implementation",
        execution_mode=execution_mode,
    )


def _complete_args(handoff_id, peer_id="test-worker"):
    return Args(
        handoff_id=handoff_id,
        by=peer_id,
        summary="done",
        files=[],
        verifications=[],
        risks=[],
        validation_covers=[],
        task_criterion_covers=[],
        room_criterion_covers=[],
    )


def _claim(handoff_id, peer_id="test-worker"):
    cmd_handoff_claim(Args(handoff_id=handoff_id, by=peer_id))


def _add_subtask(parent_id, sub_id, *, model_target="sonnet",
                 owned_files=None, status="completed", evidence="ok",
                 parent_criterion=None):
    cmd_handoff_add_subtask(Args(
        handoff_id=parent_id,
        id=sub_id,
        model_target=model_target,
        owned_files=list(owned_files) if owned_files is not None else ["lib/x.py"],
        status=status,
        evidence=evidence,
        parent_criterion=parent_criterion,
    ))


def _read_handoff(base_dir, handoff_id):
    path = os.path.join(base_dir, ".orchestrator", "handoffs", f"{handoff_id}.yaml")
    return storage.read_state(path)


# ---------------------------------------------------------------------------
# Schema round-trip
# ---------------------------------------------------------------------------

def test_execution_mode_roundtrips_for_all_enum_values_plus_legacy(monkeypatch, tmp_path):
    """execution.mode must round-trip through YAML save/load for every enum
    value, and a legacy handoff (no execution block) must read back as None."""
    base = str(tmp_path)
    _patch_storage(monkeypatch, base)
    _setup_world(base)

    for mode in VALID_EXECUTION_MODES:
        hid = f"rt-{mode.replace('_', '-')}"
        cmd_handoff_create(_create_args(hid, execution_mode=mode))
        state = _read_handoff(base, hid)
        assert state["execution"]["mode"] == mode
        assert state["execution"]["child_handoffs"] == []
        assert _get_execution_mode(state) == mode

    legacy_path = os.path.join(base, ".orchestrator", "handoffs", "legacy.yaml")
    legacy_state = {
        "handoff": {
            "id": "legacy",
            "room_id": "test-room",
            "from": "orchestrator",
            "to": "test-worker",
            "status": "open",
            "priority": "medium",
            "kind": "implementation",
        },
        "task": {"description": "legacy", "validation": [], "acceptance_criteria": []},
        "timestamps": {"created_at": "2026-01-01T00:00:00Z"},
    }
    with open(legacy_path, "w") as f:
        yaml.dump(legacy_state, f)
    state = storage.read_state(legacy_path)
    assert _get_execution_mode(state) is None


def test_create_default_is_delegate_optional(monkeypatch, tmp_path):
    """When --execution-mode is omitted, the new handoff must default to
    delegate_optional (loose, current behavior preserved)."""
    base = str(tmp_path)
    _patch_storage(monkeypatch, base)
    _setup_world(base)

    cmd_handoff_create(_create_args("h-default", execution_mode=None))
    state = _read_handoff(base, "h-default")
    assert state["execution"]["mode"] == "delegate_optional"


def test_create_with_delegate_required_persists(monkeypatch, tmp_path):
    """Explicit --execution-mode delegate_required must produce
    execution.mode=delegate_required in the YAML."""
    base = str(tmp_path)
    _patch_storage(monkeypatch, base)
    _setup_world(base)

    cmd_handoff_create(_create_args("h-req", execution_mode="delegate_required"))
    state = _read_handoff(base, "h-req")
    assert state["execution"]["mode"] == "delegate_required"
    assert state["execution"]["child_handoffs"] == []


# ---------------------------------------------------------------------------
# add-subtask
# ---------------------------------------------------------------------------

def test_add_subtask_appends_well_formed_entry(monkeypatch, tmp_path):
    """add-subtask must append a structured entry to execution.child_handoffs."""
    base = str(tmp_path)
    _patch_storage(monkeypatch, base)
    _setup_world(base)

    cmd_handoff_create(_create_args("h-add", execution_mode="delegate_required"))
    _add_subtask(
        "h-add",
        "h-add-sub-1",
        model_target="sonnet",
        owned_files=["lib/foo.py", "lib/bar.py"],
        status="completed",
        evidence="pytest 5/5 passed",
        parent_criterion="TA1",
    )

    state = _read_handoff(base, "h-add")
    children = state["execution"]["child_handoffs"]
    assert len(children) == 1
    entry = children[0]
    assert entry["id"] == "h-add-sub-1"
    assert entry["model_target"] == "sonnet"
    assert entry["owned_files"] == ["lib/foo.py", "lib/bar.py"]
    assert entry["status"] == "completed"
    assert entry["evidence"] == "pytest 5/5 passed"
    assert entry["parent_criterion"] == "TA1"


def test_add_subtask_rejects_unsafe_id(monkeypatch, tmp_path):
    """add-subtask must refuse path-traversal / non-slug --id values without
    mutating parent state."""
    base = str(tmp_path)
    _patch_storage(monkeypatch, base)
    _setup_world(base)

    cmd_handoff_create(_create_args("h-unsafe", execution_mode="delegate_required"))
    before = _read_handoff(base, "h-unsafe")

    bad_ids = ["../escape", "has spaces", "Upper", "../../etc/passwd", ""]
    for bad in bad_ids:
        with pytest.raises(SystemExit):
            cmd_handoff_add_subtask(Args(
                handoff_id="h-unsafe",
                id=bad,
                model_target="sonnet",
                owned_files=["lib/x.py"],
                status="completed",
                evidence="ok",
                parent_criterion=None,
            ))

    after = _read_handoff(base, "h-unsafe")
    assert after["execution"]["child_handoffs"] == []
    assert after == before  # parent state untouched


def test_add_subtask_byte_invariant_only_appends_to_children(monkeypatch, tmp_path):
    """Parent YAML before/after add-subtask must differ only in
    execution.child_handoffs — every other field is byte-identical."""
    base = str(tmp_path)
    _patch_storage(monkeypatch, base)
    _setup_world(base)

    cmd_handoff_create(_create_args("h-inv", execution_mode="delegate_required"))

    before = _read_handoff(base, "h-inv")
    before_minus_children = dict(before)
    before_minus_children["execution"] = {
        k: v for k, v in before["execution"].items() if k != "child_handoffs"
    }
    before_hash = hashlib.sha256(
        yaml.dump(before_minus_children, sort_keys=True).encode()
    ).hexdigest()

    _add_subtask("h-inv", "h-inv-sub-1",
                 owned_files=["lib/x.py"], status="completed", evidence="ok")

    after = _read_handoff(base, "h-inv")
    after_minus_children = dict(after)
    after_minus_children["execution"] = {
        k: v for k, v in after["execution"].items() if k != "child_handoffs"
    }
    after_hash = hashlib.sha256(
        yaml.dump(after_minus_children, sort_keys=True).encode()
    ).hexdigest()

    assert before_hash == after_hash, "non-children fields must be untouched"
    assert before["execution"]["child_handoffs"] == []
    assert len(after["execution"]["child_handoffs"]) == 1


def test_add_subtask_rejects_completed_without_owned_files(monkeypatch, tmp_path):
    """status=completed must require >=1 owned_files; reject otherwise."""
    base = str(tmp_path)
    _patch_storage(monkeypatch, base)
    _setup_world(base)

    cmd_handoff_create(_create_args("h-noown", execution_mode="delegate_required"))
    with pytest.raises(SystemExit):
        cmd_handoff_add_subtask(Args(
            handoff_id="h-noown",
            id="h-noown-sub-1",
            model_target="sonnet",
            owned_files=[],
            status="completed",
            evidence="ok",
            parent_criterion=None,
        ))
    state = _read_handoff(base, "h-noown")
    assert state["execution"]["child_handoffs"] == []


def test_add_subtask_rejects_completed_without_evidence(monkeypatch, tmp_path):
    """status=completed must require non-empty evidence; reject otherwise."""
    base = str(tmp_path)
    _patch_storage(monkeypatch, base)
    _setup_world(base)

    cmd_handoff_create(_create_args("h-noev", execution_mode="delegate_required"))
    with pytest.raises(SystemExit):
        cmd_handoff_add_subtask(Args(
            handoff_id="h-noev",
            id="h-noev-sub-1",
            model_target="sonnet",
            owned_files=["lib/x.py"],
            status="completed",
            evidence="   ",
            parent_criterion=None,
        ))
    state = _read_handoff(base, "h-noev")
    assert state["execution"]["child_handoffs"] == []


# ---------------------------------------------------------------------------
# Completion gate
# ---------------------------------------------------------------------------

def test_complete_delegate_required_no_children_fails_closed(monkeypatch, tmp_path):
    """delegate_required parent with no completed children must fail to
    complete and leave the handoff state file unchanged."""
    base = str(tmp_path)
    _patch_storage(monkeypatch, base)
    _setup_world(base)

    cmd_handoff_create(_create_args("h-fail", execution_mode="delegate_required"))
    _claim("h-fail")

    path = os.path.join(base, ".orchestrator", "handoffs", "h-fail.yaml")
    with open(path, "rb") as f:
        before_bytes = f.read()

    with pytest.raises(SystemExit):
        cmd_handoff_complete(_complete_args("h-fail"))

    with open(path, "rb") as f:
        after_bytes = f.read()
    assert before_bytes == after_bytes, "failed gate must not mutate state"

    state = _read_handoff(base, "h-fail")
    assert state["handoff"]["status"] == "claimed"
    assert "completed_at" not in state.get("timestamps", {}) \
        or state["timestamps"].get("completed_at") in (None, "")


def test_complete_delegate_required_with_one_completed_child_succeeds(monkeypatch, tmp_path):
    """delegate_required parent with at least one completed child
    (owned_files=['lib/x.py'], evidence='ok') must complete successfully."""
    base = str(tmp_path)
    _patch_storage(monkeypatch, base)
    _setup_world(base)

    cmd_handoff_create(_create_args("h-pass", execution_mode="delegate_required"))
    _add_subtask("h-pass", "h-pass-sub-1",
                 owned_files=["lib/x.py"], status="completed", evidence="ok")
    _claim("h-pass")
    cmd_handoff_complete(_complete_args("h-pass"))

    state = _read_handoff(base, "h-pass")
    assert state["handoff"]["status"] == "completed"


def test_complete_delegate_required_rejects_completed_child_with_empty_evidence(monkeypatch, tmp_path):
    """The gate must catch a completed child whose evidence is whitespace-only
    even if it slipped past add-subtask (defensive — gate is the last word)."""
    base = str(tmp_path)
    _patch_storage(monkeypatch, base)
    _setup_world(base)

    cmd_handoff_create(_create_args("h-bad-ev", execution_mode="delegate_required"))

    path = os.path.join(base, ".orchestrator", "handoffs", "h-bad-ev.yaml")
    state = storage.read_state(path)
    state["execution"]["child_handoffs"] = [{
        "id": "smuggled",
        "model_target": "sonnet",
        "owned_files": ["lib/x.py"],
        "status": "completed",
        "evidence": "   ",
    }]
    storage.write_state(path, state)

    _claim("h-bad-ev")
    with pytest.raises(SystemExit):
        cmd_handoff_complete(_complete_args("h-bad-ev"))

    state = _read_handoff(base, "h-bad-ev")
    assert state["handoff"]["status"] == "claimed"


def test_complete_legacy_handoff_no_execution_block_succeeds(monkeypatch, tmp_path):
    """A legacy handoff (no execution block at all) must complete normally —
    the gate must None-check and bypass."""
    base = str(tmp_path)
    _patch_storage(monkeypatch, base)
    _setup_world(base)

    handoffs_dir = os.path.join(base, ".orchestrator", "handoffs")
    legacy_state = {
        "handoff": {
            "id": "legacy",
            "room_id": "test-room",
            "from": "orchestrator",
            "to": "test-worker",
            "status": "claimed",
            "priority": "medium",
            "kind": "implementation",
        },
        "task": {"description": "legacy", "validation": [], "acceptance_criteria": []},
        "timestamps": {"created_at": "2026-01-01T00:00:00Z",
                       "claimed_at": "2026-01-01T00:00:01Z"},
    }
    with open(os.path.join(handoffs_dir, "legacy.yaml"), "w") as f:
        yaml.dump(legacy_state, f)

    cmd_handoff_complete(_complete_args("legacy"))
    state = _read_handoff(base, "legacy")
    assert state["handoff"]["status"] == "completed"


def test_complete_direct_mode_no_children_succeeds(monkeypatch, tmp_path):
    """direct mode bypasses the gate — complete works without any children."""
    base = str(tmp_path)
    _patch_storage(monkeypatch, base)
    _setup_world(base)

    cmd_handoff_create(_create_args("h-direct", execution_mode="direct"))
    _claim("h-direct")
    cmd_handoff_complete(_complete_args("h-direct"))

    state = _read_handoff(base, "h-direct")
    assert state["handoff"]["status"] == "completed"
    assert state["execution"]["mode"] == "direct"
    assert state["execution"]["child_handoffs"] == []


def test_complete_delegate_optional_no_children_succeeds(monkeypatch, tmp_path):
    """delegate_optional bypasses the gate — current behavior preserved."""
    base = str(tmp_path)
    _patch_storage(monkeypatch, base)
    _setup_world(base)

    cmd_handoff_create(_create_args("h-opt", execution_mode="delegate_optional"))
    _claim("h-opt")
    cmd_handoff_complete(_complete_args("h-opt"))

    state = _read_handoff(base, "h-opt")
    assert state["handoff"]["status"] == "completed"


# ---------------------------------------------------------------------------
# Brief rendering
# ---------------------------------------------------------------------------

def test_brief_renders_execution_mode_line_when_block_present(monkeypatch, tmp_path):
    """_render_brief must include 'Execution mode: <value>' for handoffs that
    carry an execution block."""
    base = str(tmp_path)
    _patch_storage(monkeypatch, base)
    _setup_world(base)

    cmd_handoff_create(_create_args("h-brief", execution_mode="delegate_required"))
    state = _read_handoff(base, "h-brief")
    rooms_dir = os.path.join(base, ".orchestrator", "rooms")
    room_state = storage.read_state(os.path.join(rooms_dir, "test-room", "state.yaml"))

    brief = _render_brief(state, room_state)
    assert "Execution mode:" in brief
    assert "delegate_required" in brief


def test_brief_renders_subtask_ledger_only_for_delegate_required_with_children(monkeypatch, tmp_path):
    """The 'Subtask Ledger' section appears iff mode=delegate_required AND
    children are non-empty. Other configurations must not render the section."""
    base = str(tmp_path)
    _patch_storage(monkeypatch, base)
    _setup_world(base)

    rooms_dir = os.path.join(base, ".orchestrator", "rooms")
    room_state = storage.read_state(os.path.join(rooms_dir, "test-room", "state.yaml"))

    # delegate_required + 0 children -> no ledger
    cmd_handoff_create(_create_args("h-empty", execution_mode="delegate_required"))
    brief_empty = _render_brief(_read_handoff(base, "h-empty"), room_state)
    assert "Subtask Ledger" not in brief_empty

    # delegate_optional + 1 child (forced) -> still no ledger
    cmd_handoff_create(_create_args("h-opt-child", execution_mode="delegate_optional"))
    _add_subtask("h-opt-child", "h-opt-child-s1",
                 owned_files=["lib/x.py"], status="completed", evidence="ok")
    brief_opt = _render_brief(_read_handoff(base, "h-opt-child"), room_state)
    assert "Subtask Ledger" not in brief_opt

    # delegate_required + 1 child -> ledger with the entry
    cmd_handoff_create(_create_args("h-req-child", execution_mode="delegate_required"))
    _add_subtask("h-req-child", "h-req-child-s1",
                 model_target="haiku",
                 owned_files=["lib/x.py", "tests/y.py"],
                 status="completed", evidence="ok")
    brief_req = _render_brief(_read_handoff(base, "h-req-child"), room_state)
    assert "Subtask Ledger" in brief_req
    assert "h-req-child-s1" in brief_req
    assert "haiku" in brief_req
    assert "completed" in brief_req


def test_brief_legacy_handoff_omits_execution_mode_line(monkeypatch, tmp_path):
    """A legacy handoff without an execution block must NOT show an
    'Execution mode:' line in its brief."""
    base = str(tmp_path)
    _patch_storage(monkeypatch, base)
    _setup_world(base)

    rooms_dir = os.path.join(base, ".orchestrator", "rooms")
    room_state = storage.read_state(os.path.join(rooms_dir, "test-room", "state.yaml"))

    legacy_state = {
        "handoff": {
            "id": "legacy",
            "room_id": "test-room",
            "from": "orchestrator",
            "to": "test-worker",
            "status": "open",
            "priority": "medium",
            "kind": "implementation",
        },
        "task": {"description": "legacy", "validation": [], "acceptance_criteria": []},
        "timestamps": {"created_at": "2026-01-01T00:00:00Z"},
    }
    brief = _render_brief(legacy_state, room_state)
    assert "Execution mode:" not in brief
    assert "Subtask Ledger" not in brief


def test_get_child_handoffs_helper_returns_list(monkeypatch, tmp_path):
    """_get_child_handoffs must return a list (possibly empty) for any input
    shape — never crash on legacy or malformed states."""
    assert _get_child_handoffs({}) == []
    assert _get_child_handoffs({"execution": None}) == []
    assert _get_child_handoffs({"execution": {}}) == []
    assert _get_child_handoffs({"execution": {"child_handoffs": None}}) == []
    assert _get_child_handoffs({"execution": {"child_handoffs": [{"id": "x"}]}}) == [{"id": "x"}]
