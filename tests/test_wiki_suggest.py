"""Tests for lib/wiki_suggest.py — wiki auto-accumulation hook.

All tests are hermetic: tmp dirs, monkeypatch, no real state.
"""
import os
import sys
import pytest
import yaml
from unittest import mock

_REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), os.pardir))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

from lib import storage, dispatch  # noqa: E402
from lib.wiki_suggest import (  # noqa: E402
    detect_continuity,
    build_wiki_delta,
    render_wiki_suggestions,
    cmd_handoff_wiki_suggest,
    _try_wiki_suggest_auto,
)


# ---------------------------------------------------------------------------
# Helpers (same pattern as test_alpha_fixes.py)
# ---------------------------------------------------------------------------

class Args:
    def __init__(self, **kwargs):
        self.__dict__.update(kwargs)


def _setup_room_and_handoff(
    base_dir,
    room_id="test-room",
    handoff_id="test-handoff",
    peer_id="test-worker",
    handoff_status="completed",
    review_outcome=None,
    review_note="",
    rework_of=None,
    must_address=None,
    risks=None,
    discovery=None,
    lifecycle=None,
):
    """Create minimal room + handoff + peer in base_dir for wiki-suggest testing."""
    rooms_dir = os.path.join(base_dir, ".orchestrator", "rooms")
    os.makedirs(os.path.join(rooms_dir, room_id), exist_ok=True)

    room_state = {
        "room": {"id": room_id, "name": "Test", "status": "active"},
        "context": {"goal": "test", "execution_cwd": base_dir},
        "lifecycle": lifecycle or {"current_phase": "execution"},
    }
    if discovery:
        room_state["discovery"] = discovery
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
            "status": handoff_status,
            "priority": "medium",
            "kind": "implementation",
        },
        "task": {"description": "test task", "validation": [], "acceptance_criteria": []},
        "timestamps": {"created_at": "2026-01-01T00:00:00Z"},
    }
    if rework_of:
        handoff_state["handoff"]["rework_of"] = rework_of
    if review_outcome:
        handoff_state["review"] = {
            "outcome": review_outcome,
            "reviewed_by": "reviewer",
            "reviewed_at": "2026-01-01T01:00:00Z",
            "note": review_note,
        }
        if must_address:
            handoff_state["rework"] = {"must_address": must_address}
    if risks:
        handoff_state["resolution"] = {"risks": risks}

    with open(os.path.join(handoffs_dir, f"{handoff_id}.yaml"), "w") as f:
        yaml.dump(handoff_state, f)

    # Peer registry
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
# Group 1: Continuity detection
# ---------------------------------------------------------------------------

def test_continuity_rework_always_continuous(monkeypatch, tmp_path):
    """handoff with rework_of field → is_continuous=True, continuity_reason='rework_lineage'."""
    base_dir = str(tmp_path)
    _setup_room_and_handoff(
        base_dir,
        handoff_id="rework-handoff",
        handoff_status="completed",
        review_outcome="approved",
        rework_of="original-handoff",
    )
    # Also create the original handoff
    handoffs_dir = os.path.join(base_dir, ".orchestrator", "handoffs")
    orig_state = {
        "handoff": {"id": "original-handoff", "room_id": "test-room", "status": "completed"},
        "review": {"outcome": "changes_requested"},
    }
    with open(os.path.join(handoffs_dir, "original-handoff.yaml"), "w") as f:
        yaml.dump(orig_state, f)

    _patch_storage(monkeypatch, base_dir)

    result = detect_continuity("test-room", "rework-handoff")

    assert result["is_continuous"] is True
    assert result["continuity_reason"] == "rework_lineage"
    assert result["cycle_count"] >= 2


def test_continuity_same_room_prior_review(monkeypatch, tmp_path):
    """room has another handoff with review.outcome='approved' → is_continuous=True."""
    base_dir = str(tmp_path)
    _setup_room_and_handoff(
        base_dir,
        handoff_id="current-handoff",
        handoff_status="completed",
        review_outcome="approved",
    )
    # Create a prior handoff for same room with approved review
    handoffs_dir = os.path.join(base_dir, ".orchestrator", "handoffs")
    prior_state = {
        "handoff": {"id": "prior-handoff", "room_id": "test-room", "status": "completed"},
        "review": {"outcome": "approved", "note": "looks good"},
    }
    with open(os.path.join(handoffs_dir, "prior-handoff.yaml"), "w") as f:
        yaml.dump(prior_state, f)

    _patch_storage(monkeypatch, base_dir)

    result = detect_continuity("test-room", "current-handoff")

    assert result["is_continuous"] is True
    assert result["continuity_reason"] == "same_room_prior_review"
    assert "prior-handoff" in result["prior_handoffs"]


def test_continuity_standalone_no_prior(monkeypatch, tmp_path):
    """room has only this handoff, no reviews → is_continuous=False, continuity_reason='none'."""
    base_dir = str(tmp_path)
    _setup_room_and_handoff(
        base_dir,
        handoff_id="solo-handoff",
        handoff_status="completed",
        review_outcome="approved",
    )
    _patch_storage(monkeypatch, base_dir)

    result = detect_continuity("test-room", "solo-handoff")

    assert result["is_continuous"] is False
    assert result["continuity_reason"] == "none"
    assert result["prior_handoffs"] == []


def test_continuity_different_room_not_counted(monkeypatch, tmp_path):
    """handoff in different room with review → doesn't affect continuity for our room."""
    base_dir = str(tmp_path)
    _setup_room_and_handoff(
        base_dir,
        room_id="our-room",
        handoff_id="our-handoff",
        handoff_status="completed",
        review_outcome="approved",
    )
    # Create another room and handoff for it
    other_rooms_dir = os.path.join(base_dir, ".orchestrator", "rooms", "other-room")
    os.makedirs(other_rooms_dir, exist_ok=True)
    with open(os.path.join(other_rooms_dir, "state.yaml"), "w") as f:
        yaml.dump({"room": {"id": "other-room"}}, f)

    handoffs_dir = os.path.join(base_dir, ".orchestrator", "handoffs")
    other_state = {
        "handoff": {"id": "other-handoff", "room_id": "other-room", "status": "completed"},
        "review": {"outcome": "approved"},
    }
    with open(os.path.join(handoffs_dir, "other-handoff.yaml"), "w") as f:
        yaml.dump(other_state, f)

    _patch_storage(monkeypatch, base_dir)

    result = detect_continuity("our-room", "our-handoff")

    assert result["is_continuous"] is False
    assert result["continuity_reason"] == "none"


# ---------------------------------------------------------------------------
# Group 2: Delta extraction
# ---------------------------------------------------------------------------

def _make_continuity(cycle_count=2, reason="same_room_prior_review"):
    return {
        "is_continuous": True,
        "cycle_count": cycle_count,
        "prior_handoffs": [],
        "continuity_reason": reason,
    }


def test_delta_lessons_from_review_note():
    """approved handoff with review.note → lessons hint generated."""
    handoff_state = {
        "handoff": {"id": "h1", "room_id": "r1"},
        "review": {"outcome": "approved", "note": "Great work, very clean implementation."},
    }
    room_state = {"room": {"id": "r1"}, "lifecycle": {"current_phase": "execution"}}

    delta = build_wiki_delta(handoff_state, room_state, _make_continuity(), "approve", [])

    assert delta["has_suggestions"] is True
    assert len(delta["pages"]["lessons"]) == 1
    assert delta["pages"]["lessons"][0]["hint"] == "Great work, very clean implementation."
    assert delta["pages"]["lessons"][0]["source"] == "review.note"


def test_delta_lessons_from_must_address():
    """rework handoff with rework.must_address → lessons hints."""
    handoff_state = {
        "handoff": {"id": "h1", "room_id": "r1", "rework_of": "h0"},
        "review": {"outcome": "changes_requested", "note": ""},
        "rework": {"must_address": ["Fix the error handling", "Add logging"]},
    }
    room_state = {"room": {"id": "r1"}, "lifecycle": {"current_phase": "execution"}}

    delta = build_wiki_delta(handoff_state, room_state, _make_continuity(), "rework", [])

    assert delta["has_suggestions"] is True
    lessons = delta["pages"]["lessons"]
    hints = [h["hint"] for h in lessons]
    assert "Fix the error handling" in hints
    assert "Add logging" in hints
    assert all(h["source"] == "rework.must_address" for h in lessons)


def test_delta_decisions_from_discovery():
    """room with discovery.decisions_made → decisions hint."""
    handoff_state = {
        "handoff": {"id": "h1", "room_id": "r1"},
        "review": {"outcome": "approved", "note": ""},
    }
    room_state = {
        "room": {"id": "r1"},
        "lifecycle": {"current_phase": "execution"},
        "discovery": {"decisions_made": ["Use async processing", "Adopt redis for caching"]},
    }

    delta = build_wiki_delta(handoff_state, room_state, _make_continuity(), "approve", [])

    assert delta["has_suggestions"] is True
    decisions = delta["pages"]["decisions"]
    hints = [h["hint"] for h in decisions]
    assert "Use async processing" in hints
    assert "Adopt redis for caching" in hints


def test_delta_deferred_from_risks():
    """resolution.risks with 'defer' keyword → deferred hint with matched_text."""
    handoff_state = {
        "handoff": {"id": "h1", "room_id": "r1"},
        "review": {"outcome": "approved", "note": ""},
        "resolution": {"risks": ["OAuth support is deferred to next sprint", "Normal risk item"]},
    }
    room_state = {"room": {"id": "r1"}, "lifecycle": {"current_phase": "execution"}}

    delta = build_wiki_delta(handoff_state, room_state, _make_continuity(), "approve", [])

    deferred = delta["pages"]["deferred"]
    assert len(deferred) == 1
    assert "OAuth support is deferred to next sprint" == deferred[0]["hint"]
    assert deferred[0]["matched_text"] == "deferred"


def test_delta_no_hints_empty_sources():
    """No review note, no discovery, no risks → has_suggestions=False."""
    handoff_state = {
        "handoff": {"id": "h1", "room_id": "r1"},
        "review": {"outcome": "approved", "note": ""},
    }
    room_state = {"room": {"id": "r1"}, "lifecycle": {"current_phase": "execution"}}

    delta = build_wiki_delta(handoff_state, room_state, _make_continuity(), "approve", [])

    assert delta["has_suggestions"] is False


# ---------------------------------------------------------------------------
# Group 3: Strength
# ---------------------------------------------------------------------------

def test_strength_high_for_approve():
    """source_event='approve' → strength='high'."""
    handoff_state = {
        "handoff": {"id": "h1", "room_id": "r1"},
        "review": {"outcome": "approved", "note": "good"},
    }
    room_state = {"room": {"id": "r1"}, "lifecycle": {"current_phase": "execution"}}

    delta = build_wiki_delta(handoff_state, room_state, _make_continuity(), "approve", [])

    assert delta["strength"] == "high"


def test_strength_medium_for_rework():
    """source_event='rework' → strength='medium', only lessons/patterns emitted."""
    handoff_state = {
        "handoff": {"id": "h1", "room_id": "r1", "rework_of": "h0"},
        "review": {"outcome": "changes_requested", "note": "needs work"},
        "rework": {"must_address": ["Fix X"]},
        "resolution": {"risks": ["Defer Y for later"]},
    }
    room_state = {
        "room": {"id": "r1"},
        "lifecycle": {"current_phase": "execution"},
        "discovery": {"decisions_made": ["Use Z"]},
    }

    delta = build_wiki_delta(handoff_state, room_state, _make_continuity(), "rework", [])

    assert delta["strength"] == "medium"
    # decisions, deferred, current_state should be empty for rework
    assert delta["pages"]["decisions"] == []
    assert delta["pages"]["deferred"] == []
    assert delta["pages"]["current_state"] == []
    # lessons should have content
    assert len(delta["pages"]["lessons"]) > 0


# ---------------------------------------------------------------------------
# Group 4: Dedupe
# ---------------------------------------------------------------------------

def test_dedupe_same_hint_filtered():
    """Prior handoff has stored hint matching current → duplicate filtered."""
    prior_state = {
        "handoff": {"id": "h0", "room_id": "r1"},
        "review": {"outcome": "approved", "note": "Same note as before"},
        "wiki_suggest": {
            "generated_hints": {
                "lessons": ["same note as before"],  # normalized
                "decisions": [], "deferred": [], "patterns": [], "current_state": [],
            }
        },
    }
    handoff_state = {
        "handoff": {"id": "h1", "room_id": "r1"},
        "review": {"outcome": "approved", "note": "Same note as before"},
    }
    room_state = {"room": {"id": "r1"}, "lifecycle": {"current_phase": "execution"}}
    delta = build_wiki_delta(handoff_state, room_state, _make_continuity(), "approve", [prior_state])
    assert all(h["hint"] != "Same note as before" for h in delta["pages"]["lessons"])


def test_dedupe_different_hints_kept():
    """Prior handoff has different stored hint → current hint kept."""
    prior_state = {
        "handoff": {"id": "h0", "room_id": "r1"},
        "review": {"outcome": "approved", "note": "Prior note"},
        "wiki_suggest": {
            "generated_hints": {
                "lessons": ["prior note"],  # different from current
                "decisions": [], "deferred": [], "patterns": [], "current_state": [],
            }
        },
    }
    handoff_state = {
        "handoff": {"id": "h1", "room_id": "r1"},
        "review": {"outcome": "approved", "note": "New different note"},
    }
    room_state = {"room": {"id": "r1"}, "lifecycle": {"current_phase": "execution"}}
    delta = build_wiki_delta(handoff_state, room_state, _make_continuity(), "approve", [prior_state])
    hints = [h["hint"] for h in delta["pages"]["lessons"]]
    assert "New different note" in hints


# ---------------------------------------------------------------------------
# Group 5: CLI integration
# ---------------------------------------------------------------------------

def test_wiki_suggest_cli_approved_handoff(monkeypatch, tmp_path, capsys):
    """Approved handoff with review.note in continuous room → outputs suggestions."""
    base_dir = str(tmp_path)
    _setup_room_and_handoff(
        base_dir,
        handoff_id="current-handoff",
        handoff_status="completed",
        review_outcome="approved",
        review_note="Great implementation of the feature.",
    )
    # Create a prior handoff so this room is continuous
    handoffs_dir = os.path.join(base_dir, ".orchestrator", "handoffs")
    prior_state = {
        "handoff": {"id": "prior-handoff", "room_id": "test-room", "status": "completed"},
        "review": {"outcome": "approved", "note": "prior note"},
    }
    with open(os.path.join(handoffs_dir, "prior-handoff.yaml"), "w") as f:
        yaml.dump(prior_state, f)

    _patch_storage(monkeypatch, base_dir)

    args = Args(handoff_id="current-handoff")
    cmd_handoff_wiki_suggest(args)

    captured = capsys.readouterr()
    assert "wiki suggestions" in captured.out
    assert "current-handoff" in captured.out
    assert "Great implementation of the feature." in captured.out


def test_wiki_suggest_cli_standalone_skips(monkeypatch, tmp_path, capsys):
    """Single handoff in room → prints skip message."""
    base_dir = str(tmp_path)
    _setup_room_and_handoff(
        base_dir,
        handoff_id="solo-handoff",
        handoff_status="completed",
        review_outcome="approved",
        review_note="Some note",
    )
    _patch_storage(monkeypatch, base_dir)

    args = Args(handoff_id="solo-handoff")
    cmd_handoff_wiki_suggest(args)

    captured = capsys.readouterr()
    assert "standalone" in captured.out


def test_wiki_suggest_cli_not_completed_errors(monkeypatch, tmp_path):
    """Open handoff → sys.exit."""
    base_dir = str(tmp_path)
    _setup_room_and_handoff(
        base_dir,
        handoff_id="open-handoff",
        handoff_status="open",
    )
    _patch_storage(monkeypatch, base_dir)

    args = Args(handoff_id="open-handoff")
    with pytest.raises(SystemExit):
        cmd_handoff_wiki_suggest(args)


# ---------------------------------------------------------------------------
# Group 6: Auto-invoke integration
# ---------------------------------------------------------------------------

def test_auto_suggest_disabled_by_config(monkeypatch, tmp_path, capsys):
    """wiki.auto_suggest=false → _try_wiki_suggest_auto produces no output."""
    base_dir = str(tmp_path)
    _setup_room_and_handoff(
        base_dir,
        handoff_id="test-handoff",
        handoff_status="completed",
        review_outcome="approved",
        review_note="Important note",
    )
    # Create a prior handoff to make it continuous
    handoffs_dir = os.path.join(base_dir, ".orchestrator", "handoffs")
    prior_state = {
        "handoff": {"id": "prior-handoff", "room_id": "test-room", "status": "completed"},
        "review": {"outcome": "approved", "note": "prior"},
    }
    with open(os.path.join(handoffs_dir, "prior-handoff.yaml"), "w") as f:
        yaml.dump(prior_state, f)

    _patch_storage(monkeypatch, base_dir)

    # Patch load_config to return wiki.auto_suggest=False
    from lib import config as config_module
    monkeypatch.setattr(config_module, "load_config", lambda: {"wiki": {"auto_suggest": False}})

    handoff_state = storage.read_state(storage.handoff_path("test-handoff"))
    room_state = storage.read_state(storage.room_state_path("test-room"))

    _try_wiki_suggest_auto("test-handoff", handoff_state, room_state, "approve")

    captured = capsys.readouterr()
    assert captured.out == ""


def test_auto_suggest_exception_safe(monkeypatch, tmp_path, capsys):
    """wiki_suggest raises → _try_wiki_suggest_auto swallows exception silently."""
    base_dir = str(tmp_path)
    _setup_room_and_handoff(
        base_dir,
        handoff_id="test-handoff",
        handoff_status="completed",
        review_outcome="approved",
    )
    _patch_storage(monkeypatch, base_dir)

    # Make detect_continuity raise an unexpected error
    import lib.wiki_suggest as ws_module
    monkeypatch.setattr(ws_module, "detect_continuity", lambda *a, **kw: (_ for _ in ()).throw(RuntimeError("boom")))

    handoff_state = {
        "handoff": {"id": "test-handoff", "room_id": "test-room"},
        "review": {"outcome": "approved", "note": ""},
    }
    room_state = {"room": {"id": "test-room"}}

    # Should not raise
    _try_wiki_suggest_auto("test-handoff", handoff_state, room_state, "approve")

    captured = capsys.readouterr()
    assert captured.out == ""


# ---------------------------------------------------------------------------
# Group 7: Rework lineage chain walking (Bug 1 regression tests)
# ---------------------------------------------------------------------------

def _write_handoff(base_dir, handoff_id, room_id="test-room", status="completed",
                   review_outcome=None, review_note="", rework_of=None,
                   must_address=None, risks=None):
    """Write a minimal handoff YAML file directly."""
    state = {
        "handoff": {
            "id": handoff_id,
            "room_id": room_id,
            "from": "orchestrator",
            "to": "test-worker",
            "status": status,
            "priority": "medium",
            "kind": "implementation",
        },
        "task": {"description": f"task for {handoff_id}"},
        "timestamps": {"created_at": "2026-01-01T00:00:00Z"},
    }
    if rework_of:
        state["handoff"]["rework_of"] = rework_of
        state["rework"] = {"review_note": "", "must_address": must_address or []}
    if review_outcome:
        state["review"] = {
            "outcome": review_outcome,
            "reviewed_by": "test-reviewer",
            "reviewed_at": "2026-01-01T00:00:00Z",
            "note": review_note,
        }
    if risks:
        state["resolution"] = {"risks": risks}

    handoff_dir = os.path.join(base_dir, ".orchestrator", "handoffs")
    os.makedirs(handoff_dir, exist_ok=True)
    with open(os.path.join(handoff_dir, f"{handoff_id}.yaml"), "w") as f:
        yaml.dump(state, f)


def _setup_orch_dir(base_dir):
    """Create minimal .orchestrator structure for chain-walk tests."""
    orch = os.path.join(base_dir, ".orchestrator")
    os.makedirs(os.path.join(orch, "rooms", "test-room"), exist_ok=True)
    os.makedirs(os.path.join(orch, "rooms", "TEMPLATE"), exist_ok=True)
    os.makedirs(os.path.join(orch, "handoffs"), exist_ok=True)

    room_state = {
        "room": {"id": "test-room", "name": "Test", "status": "active"},
        "context": {"goal": "test"},
        "lifecycle": {"current_phase": "execution"},
    }
    with open(os.path.join(orch, "rooms", "test-room", "state.yaml"), "w") as f:
        yaml.dump(room_state, f)
    with open(os.path.join(orch, "rooms", "test-room", "log.md"), "w") as f:
        f.write("# Log\n")
    with open(os.path.join(orch, "rooms", "TEMPLATE", "state.yaml"), "w") as f:
        yaml.dump({"room": {"id": "TEMPLATE"}}, f)

    with open(os.path.join(orch, "peer_registry.yaml"), "w") as f:
        yaml.dump({"peers": []}, f)


def test_rework_chain_3_levels(monkeypatch, tmp_path):
    """3-step rework chain: source -> rework-1 -> rework-2.
    cycle_count must be 3, prior_handoffs must be exactly ['source', 'rework-1']."""
    base_dir = str(tmp_path)
    _setup_orch_dir(base_dir)
    _patch_storage(monkeypatch, base_dir)

    _write_handoff(base_dir, "source", room_id="test-room", status="completed",
                   review_outcome="approved")
    _write_handoff(base_dir, "rework-1", room_id="test-room", status="completed",
                   review_outcome="approved", rework_of="source")
    _write_handoff(base_dir, "rework-2", room_id="test-room", status="completed",
                   review_outcome="approved", rework_of="rework-1")

    result = detect_continuity("test-room", "rework-2")

    assert result["is_continuous"] is True
    assert result["continuity_reason"] == "rework_lineage"
    assert result["cycle_count"] == 3
    assert result["prior_handoffs"] == ["source", "rework-1"]  # oldest first


def test_rework_lineage_ignores_unrelated(monkeypatch, tmp_path):
    """Unrelated handoff in same room must NOT appear in rework lineage prior_handoffs."""
    base_dir = str(tmp_path)
    _setup_orch_dir(base_dir)
    _patch_storage(monkeypatch, base_dir)

    _write_handoff(base_dir, "source", room_id="test-room", status="completed",
                   review_outcome="approved")
    _write_handoff(base_dir, "unrelated", room_id="test-room", status="completed",
                   review_outcome="approved")
    _write_handoff(base_dir, "rework-1", room_id="test-room", status="completed",
                   review_outcome="approved", rework_of="source")

    result = detect_continuity("test-room", "rework-1")

    assert result["continuity_reason"] == "rework_lineage"
    assert result["prior_handoffs"] == ["source"]  # NOT ["source", "unrelated"]
    assert result["cycle_count"] == 2


def test_broken_lineage_failsoft(monkeypatch, tmp_path):
    """rework_of points to non-existent handoff. Should fail-soft: chain stops, partial result."""
    base_dir = str(tmp_path)
    _setup_orch_dir(base_dir)
    _patch_storage(monkeypatch, base_dir)

    # rework-1 with rework_of pointing to non-existent "ghost"
    _write_handoff(base_dir, "rework-1", room_id="test-room", status="completed",
                   review_outcome="approved", rework_of="ghost")

    result = detect_continuity("test-room", "rework-1")

    assert result["is_continuous"] is True
    assert result["continuity_reason"] == "rework_lineage"
    # ghost can't be loaded → chain stops before adding ghost, prior_handoffs = []
    assert result["prior_handoffs"] == []
    assert result["cycle_count"] == 1


# ---------------------------------------------------------------------------
# Group 8: Per-page dedupe (Bug 2 regression tests)
# ---------------------------------------------------------------------------

def test_decisions_dedupe(monkeypatch, tmp_path):
    """Decision deduped only when prior has stored fingerprint for that decision."""
    # Prior stored "use rest over grpc" as a generated decision hint
    prior_state = {
        "handoff": {"id": "h0", "room_id": "r1"},
        "review": {"outcome": "approved", "note": ""},
        "wiki_suggest": {
            "generated_hints": {
                "lessons": [], "deferred": [], "patterns": [], "current_state": [],
                "decisions": ["use rest over grpc"],  # was actually suggested
            }
        },
    }
    handoff_state = {
        "handoff": {"id": "h1", "room_id": "r1"},
        "review": {"outcome": "approved", "note": ""},
    }
    room_state = {
        "room": {"id": "r1"},
        "lifecycle": {"current_phase": "execution"},
        "discovery": {"decisions_made": ["Use REST over gRPC"]},
    }
    delta = build_wiki_delta(handoff_state, room_state, _make_continuity(), "approve", [prior_state])
    hints = [h["hint"] for h in delta["pages"]["decisions"]]
    assert "Use REST over gRPC" not in hints


def test_deferred_dedupe(monkeypatch, tmp_path):
    """Deferred hint deduped only when prior has stored fingerprint."""
    prior_state = {
        "handoff": {"id": "h0", "room_id": "r1"},
        "review": {"outcome": "approved", "note": ""},
        "resolution": {"risks": ["Token refresh deferred to v2"]},
        "wiki_suggest": {
            "generated_hints": {
                "lessons": [], "decisions": [], "patterns": [], "current_state": [],
                "deferred": ["token refresh deferred to v2"],  # normalized, was suggested
            }
        },
    }
    handoff_state = {
        "handoff": {"id": "h1", "room_id": "r1"},
        "review": {"outcome": "approved", "note": ""},
        "resolution": {"risks": ["Token refresh deferred to v2"]},
    }
    room_state = {"room": {"id": "r1"}, "lifecycle": {"current_phase": "execution"}}
    delta = build_wiki_delta(handoff_state, room_state, _make_continuity(), "approve", [prior_state])
    hints = [h["hint"] for h in delta["pages"]["deferred"]]
    assert "Token refresh deferred to v2" not in hints


def test_current_state_blocker_resolved_dedupe(monkeypatch, tmp_path):
    """Blocker resolved deduped only when prior has stored fingerprint."""
    prior_blocked = {
        "handoff": {"id": "h0", "room_id": "r1", "status": "blocked"},
        "review": {"outcome": "approved", "note": ""},
        "wiki_suggest": {
            "generated_hints": {
                "lessons": [], "decisions": [], "deferred": [], "patterns": [],
                "current_state": ["blocker resolved \u2014 room is no longer blocked."],
            }
        },
    }
    handoff_state = {
        "handoff": {"id": "h1", "room_id": "r1"},
        "review": {"outcome": "approved", "note": ""},
    }
    room_state = {"room": {"id": "r1"}, "lifecycle": {"current_phase": "execution"}}
    delta = build_wiki_delta(handoff_state, room_state, _make_continuity(), "approve", [prior_blocked])
    hints = [h["hint"] for h in delta["pages"]["current_state"]]
    assert "Blocker resolved — room is no longer blocked." not in hints


def test_patterns_dedupe_count_independent(monkeypatch, tmp_path):
    """Pattern deduped only when prior has stored pattern fingerprint."""
    prior_state = {
        "handoff": {"id": "h0", "room_id": "r1", "rework_of": "h-orig"},
        "review": {"outcome": "approved", "note": ""},
        "wiki_suggest": {
            "generated_hints": {
                "lessons": [], "decisions": [], "deferred": [], "current_state": [],
                "patterns": ["repeated rework pattern detected: cycles on this room."],  # digits stripped
            }
        },
    }
    handoff_state = {
        "handoff": {"id": "h1", "room_id": "r1", "rework_of": "h0"},
        "review": {"outcome": "approved", "note": ""},
    }
    room_state = {"room": {"id": "r1"}, "lifecycle": {"current_phase": "execution"}}
    continuity = {
        "is_continuous": True, "cycle_count": 4,
        "prior_handoffs": ["h-orig", "h0"], "continuity_reason": "rework_lineage",
    }
    delta = build_wiki_delta(handoff_state, room_state, continuity, "approve", [prior_state])
    hints = [h["hint"] for h in delta["pages"]["patterns"]]
    assert not any("Repeated rework pattern" in h for h in hints)


# ---------------------------------------------------------------------------
# Group 9: Fingerprint storage + new-decision / first-time surface bugs
# ---------------------------------------------------------------------------

def test_new_decision_not_deduped_without_fingerprint():
    """New decision in room should NOT be deduped when prior has no stored fingerprint."""
    prior_state = {
        "handoff": {"id": "h0", "room_id": "r1"},
        "review": {"outcome": "approved", "note": ""},
        # No wiki_suggest.generated_hints → no dedupe from this prior
    }
    handoff_state = {
        "handoff": {"id": "h1", "room_id": "r1"},
        "review": {"outcome": "approved", "note": ""},
    }
    room_state = {
        "room": {"id": "r1"},
        "lifecycle": {"current_phase": "execution"},
        "discovery": {"decisions_made": ["Brand new decision"]},
    }
    delta = build_wiki_delta(handoff_state, room_state, _make_continuity(), "approve", [prior_state])
    hints = [h["hint"] for h in delta["pages"]["decisions"]]
    assert "Brand new decision" in hints


def test_blocker_resolved_appears_first_time():
    """Blocker resolved should appear when prior has no stored current_state fingerprint."""
    # Prior was blocked but has no stored hints (legacy)
    prior_blocked = {
        "handoff": {"id": "h0", "room_id": "r1", "status": "blocked"},
        "review": {"outcome": "approved", "note": ""},
        # No wiki_suggest → no dedupe
    }
    handoff_state = {
        "handoff": {"id": "h1", "room_id": "r1"},
        "review": {"outcome": "approved", "note": ""},
    }
    room_state = {"room": {"id": "r1"}, "lifecycle": {"current_phase": "execution"}}
    delta = build_wiki_delta(handoff_state, room_state, _make_continuity(), "approve", [prior_blocked])
    hints = [h["hint"] for h in delta["pages"]["current_state"]]
    assert "Blocker resolved — room is no longer blocked." in hints


def test_pattern_appears_first_threshold():
    """Pattern hint should appear when cycle_count first reaches threshold and no stored fingerprint."""
    prior_state = {
        "handoff": {"id": "h0", "room_id": "r1", "rework_of": "h-orig"},
        "review": {"outcome": "approved", "note": ""},
        # No wiki_suggest → no dedupe
    }
    handoff_state = {
        "handoff": {"id": "h1", "room_id": "r1", "rework_of": "h0"},
        "review": {"outcome": "approved", "note": ""},
    }
    room_state = {"room": {"id": "r1"}, "lifecycle": {"current_phase": "execution"}}
    continuity = {
        "is_continuous": True, "cycle_count": 3,
        "prior_handoffs": ["h-orig", "h0"], "continuity_reason": "rework_lineage",
    }
    delta = build_wiki_delta(handoff_state, room_state, continuity, "approve", [prior_state])
    hints = [h["hint"] for h in delta["pages"]["patterns"]]
    assert any("Repeated rework pattern" in h for h in hints)


def test_manual_wiki_suggest_does_not_store_fingerprint(monkeypatch, tmp_path):
    """Manual orchctl handoff wiki-suggest should NOT write fingerprints to handoff state."""
    base_dir = str(tmp_path)
    _setup_orch_dir(base_dir)
    _patch_storage(monkeypatch, base_dir)

    _write_handoff(base_dir, "prior", room_id="test-room", status="completed",
                   review_outcome="approved", review_note="prior note")
    _write_handoff(base_dir, "current", room_id="test-room", status="completed",
                   review_outcome="approved", review_note="Important lesson learned")

    args = type('Args', (), {"handoff_id": "current"})()
    cmd_handoff_wiki_suggest(args)

    # Manual command must NOT store fingerprints
    state = storage.read_state(storage.handoff_path("current"))
    assert "wiki_suggest" not in state


def test_auto_hook_stores_fingerprint(monkeypatch, tmp_path):
    """Auto hook _try_wiki_suggest_auto DOES store fingerprints in handoff state."""
    base_dir = str(tmp_path)
    _setup_orch_dir(base_dir)
    _patch_storage(monkeypatch, base_dir)

    _write_handoff(base_dir, "prior", room_id="test-room", status="completed",
                   review_outcome="approved", review_note="prior note")
    _write_handoff(base_dir, "current", room_id="test-room", status="completed",
                   review_outcome="approved", review_note="Auto lesson")

    from lib import config as config_module
    monkeypatch.setattr(config_module, "load_config", lambda: {"wiki": {"auto_suggest": True}})

    handoff_state = storage.read_state(storage.handoff_path("current"))
    room_state = storage.read_state(storage.room_state_path("test-room"))

    _try_wiki_suggest_auto("current", handoff_state, room_state, "approve")

    # Auto hook MUST store fingerprints
    state = storage.read_state(storage.handoff_path("current"))
    assert "wiki_suggest" in state
    assert "generated_hints" in state["wiki_suggest"]
    assert "auto lesson" in state["wiki_suggest"]["generated_hints"]["lessons"]


def test_manual_preserves_existing_fingerprint(monkeypatch, tmp_path):
    """Manual wiki-suggest on handoff with existing fingerprint must NOT overwrite it."""
    base_dir = str(tmp_path)
    _setup_orch_dir(base_dir)
    _patch_storage(monkeypatch, base_dir)

    _write_handoff(base_dir, "prior", room_id="test-room", status="completed",
                   review_outcome="approved", review_note="prior note")
    _write_handoff(base_dir, "current", room_id="test-room", status="completed",
                   review_outcome="approved", review_note="Some note")

    # Pre-store a fingerprint (as if auto hook ran at approve time)
    path = storage.handoff_path("current")
    state = storage.read_state(path)
    state["wiki_suggest"] = {
        "generated_hints": {
            "lessons": ["original fingerprint"],
            "decisions": [], "deferred": [], "patterns": [], "current_state": [],
        }
    }
    storage.write_state(path, state)

    # Run manual command
    args = type('Args', (), {"handoff_id": "current"})()
    cmd_handoff_wiki_suggest(args)

    # Existing fingerprint must be preserved, not overwritten
    state = storage.read_state(path)
    assert state["wiki_suggest"]["generated_hints"]["lessons"] == ["original fingerprint"]
