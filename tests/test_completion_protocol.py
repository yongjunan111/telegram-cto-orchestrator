"""Regression tests for worker completion handoff instructions."""

from lib.bootstrap import _render_team_lead_protocol
from lib.handoffs import _build_verification


def test_bootstrap_requires_official_completion_handshake():
    text = _render_team_lead_protocol("orch-worker-a")

    assert "Official Completion Handshake" in text
    assert "not official state" in text
    assert "handoff claim" in text
    assert "handoff complete" in text
    assert "--by orch-worker-a" in text
    assert "claude-peers" in text
    assert "Do NOT stop until `handoff complete` succeeds" in text


def test_dispatch_brief_verification_requires_state_transition():
    text = _build_verification(
        ["criterion one"],
        [],
        ["run targeted tests"],
        handoff_id="handoff-1",
        peer_id="worker-1",
    )

    assert "Official completion steps required" in text
    assert "chat report" in text
    assert ".venv/bin/python orchctl handoff claim handoff-1 --by worker-1" in text
    assert ".venv/bin/python orchctl handoff complete handoff-1 --by worker-1" in text
    assert "--validation-cover" in text
    assert "--task-criterion-cover" in text
    assert "claude-peers" in text
