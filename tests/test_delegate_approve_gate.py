"""Tests for delegate_required approve gate (V1) and add-adversarial-review helper.

Hermetic: tmp dirs, monkeypatched storage paths, no real tmux, no network.
Calls cmd_handoff_approve / cmd_handoff_add_adversarial_review directly via
argparse.Namespace.
"""
import argparse
import os
import sys

import pytest
import yaml

_REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), os.pardir))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

from lib import storage  # noqa: E402
from lib.handoffs import (  # noqa: E402
    cmd_handoff_approve,
    cmd_handoff_add_adversarial_review,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture()
def tmp_root(tmp_path):
    """Set up a full .orchestrator skeleton and monkeypatch storage paths."""
    orc = tmp_path / ".orchestrator"
    rooms_dir = orc / "rooms" / "test-room"
    handoffs_dir = orc / "handoffs"
    runtime_dir = orc / "runtime"
    sessions_dir = runtime_dir / "sessions"
    adv_dir = runtime_dir / "adversarial-reviews"

    for d in (rooms_dir, handoffs_dir, sessions_dir, adv_dir):
        d.mkdir(parents=True)

    # Write room state
    room_state = {
        "room": {"id": "test-room", "status": "active"},
        "context": {"acceptance_criteria": []},
        "log": [],
    }
    with open(str(rooms_dir / "state.yaml"), "w") as f:
        yaml.dump(room_state, f)

    # Peer registry with cto (no team-lead), team-lead-peer (team-lead), worker-peer (no team-lead)
    peer_registry = {
        "peers": [
            {"id": "cto", "type": "reviewer", "capabilities": []},
            {"id": "team-lead-peer", "type": "worker", "capabilities": ["team-lead"]},
            {"id": "worker-peer", "type": "worker", "capabilities": []},
        ]
    }
    with open(str(orc / "peer_registry.yaml"), "w") as f:
        yaml.dump(peer_registry, f)

    # Monkeypatch storage paths
    storage.ORCHESTRATOR_DIR = str(orc)
    storage.ROOMS_DIR = str(orc / "rooms")
    storage.HANDOFFS_DIR = str(handoffs_dir)
    storage.PEER_REGISTRY_PATH = str(orc / "peer_registry.yaml")
    storage.RUNTIME_DIR = str(runtime_dir)
    storage.SESSIONS_DIR = str(sessions_dir)

    return tmp_path


def _handoff_path(tmp_root, handoff_id):
    return str(tmp_root / ".orchestrator" / "handoffs" / f"{handoff_id}.yaml")


def _write_handoff(tmp_root, handoff_id, *, mode="delegate_required",
                   to="team-lead-peer", completed_by="team-lead-peer",
                   completed_at="2026-04-26T10:00:00Z",
                   child_statuses=("completed",),
                   extra_children=None):
    """Write a completed delegate handoff with the given parameters."""
    children = []
    for i, st in enumerate(child_statuses):
        children.append({
            "id": f"sub-{i}",
            "model_target": "sonnet",
            "owned_files": ["lib/handoffs.py"],
            "status": st,
            "evidence": "done",
        })
    if extra_children:
        children.extend(extra_children)

    state = {
        "handoff": {
            "id": handoff_id,
            "room_id": "test-room",
            "from": "orchestrator",
            "to": to,
            "status": "completed",
            "kind": "implementation",
        },
        "task": {
            "description": "test task",
            "acceptance_criteria": [],
            "validation": [],
        },
        "execution": {
            "mode": mode,
            "child_handoffs": children,
        },
        "resolution": {
            "completed_by": completed_by,
            "validation_coverage": [],
            "acceptance_coverage": {},
        },
        "review": {},
        "timestamps": {
            "created_at": "2026-04-26T09:00:00Z",
            "claimed_at": "2026-04-26T09:30:00Z",
            "completed_at": completed_at,
        },
    }
    path = _handoff_path(tmp_root, handoff_id)
    with open(path, "w") as f:
        yaml.dump(state, f)
    return path


def _write_adversarial_review(tmp_root, handoff_id, *,
                               reviewer="cto",
                               verdict="ship-as-is",
                               review_target_completed_at="2026-04-26T10:00:00Z",
                               findings_count=0,
                               artifact_handoff_id=None,
                               filename="2026-04-26T11:00:00Z.md"):
    """Write an adversarial review artifact under runtime/adversarial-reviews/<handoff_id>/."""
    base = tmp_root / ".orchestrator" / "runtime" / "adversarial-reviews" / handoff_id
    base.mkdir(parents=True, exist_ok=True)
    fm = {
        "handoff_id": artifact_handoff_id if artifact_handoff_id is not None else handoff_id,
        "reviewer": reviewer,
        "review_target_completed_at": review_target_completed_at,
        "verdict": verdict,
        "findings_count": findings_count,
    }
    fm_yaml = yaml.safe_dump(fm, default_flow_style=False, sort_keys=True, allow_unicode=True)
    md_text = f"---\n{fm_yaml}---\n\nReview body.\n"
    path = base / filename
    with open(str(path), "w") as f:
        f.write(md_text)
    return str(path)


def _approve(handoff_id, reviewer="cto", note=None):
    args = argparse.Namespace(handoff_id=handoff_id, by=reviewer, note=note)
    return cmd_handoff_approve(args)


# ---------------------------------------------------------------------------
# T1: missing team-lead capability
# ---------------------------------------------------------------------------

def test_t1_missing_team_lead_capability(tmp_root):
    """Assignee is worker-peer (no team-lead capability) → fail."""
    _write_handoff(tmp_root, "h-t1", to="worker-peer", completed_by="worker-peer")
    _write_adversarial_review(tmp_root, "h-t1")
    with pytest.raises(SystemExit) as exc:
        _approve("h-t1")
    assert exc.value.code == 1


# ---------------------------------------------------------------------------
# T2a: handoff.to == cto
# ---------------------------------------------------------------------------

def test_t2a_assignee_is_cto(tmp_root):
    """handoff.to == cto → fail with 'missing: assignee'."""
    _write_handoff(tmp_root, "h-t2a", to="cto", completed_by="team-lead-peer")
    _write_adversarial_review(tmp_root, "h-t2a")
    with pytest.raises(SystemExit) as exc:
        _approve("h-t2a")
    assert exc.value.code == 1


# ---------------------------------------------------------------------------
# T2b: resolution.completed_by == cto
# ---------------------------------------------------------------------------

def test_t2b_completer_is_cto(tmp_root):
    """resolution.completed_by == cto → fail with 'missing: completer'."""
    _write_handoff(tmp_root, "h-t2b", to="team-lead-peer", completed_by="cto")
    _write_adversarial_review(tmp_root, "h-t2b")
    with pytest.raises(SystemExit) as exc:
        _approve("h-t2b")
    assert exc.value.code == 1


# ---------------------------------------------------------------------------
# T3: 0 completed child_handoffs
# ---------------------------------------------------------------------------

def test_t3_no_completed_children(tmp_root):
    """No completed child → fail with 'missing: completed child ledger'."""
    _write_handoff(tmp_root, "h-t3", child_statuses=("failed",))
    _write_adversarial_review(tmp_root, "h-t3")
    with pytest.raises(SystemExit) as exc:
        _approve("h-t3")
    assert exc.value.code == 1


# ---------------------------------------------------------------------------
# T4a: no review artifact at all
# ---------------------------------------------------------------------------

def test_t4a_no_review_artifact(tmp_root):
    """No artifact → fail with 'missing: adversarial review'."""
    _write_handoff(tmp_root, "h-t4a")
    with pytest.raises(SystemExit) as exc:
        _approve("h-t4a")
    assert exc.value.code == 1


# ---------------------------------------------------------------------------
# T4b: artifact exists but review_target_completed_at != completed_at
# ---------------------------------------------------------------------------

def test_t4b_stale_completed_at(tmp_root):
    """Artifact completed_at mismatch → fail."""
    _write_handoff(tmp_root, "h-t4b", completed_at="2026-04-26T10:00:00Z")
    _write_adversarial_review(tmp_root, "h-t4b",
                               review_target_completed_at="2026-04-25T10:00:00Z")
    with pytest.raises(SystemExit) as exc:
        _approve("h-t4b")
    assert exc.value.code == 1


# ---------------------------------------------------------------------------
# T4c: reviewer == assignee
# ---------------------------------------------------------------------------

def test_t4c_reviewer_is_assignee(tmp_root):
    """reviewer == assignee → fail."""
    _write_handoff(tmp_root, "h-t4c", to="team-lead-peer", completed_by="worker-peer")
    _write_adversarial_review(tmp_root, "h-t4c", reviewer="team-lead-peer")
    with pytest.raises(SystemExit) as exc:
        _approve("h-t4c")
    assert exc.value.code == 1


# ---------------------------------------------------------------------------
# T4d: reviewer == completer
# ---------------------------------------------------------------------------

def test_t4d_reviewer_is_completer(tmp_root):
    """reviewer == completer → fail (even if completer != assignee)."""
    # Use a second team-lead capable peer as completer
    # Add extra peer to registry
    peer_reg_path = str(tmp_root / ".orchestrator" / "peer_registry.yaml")
    with open(peer_reg_path) as f:
        reg = yaml.safe_load(f)
    reg["peers"].append({"id": "team-lead-2", "type": "worker", "capabilities": ["team-lead"]})
    with open(peer_reg_path, "w") as f:
        yaml.dump(reg, f)

    _write_handoff(tmp_root, "h-t4d", to="team-lead-peer", completed_by="team-lead-2")
    _write_adversarial_review(tmp_root, "h-t4d", reviewer="team-lead-2")
    with pytest.raises(SystemExit) as exc:
        _approve("h-t4d")
    assert exc.value.code == 1


# ---------------------------------------------------------------------------
# T4e: artifact handoff_id mismatches
# ---------------------------------------------------------------------------

def test_t4e_handoff_id_mismatch(tmp_root):
    """Artifact handoff_id != current handoff → fail."""
    _write_handoff(tmp_root, "h-t4e")
    _write_adversarial_review(tmp_root, "h-t4e", artifact_handoff_id="other-handoff")
    with pytest.raises(SystemExit) as exc:
        _approve("h-t4e")
    assert exc.value.code == 1


# ---------------------------------------------------------------------------
# T5a-d: verdict variations
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("bad_verdict,slug", [
    ("needs-fix", "h-t5-needsfix"),
    ("reject", "h-t5-reject"),
    ("", "h-t5-empty"),
    ("unknown-string", "h-t5-unknown"),
])
def test_t5_bad_verdict(tmp_root, bad_verdict, slug):
    """Non-ship-as-is verdict → fail."""
    hid = slug
    _write_handoff(tmp_root, hid)
    _write_adversarial_review(tmp_root, hid, verdict=bad_verdict)
    with pytest.raises(SystemExit) as exc:
        _approve(hid)
    assert exc.value.code == 1


# ---------------------------------------------------------------------------
# T6: all evidence present → SUCCESS
# ---------------------------------------------------------------------------

def test_t6_all_evidence_present(tmp_root):
    """All conditions met → approved successfully."""
    _write_handoff(tmp_root, "h-t6",
                   to="team-lead-peer",
                   completed_by="team-lead-peer",
                   completed_at="2026-04-26T10:00:00Z")
    _write_adversarial_review(tmp_root, "h-t6",
                               reviewer="cto",
                               verdict="ship-as-is",
                               review_target_completed_at="2026-04-26T10:00:00Z")
    # Should not raise
    _approve("h-t6")
    # Verify state was written
    state = storage.read_state(_handoff_path(tmp_root, "h-t6"))
    assert state.get("review", {}).get("outcome") == "approved"
    assert state["review"]["reviewed_by"] == "cto"


# ---------------------------------------------------------------------------
# T7: non-delegate handoffs not blocked
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("mode,slug", [
    ("direct", "h-t7-direct"),
    ("delegate_optional", "h-t7-delopt"),
    (None, "h-t7-missing"),
])
def test_t7_non_delegate_not_blocked(tmp_root, mode, slug):
    """direct / delegate_optional / missing mode → gate does not run."""
    hid = slug
    _write_handoff(tmp_root, hid,
                   mode=mode,
                   to="worker-peer",   # no team-lead cap; gate must NOT fire
                   completed_by="worker-peer")
    # No review artifact; gate must NOT fire for non-delegate
    # Should succeed (no adversarial review needed)
    _approve(hid)
    state = storage.read_state(_handoff_path(tmp_root, hid))
    assert state.get("review", {}).get("outcome") == "approved"


# ---------------------------------------------------------------------------
# Helper CLI smoke: cmd_handoff_add_adversarial_review
# ---------------------------------------------------------------------------

def test_add_adversarial_review_smoke(tmp_root, tmp_path):
    """Writing review artifact produces valid frontmatter."""
    _write_handoff(tmp_root, "h-smoke",
                   to="team-lead-peer",
                   completed_by="team-lead-peer",
                   completed_at="2026-04-26T10:00:00Z")

    body_file = str(tmp_path / "body.md")
    with open(body_file, "w") as f:
        f.write("# Review\nLooks good.\n")

    args = argparse.Namespace(
        handoff_id="h-smoke",
        reviewer="cto",
        verdict="ship-as-is",
        findings_count="0",
        body_file=body_file,
    )
    cmd_handoff_add_adversarial_review(args)

    # Verify artifact exists and frontmatter is valid
    adv_dir = tmp_root / ".orchestrator" / "runtime" / "adversarial-reviews" / "h-smoke"
    artifacts = list(adv_dir.glob("*.md"))
    assert len(artifacts) == 1
    content = artifacts[0].read_text()
    assert content.startswith("---\n")
    # Parse frontmatter
    end = content.find("\n---\n", 4)
    fm = yaml.safe_load(content[4:end])
    assert fm["handoff_id"] == "h-smoke"
    assert fm["reviewer"] == "cto"
    assert fm["verdict"] == "ship-as-is"
    assert fm["review_target_completed_at"] == "2026-04-26T10:00:00Z"
    assert fm["findings_count"] == 0


def test_add_adversarial_review_no_completed_at(tmp_root, tmp_path):
    """Handoff without completed_at → error, no file written."""
    # Write an open (not completed) handoff
    state = {
        "handoff": {
            "id": "h-open",
            "room_id": "test-room",
            "from": "orchestrator",
            "to": "team-lead-peer",
            "status": "open",
            "kind": "implementation",
        },
        "task": {"description": "x", "acceptance_criteria": [], "validation": []},
        "execution": {"mode": "delegate_required", "child_handoffs": []},
        "resolution": {},
        "review": {},
        "timestamps": {"created_at": "2026-04-26T09:00:00Z", "claimed_at": None, "completed_at": None},
    }
    path = _handoff_path(tmp_root, "h-open")
    with open(path, "w") as f:
        yaml.dump(state, f)

    body_file = str(tmp_path / "body.md")
    with open(body_file, "w") as f:
        f.write("body\n")

    args = argparse.Namespace(
        handoff_id="h-open",
        reviewer="cto",
        verdict="ship-as-is",
        findings_count="0",
        body_file=body_file,
    )
    with pytest.raises(SystemExit) as exc:
        cmd_handoff_add_adversarial_review(args)
    assert exc.value.code == 1


def test_add_adversarial_review_bad_findings_count(tmp_root, tmp_path):
    """Non-integer findings_count → error."""
    _write_handoff(tmp_root, "h-bfc",
                   to="team-lead-peer",
                   completed_by="team-lead-peer",
                   completed_at="2026-04-26T10:00:00Z")

    body_file = str(tmp_path / "body.md")
    with open(body_file, "w") as f:
        f.write("body\n")

    args = argparse.Namespace(
        handoff_id="h-bfc",
        reviewer="cto",
        verdict="ship-as-is",
        findings_count="notanint",
        body_file=body_file,
    )
    with pytest.raises(SystemExit) as exc:
        cmd_handoff_add_adversarial_review(args)
    assert exc.value.code == 1


# ---------------------------------------------------------------------------
# F1 — empty child evidence bypass
# ---------------------------------------------------------------------------

def test_f1_empty_owned_files_blocks_approve(tmp_root, capsys):
    """Completed child with owned_files=[] → approve must fail (F1 bypass pre-fix passes)."""
    _write_handoff(tmp_root, "h-f1a",
                   to="team-lead-peer",
                   completed_by="team-lead-peer",
                   completed_at="2026-04-26T10:00:00Z",
                   child_statuses=(),
                   extra_children=[{
                       "id": "sub-empty-owned",
                       "model_target": "sonnet",
                       "owned_files": [],
                       "status": "completed",
                       "evidence": "lots of text evidence",
                   }])
    _write_adversarial_review(tmp_root, "h-f1a",
                               reviewer="cto",
                               verdict="ship-as-is",
                               review_target_completed_at="2026-04-26T10:00:00Z")
    with pytest.raises(SystemExit) as exc:
        _approve("h-f1a")
    assert exc.value.code == 1
    captured = capsys.readouterr()
    assert "missing: completed child evidence" in captured.err
    assert "sub-empty-owned" in captured.err


def test_f1_blank_evidence_blocks_approve(tmp_root, capsys):
    """Completed child with blank evidence → approve must fail."""
    _write_handoff(tmp_root, "h-f1b",
                   to="team-lead-peer",
                   completed_by="team-lead-peer",
                   completed_at="2026-04-26T10:00:00Z",
                   child_statuses=(),
                   extra_children=[{
                       "id": "sub-blank-ev",
                       "model_target": "sonnet",
                       "owned_files": ["lib/handoffs.py"],
                       "status": "completed",
                       "evidence": "   ",
                   }])
    _write_adversarial_review(tmp_root, "h-f1b",
                               reviewer="cto",
                               verdict="ship-as-is",
                               review_target_completed_at="2026-04-26T10:00:00Z")
    with pytest.raises(SystemExit) as exc:
        _approve("h-f1b")
    assert exc.value.code == 1
    captured = capsys.readouterr()
    assert "missing: completed child evidence" in captured.err
    assert "sub-blank-ev" in captured.err


# ---------------------------------------------------------------------------
# F2a — helper rejects non-peer reviewer
# ---------------------------------------------------------------------------

def test_f2a_helper_rejects_non_peer_reviewer(tmp_root, tmp_path, capsys):
    """add-adversarial-review with --reviewer not in peer_registry → exit 1."""
    _write_handoff(tmp_root, "h-f2a",
                   to="team-lead-peer",
                   completed_by="team-lead-peer",
                   completed_at="2026-04-26T10:00:00Z")

    body_file = str(tmp_path / "body.md")
    with open(body_file, "w") as f:
        f.write("review body\n")

    args = argparse.Namespace(
        handoff_id="h-f2a",
        reviewer="not-a-peer",
        verdict="ship-as-is",
        findings_count="0",
        body_file=body_file,
    )
    with pytest.raises(SystemExit) as exc:
        cmd_handoff_add_adversarial_review(args)
    assert exc.value.code == 1
    captured = capsys.readouterr()
    assert "not-a-peer" in captured.err or "peer" in captured.err.lower()

    # Also confirm no artifact was written
    adv_dir = tmp_root / ".orchestrator" / "runtime" / "adversarial-reviews" / "h-f2a"
    artifacts = list(adv_dir.glob("*.md")) if adv_dir.exists() else []
    assert len(artifacts) == 0


# ---------------------------------------------------------------------------
# F2b — gate rejects artifact with non-peer reviewer
# ---------------------------------------------------------------------------

def test_f2b_gate_rejects_artifact_with_non_peer_reviewer(tmp_root, capsys):
    """Hand-crafted artifact with reviewer not in peer_registry → approve must fail."""
    _write_handoff(tmp_root, "h-f2b",
                   to="team-lead-peer",
                   completed_by="team-lead-peer",
                   completed_at="2026-04-26T10:00:00Z")
    # Hand-write artifact with bogus reviewer bypassing helper validation
    _write_adversarial_review(tmp_root, "h-f2b",
                               reviewer="not-a-peer",
                               verdict="ship-as-is",
                               review_target_completed_at="2026-04-26T10:00:00Z")
    with pytest.raises(SystemExit) as exc:
        _approve("h-f2b")
    assert exc.value.code == 1
    captured = capsys.readouterr()
    assert "adversarial review" in captured.err.lower()


# ---------------------------------------------------------------------------
# F3 — symlinked per-handoff dir
# ---------------------------------------------------------------------------

def test_f3_symlinked_per_handoff_dir_blocks_approve(tmp_root, tmp_path, capsys):
    """Per-handoff dir is a symlink → approve must fail."""
    _write_handoff(tmp_root, "h-f3dir",
                   to="team-lead-peer",
                   completed_by="team-lead-peer",
                   completed_at="2026-04-26T10:00:00Z")

    # Create a real dir in tmp_path with a valid artifact inside
    real_dir = tmp_path / "escape-dir"
    real_dir.mkdir()
    import yaml as _yaml
    fm = {
        "handoff_id": "h-f3dir",
        "reviewer": "cto",
        "review_target_completed_at": "2026-04-26T10:00:00Z",
        "verdict": "ship-as-is",
        "findings_count": 0,
    }
    fm_yaml = _yaml.safe_dump(fm, default_flow_style=False, sort_keys=True)
    md_text = f"---\n{fm_yaml}---\n\nReview body.\n"
    (real_dir / "2026-04-26T11:00:00Z.md").write_text(md_text)

    # Plant the per-handoff dir as a symlink to real_dir
    adv_base = tmp_root / ".orchestrator" / "runtime" / "adversarial-reviews"
    adv_base.mkdir(parents=True, exist_ok=True)
    symlink_path = adv_base / "h-f3dir"
    symlink_path.symlink_to(real_dir)

    with pytest.raises(SystemExit) as exc:
        _approve("h-f3dir")
    assert exc.value.code == 1
    captured = capsys.readouterr()
    assert "adversarial review" in captured.err.lower()


def test_f3_symlinked_md_file_blocks_approve(tmp_root, tmp_path, capsys):
    """Symlinked .md artifact inside per-handoff dir → approve must fail."""
    _write_handoff(tmp_root, "h-f3md",
                   to="team-lead-peer",
                   completed_by="team-lead-peer",
                   completed_at="2026-04-26T10:00:00Z")

    # Create a real md file outside the reviews tree
    import yaml as _yaml
    fm = {
        "handoff_id": "h-f3md",
        "reviewer": "cto",
        "review_target_completed_at": "2026-04-26T10:00:00Z",
        "verdict": "ship-as-is",
        "findings_count": 0,
    }
    fm_yaml = _yaml.safe_dump(fm, default_flow_style=False, sort_keys=True)
    md_text = f"---\n{fm_yaml}---\n\nReview body.\n"
    real_md = tmp_path / "real-review.md"
    real_md.write_text(md_text)

    # Create the per-handoff dir properly, then plant a symlink to the real md
    adv_base = tmp_root / ".orchestrator" / "runtime" / "adversarial-reviews"
    per_hid = adv_base / "h-f3md"
    per_hid.mkdir(parents=True)
    symlink_md = per_hid / "2026-04-26T11:00:00Z.md"
    symlink_md.symlink_to(real_md)

    with pytest.raises(SystemExit) as exc:
        _approve("h-f3md")
    assert exc.value.code == 1
    captured = capsys.readouterr()
    assert "adversarial review" in captured.err.lower()


# ---------------------------------------------------------------------------
# F4 — same-second overwrite
# ---------------------------------------------------------------------------

def test_f4_same_second_helper_writes_no_overwrite(tmp_root, tmp_path, monkeypatch):
    """Two helper writes in same second must produce two distinct files (no overwrite)."""
    _write_handoff(tmp_root, "h-f4",
                   to="team-lead-peer",
                   completed_by="team-lead-peer",
                   completed_at="2026-04-26T10:00:00Z")

    # Monkeypatch storage.now_iso to always return the same second-precision string
    from lib import storage as _storage
    monkeypatch.setattr(_storage, "now_iso", lambda: "2026-04-26T11:00:00Z")

    body_file1 = str(tmp_path / "body1.md")
    body_file2 = str(tmp_path / "body2.md")
    with open(body_file1, "w") as f:
        f.write("first review body\n")
    with open(body_file2, "w") as f:
        f.write("second review body\n")

    args1 = argparse.Namespace(
        handoff_id="h-f4",
        reviewer="cto",
        verdict="ship-as-is",
        findings_count="0",
        body_file=body_file1,
    )
    args2 = argparse.Namespace(
        handoff_id="h-f4",
        reviewer="cto",
        verdict="ship-as-is",
        findings_count="1",
        body_file=body_file2,
    )

    cmd_handoff_add_adversarial_review(args1)
    cmd_handoff_add_adversarial_review(args2)

    adv_dir = tmp_root / ".orchestrator" / "runtime" / "adversarial-reviews" / "h-f4"
    artifacts = sorted(adv_dir.glob("*.md"))
    # Post-fix: must have 2 distinct files
    assert len(artifacts) == 2, f"Expected 2 artifacts, got {len(artifacts)}: {[a.name for a in artifacts]}"
    # First artifact must preserve original content (not overwritten)
    contents = [a.read_text() for a in artifacts]
    assert contents[0] != contents[1], "Both artifacts have same content — first was overwritten"


# ---------------------------------------------------------------------------
# NEW F1 — dirfd symlink hardening for _write_adversarial_review_atomic
# ---------------------------------------------------------------------------

def _make_body_file(tmp_path, name="body.md", text="review body\n"):
    p = tmp_path / name
    p.write_text(text)
    return str(p)


def _add_review_args(handoff_id, body_file, reviewer="cto"):
    return argparse.Namespace(
        handoff_id=handoff_id,
        reviewer=reviewer,
        verdict="ship-as-is",
        findings_count="0",
        body_file=body_file,
    )


def test_f1_dirfd_adversarial_reviews_root_symlinked_blocks(tmp_root, tmp_path):
    """runtime/adversarial-reviews is a symlink → helper exits 1; outside dir unchanged."""
    _write_handoff(tmp_root, "h-nf1a",
                   to="team-lead-peer", completed_by="team-lead-peer",
                   completed_at="2026-04-26T10:00:00Z")

    outside = tmp_path / "outside"
    outside.mkdir()

    # Remove the legitimate adversarial-reviews dir, plant symlink in its place
    adv_reviews = tmp_root / ".orchestrator" / "runtime" / "adversarial-reviews"
    import shutil
    shutil.rmtree(str(adv_reviews))
    adv_reviews.symlink_to(outside)

    body_file = _make_body_file(tmp_path)
    args = _add_review_args("h-nf1a", body_file)
    with pytest.raises(SystemExit) as exc:
        cmd_handoff_add_adversarial_review(args)
    assert exc.value.code == 1

    # Nothing written in outside dir
    assert list(outside.iterdir()) == []


def test_f1_dirfd_per_handoff_dir_symlinked_blocks(tmp_root, tmp_path):
    """runtime/adversarial-reviews/<hid> is a symlink → helper exits 1; outside empty."""
    _write_handoff(tmp_root, "h-nf1b",
                   to="team-lead-peer", completed_by="team-lead-peer",
                   completed_at="2026-04-26T10:00:00Z")

    outside = tmp_path / "outside-hid"
    outside.mkdir()

    adv_reviews = tmp_root / ".orchestrator" / "runtime" / "adversarial-reviews"
    adv_reviews.mkdir(parents=True, exist_ok=True)
    per_hid = adv_reviews / "h-nf1b"
    per_hid.symlink_to(outside)

    body_file = _make_body_file(tmp_path)
    args = _add_review_args("h-nf1b", body_file)
    with pytest.raises(SystemExit) as exc:
        cmd_handoff_add_adversarial_review(args)
    assert exc.value.code == 1

    assert list(outside.iterdir()) == []


def test_f1_dirfd_md_path_symlinked_blocks(tmp_root, tmp_path, monkeypatch):
    """runtime/adversarial-reviews/<hid>/<ts>.md is a pre-planted symlink → helper exits 1; target unchanged."""
    from lib import storage as _storage
    monkeypatch.setattr(_storage, "now_iso", lambda: "2026-04-26T12:00:00Z")

    _write_handoff(tmp_root, "h-nf1c",
                   to="team-lead-peer", completed_by="team-lead-peer",
                   completed_at="2026-04-26T10:00:00Z")

    adv_reviews = tmp_root / ".orchestrator" / "runtime" / "adversarial-reviews"
    per_hid = adv_reviews / "h-nf1c"
    per_hid.mkdir(parents=True, exist_ok=True)

    # Plant a symlink at the exact filename the helper would create
    outside_file = tmp_path / "target.md"
    outside_file.write_text("original\n")
    (per_hid / "2026-04-26T12:00:00Z.md").symlink_to(outside_file)

    body_file = _make_body_file(tmp_path)
    args = _add_review_args("h-nf1c", body_file)
    with pytest.raises(SystemExit) as exc:
        cmd_handoff_add_adversarial_review(args)
    assert exc.value.code == 1

    # The symlink target must not have been modified
    assert outside_file.read_text() == "original\n"


def test_f1_preserved_same_second_writes_distinct(tmp_root, tmp_path, monkeypatch):
    """Same-second monkeypatched now_iso → 2 helper calls produce 2 distinct files."""
    from lib import storage as _storage
    monkeypatch.setattr(_storage, "now_iso", lambda: "2026-04-26T13:00:00Z")

    _write_handoff(tmp_root, "h-nf1d",
                   to="team-lead-peer", completed_by="team-lead-peer",
                   completed_at="2026-04-26T10:00:00Z")

    cmd_handoff_add_adversarial_review(_add_review_args("h-nf1d", _make_body_file(tmp_path, "b1.md", "body one\n")))
    cmd_handoff_add_adversarial_review(_add_review_args("h-nf1d", _make_body_file(tmp_path, "b2.md", "body two\n")))

    adv_dir = tmp_root / ".orchestrator" / "runtime" / "adversarial-reviews" / "h-nf1d"
    artifacts = sorted(adv_dir.glob("*.md"))
    assert len(artifacts) == 2
    contents = [a.read_text() for a in artifacts]
    assert contents[0] != contents[1]


def test_f1_preserved_normal_path_writes(tmp_root, tmp_path):
    """Clean path → helper writes one valid artifact with correct frontmatter."""
    _write_handoff(tmp_root, "h-nf1e",
                   to="team-lead-peer", completed_by="team-lead-peer",
                   completed_at="2026-04-26T10:00:00Z")

    cmd_handoff_add_adversarial_review(_add_review_args("h-nf1e", _make_body_file(tmp_path, "b.md", "body\n")))

    adv_dir = tmp_root / ".orchestrator" / "runtime" / "adversarial-reviews" / "h-nf1e"
    artifacts = list(adv_dir.glob("*.md"))
    assert len(artifacts) == 1
    content = artifacts[0].read_text()
    assert content.startswith("---\n")
    end = content.find("\n---\n", 4)
    fm = yaml.safe_load(content[4:end])
    assert fm["handoff_id"] == "h-nf1e"
    assert fm["verdict"] == "ship-as-is"


def test_f1_helper_succeeds_under_symlinked_ancestor(tmp_root, tmp_path):
    """runtime dir is under a symlinked ancestor → helper must still write successfully."""
    real_rt = tmp_path / "real-runtime"
    real_rt.mkdir()
    sym_rt = tmp_path / "sym-runtime"
    sym_rt.symlink_to(real_rt)

    # Redirect storage.RUNTIME_DIR to the symlinked path
    from lib import storage as _storage
    original_runtime = _storage.RUNTIME_DIR
    _storage.RUNTIME_DIR = str(sym_rt)
    try:
        _write_handoff(tmp_root, "h-nf1f",
                       to="team-lead-peer", completed_by="team-lead-peer",
                       completed_at="2026-04-26T10:00:00Z")

        cmd_handoff_add_adversarial_review(_add_review_args("h-nf1f", _make_body_file(tmp_path, "bsym.md", "body\n")))

        adv_dir = real_rt / "adversarial-reviews" / "h-nf1f"
        artifacts = list(adv_dir.glob("*.md"))
        assert len(artifacts) == 1
    finally:
        _storage.RUNTIME_DIR = original_runtime


# ---------------------------------------------------------------------------
# NEW F2 — mtime > completed_at strict UTC check
# ---------------------------------------------------------------------------

def _write_adv_with_mtime(tmp_root, handoff_id, mtime_epoch, reviewer="cto",
                            verdict="ship-as-is",
                            completed_at="2026-04-26T10:00:00Z"):
    """Write artifact via _write_adversarial_review helper then set its mtime."""
    path = _write_adversarial_review(tmp_root, handoff_id,
                                     reviewer=reviewer,
                                     verdict=verdict,
                                     review_target_completed_at=completed_at)
    os.utime(path, (mtime_epoch, mtime_epoch))
    return path


def test_f2_stale_artifact_mtime_blocks_approve(tmp_root, capsys):
    """Artifact mtime 1h BEFORE completed_at → approve blocks."""
    from datetime import datetime, timezone
    completed_at = "2026-04-26T10:00:00Z"
    epoch = datetime.strptime(completed_at, "%Y-%m-%dT%H:%M:%SZ").replace(
        tzinfo=timezone.utc).timestamp()

    _write_handoff(tmp_root, "h-nf2a",
                   to="team-lead-peer", completed_by="team-lead-peer",
                   completed_at=completed_at)
    _write_adv_with_mtime(tmp_root, "h-nf2a", epoch - 3600)

    with pytest.raises(SystemExit) as exc:
        _approve("h-nf2a")
    assert exc.value.code == 1


def test_f2_artifact_mtime_equal_completed_at_blocks_approve(tmp_root, capsys):
    """Artifact mtime EXACTLY == completed_at epoch → approve blocks (strict >)."""
    from datetime import datetime, timezone
    completed_at = "2026-04-26T10:00:00Z"
    epoch = datetime.strptime(completed_at, "%Y-%m-%dT%H:%M:%SZ").replace(
        tzinfo=timezone.utc).timestamp()

    _write_handoff(tmp_root, "h-nf2b",
                   to="team-lead-peer", completed_by="team-lead-peer",
                   completed_at=completed_at)
    _write_adv_with_mtime(tmp_root, "h-nf2b", epoch)  # exactly equal

    with pytest.raises(SystemExit) as exc:
        _approve("h-nf2b")
    assert exc.value.code == 1


def test_f2_fresh_artifact_mtime_passes_approve(tmp_root):
    """Artifact mtime == completed_at + 1s → approve passes."""
    from datetime import datetime, timezone
    completed_at = "2026-04-26T10:00:00Z"
    epoch = datetime.strptime(completed_at, "%Y-%m-%dT%H:%M:%SZ").replace(
        tzinfo=timezone.utc).timestamp()

    _write_handoff(tmp_root, "h-nf2c",
                   to="team-lead-peer", completed_by="team-lead-peer",
                   completed_at=completed_at)
    _write_adv_with_mtime(tmp_root, "h-nf2c", epoch + 1)

    # Should not raise
    _approve("h-nf2c")
    state = storage.read_state(_handoff_path(tmp_root, "h-nf2c"))
    assert state["review"]["outcome"] == "approved"


def test_f2_malformed_completed_at_fail_closed_no_crash(tmp_root, capsys):
    """Handoff with completed_at='not-a-date' → approve blocks without unhandled exception."""
    _write_handoff(tmp_root, "h-nf2d",
                   to="team-lead-peer", completed_by="team-lead-peer",
                   completed_at="not-a-date")
    _write_adversarial_review(tmp_root, "h-nf2d",
                               reviewer="cto",
                               verdict="ship-as-is",
                               review_target_completed_at="not-a-date")

    with pytest.raises(SystemExit) as exc:
        _approve("h-nf2d")
    assert exc.value.code == 1
    # Must not have raised any unhandled exception (only SystemExit)


# ---------------------------------------------------------------------------
# NEW F3 — strict non-empty string validation of owned_files entries
# ---------------------------------------------------------------------------

def _handoff_with_owned_files(tmp_root, handoff_id, owned_files):
    """Write a completed handoff with a single completed child using custom owned_files."""
    _write_handoff(tmp_root, handoff_id,
                   to="team-lead-peer",
                   completed_by="team-lead-peer",
                   completed_at="2026-04-26T10:00:00Z",
                   child_statuses=(),
                   extra_children=[{
                       "id": "sub-0",
                       "model_target": "sonnet",
                       "owned_files": owned_files,
                       "status": "completed",
                       "evidence": "has evidence",
                   }])
    _write_adversarial_review(tmp_root, handoff_id,
                               reviewer="cto",
                               verdict="ship-as-is",
                               review_target_completed_at="2026-04-26T10:00:00Z",
                               filename="2026-04-26T11:00:01Z.md")


def test_f3_empty_string_owned_files_entry_blocks(tmp_root, capsys):
    """owned_files=[""] → approve must block."""
    _handoff_with_owned_files(tmp_root, "h-nf3a", [""])
    with pytest.raises(SystemExit) as exc:
        _approve("h-nf3a")
    assert exc.value.code == 1
    assert "missing: completed child evidence" in capsys.readouterr().err


def test_f3_whitespace_owned_files_entry_blocks(tmp_root, capsys):
    """owned_files=["   "] → approve must block."""
    _handoff_with_owned_files(tmp_root, "h-nf3b", ["   "])
    with pytest.raises(SystemExit) as exc:
        _approve("h-nf3b")
    assert exc.value.code == 1
    assert "missing: completed child evidence" in capsys.readouterr().err


def test_f3_none_owned_files_entry_blocks(tmp_root, capsys):
    """owned_files=[None] → approve must block."""
    _handoff_with_owned_files(tmp_root, "h-nf3c", [None])
    with pytest.raises(SystemExit) as exc:
        _approve("h-nf3c")
    assert exc.value.code == 1
    assert "missing: completed child evidence" in capsys.readouterr().err


def test_f3_int_owned_files_entry_blocks(tmp_root, capsys):
    """owned_files=[123] → approve must block."""
    _handoff_with_owned_files(tmp_root, "h-nf3d", [123])
    with pytest.raises(SystemExit) as exc:
        _approve("h-nf3d")
    assert exc.value.code == 1
    assert "missing: completed child evidence" in capsys.readouterr().err


def test_f3_mixed_valid_invalid_owned_files_blocks(tmp_root, capsys):
    """owned_files=["lib/foo.py", ""] → any-invalid-fails-closed → approve must block."""
    _handoff_with_owned_files(tmp_root, "h-nf3e", ["lib/foo.py", ""])
    with pytest.raises(SystemExit) as exc:
        _approve("h-nf3e")
    assert exc.value.code == 1
    assert "missing: completed child evidence" in capsys.readouterr().err


# ---------------------------------------------------------------------------
# Publication-state safety (rework-3): temp+link atomic write
# ---------------------------------------------------------------------------

def _ps_handoff(tmp_root, hid, completed_at="2026-04-26T10:00:00Z"):
    """Write a minimal valid completed delegate handoff."""
    _write_handoff(tmp_root, hid,
                   to="team-lead-peer",
                   completed_by="team-lead-peer",
                   completed_at=completed_at)


def _ps_args(tmp_path, hid, body="review body\n", name="body.md"):
    p = tmp_path / name
    p.write_text(body)
    return argparse.Namespace(
        handoff_id=hid,
        reviewer="cto",
        verdict="ship-as-is",
        findings_count="0",
        body_file=str(p),
    )


def _adv_dir(tmp_root, hid):
    return tmp_root / ".orchestrator" / "runtime" / "adversarial-reviews" / hid


def test_publication_state_write_fails_before_any_bytes(tmp_root, tmp_path, monkeypatch):
    """os.write raises immediately → helper exits 1; NO .md file written."""
    _ps_handoff(tmp_root, "h-ps1")

    def fake_write(fd, data):
        raise OSError("simulated immediate fail")
    monkeypatch.setattr(os, "write", fake_write)

    with pytest.raises(SystemExit) as exc:
        cmd_handoff_add_adversarial_review(_ps_args(tmp_path, "h-ps1"))
    assert exc.value.code == 1

    d = _adv_dir(tmp_root, "h-ps1")
    md_files = list(d.glob("*.md")) if d.exists() else []
    assert md_files == [], f"Expected no .md files, got: {[f.name for f in md_files]}"


def test_publication_state_partial_write_at_body_no_approveable_artifact(tmp_root, tmp_path, monkeypatch):
    """os.write writes 50 bytes then fails → helper exits 1; NO .md file; approve blocks."""
    _ps_handoff(tmp_root, "h-ps2")

    real_write = os.write

    def make_fake_write(fail_after_bytes):
        written_total = {"count": 0}

        def fake(fd, data):
            if written_total["count"] >= fail_after_bytes:
                raise OSError("simulated post-partial fail")
            remaining = fail_after_bytes - written_total["count"]
            chunk = data[:max(1, min(len(data), remaining))]
            n = real_write(fd, chunk)
            written_total["count"] += n
            return n
        return fake

    monkeypatch.setattr(os, "write", make_fake_write(50))

    with pytest.raises(SystemExit) as exc:
        cmd_handoff_add_adversarial_review(_ps_args(tmp_path, "h-ps2"))
    assert exc.value.code == 1

    d = _adv_dir(tmp_root, "h-ps2")
    md_files = list(d.glob("*.md")) if d.exists() else []
    assert md_files == [], f"Expected no .md files after partial write, got: {[f.name for f in md_files]}"

    # Also confirm approve gate blocks (no approvable artifact)
    monkeypatch.undo()
    with pytest.raises(SystemExit) as exc2:
        _approve("h-ps2")
    assert exc2.value.code == 1


def test_publication_state_write_fails_late(tmp_root, tmp_path, monkeypatch):
    """os.write writes most bytes then fails → helper exits 1; NO .md file."""
    _ps_handoff(tmp_root, "h-ps3")

    real_write = os.write
    call_count = {"n": 0}

    def fake_write(fd, data):
        call_count["n"] += 1
        if call_count["n"] >= 3:
            raise OSError("simulated late write fail")
        return real_write(fd, data[:20])

    monkeypatch.setattr(os, "write", fake_write)

    with pytest.raises(SystemExit) as exc:
        cmd_handoff_add_adversarial_review(_ps_args(tmp_path, "h-ps3"))
    assert exc.value.code == 1

    d = _adv_dir(tmp_root, "h-ps3")
    md_files = list(d.glob("*.md")) if d.exists() else []
    assert md_files == [], f"Expected no .md files after late write fail, got: {[f.name for f in md_files]}"


def test_publication_state_fsync_fails_post_write(tmp_root, tmp_path, monkeypatch):
    """os.fsync raises after full write → helper exits 1; NO .md file; approve blocks."""
    _ps_handoff(tmp_root, "h-ps4")

    original_fsync = os.fsync
    fsync_calls = {"count": 0}

    def fake_fsync(fd):
        fsync_calls["count"] += 1
        # Only fail the first fsync call (the content fsync, not the dir fsync)
        if fsync_calls["count"] == 1:
            raise OSError("simulated fsync fail")
        return original_fsync(fd)

    monkeypatch.setattr(os, "fsync", fake_fsync)

    with pytest.raises(SystemExit) as exc:
        cmd_handoff_add_adversarial_review(_ps_args(tmp_path, "h-ps4"))
    assert exc.value.code == 1

    d = _adv_dir(tmp_root, "h-ps4")
    md_files = list(d.glob("*.md")) if d.exists() else []
    assert md_files == [], f"Expected no .md files after fsync fail, got: {[f.name for f in md_files]}"

    # Approve also blocks
    monkeypatch.undo()
    with pytest.raises(SystemExit) as exc2:
        _approve("h-ps4")
    assert exc2.value.code == 1


def test_publication_state_collision_preserved(tmp_root, tmp_path, monkeypatch):
    """Two helper calls with fixed now_iso → <ts>.md and <ts>-1.md; first content untouched."""
    from lib import storage as _storage
    monkeypatch.setattr(_storage, "now_iso", lambda: "2026-04-26T14:00:00Z")

    _ps_handoff(tmp_root, "h-ps5")

    cmd_handoff_add_adversarial_review(_ps_args(tmp_path, "h-ps5", body="first body\n", name="b1.md"))
    cmd_handoff_add_adversarial_review(_ps_args(tmp_path, "h-ps5", body="second body\n", name="b2.md"))

    d = _adv_dir(tmp_root, "h-ps5")
    artifacts = sorted(d.glob("*.md"), key=lambda p: p.name)
    assert len(artifacts) == 2, f"Expected 2 artifacts, got: {[a.name for a in artifacts]}"

    # First artifact (ts.md) must preserve its original content
    first = next(a for a in artifacts if a.name == "2026-04-26T14:00:00Z.md")
    second = next(a for a in artifacts if a.name == "2026-04-26T14:00:00Z-1.md")
    assert "first body" in first.read_text()
    assert "second body" in second.read_text()


def test_publication_state_normal_success_writes_one_final_md(tmp_root, tmp_path):
    """Clean state → exactly ONE .md file, valid frontmatter with all 5 fields, NO .tmp leftovers."""
    _ps_handoff(tmp_root, "h-ps6")
    cmd_handoff_add_adversarial_review(_ps_args(tmp_path, "h-ps6"))

    d = _adv_dir(tmp_root, "h-ps6")
    all_files = list(d.iterdir()) if d.exists() else []
    md_files = [f for f in all_files if f.name.endswith(".md")]
    tmp_files = [f for f in all_files if ".tmp." in f.name]

    assert len(md_files) == 1, f"Expected 1 .md file, got: {[f.name for f in md_files]}"
    assert tmp_files == [], f"Expected no .tmp files, got: {[f.name for f in tmp_files]}"

    content = md_files[0].read_text()
    assert content.startswith("---\n")
    end = content.find("\n---\n", 4)
    fm = yaml.safe_load(content[4:end])
    for field in ("handoff_id", "reviewer", "verdict", "review_target_completed_at", "findings_count"):
        assert field in fm, f"Missing frontmatter field: {field}"

    # mtime must be > completed_at epoch
    from datetime import datetime, timezone
    completed_epoch = datetime.strptime("2026-04-26T10:00:00Z", "%Y-%m-%dT%H:%M:%SZ").replace(
        tzinfo=timezone.utc).timestamp()
    assert md_files[0].stat().st_mtime > completed_epoch


def test_publication_state_reader_ignores_tmp_files(tmp_root, tmp_path):
    """Pre-planted .tmp file with valid frontmatter → reader ignores it; approve blocks."""
    _ps_handoff(tmp_root, "h-ps7")

    # Plant a .tmp file directly (simulates a crashed helper mid-write)
    d = _adv_dir(tmp_root, "h-ps7")
    d.mkdir(parents=True, exist_ok=True)

    fm = {
        "handoff_id": "h-ps7",
        "reviewer": "cto",
        "review_target_completed_at": "2026-04-26T10:00:00Z",
        "verdict": "ship-as-is",
        "findings_count": 0,
    }
    fm_yaml = yaml.safe_dump(fm, default_flow_style=False, sort_keys=True)
    md_text = f"---\n{fm_yaml}---\n\nReview body.\n"
    tmp_file = d / ".2026-04-26T10:00:00Z.md.tmp.99999.0"
    tmp_file.write_text(md_text)

    # Approve must block because no .md file exists
    with pytest.raises(SystemExit) as exc:
        _approve("h-ps7")
    assert exc.value.code == 1


def test_publication_state_temp_unlinked_after_success(tmp_root, tmp_path):
    """Normal success → NO .tmp files remain in the per-handoff dir."""
    _ps_handoff(tmp_root, "h-ps8")
    cmd_handoff_add_adversarial_review(_ps_args(tmp_path, "h-ps8"))

    d = _adv_dir(tmp_root, "h-ps8")
    all_files = list(d.iterdir()) if d.exists() else []
    tmp_files = [f for f in all_files if ".tmp." in f.name or not f.name.endswith(".md")]
    assert tmp_files == [], f"Expected no non-.md files after success, got: {[f.name for f in tmp_files]}"


# ---------------------------------------------------------------------------
# rework-4 — boundary-symmetry: adversarial-reviews root symlink
# ---------------------------------------------------------------------------

def _make_outside_adv_artifact(outside_dir, handoff_id,
                                 completed_at="2026-04-26T10:00:00Z",
                                 reviewer="cto"):
    """Write a frontmatter-valid ship-as-is artifact in an outside directory.

    Sets mtime strictly > completed_at so the mtime gate would pass if the
    symlink check were absent.
    """
    import shutil
    from datetime import datetime, timezone

    per_hid = outside_dir / handoff_id
    per_hid.mkdir(parents=True, exist_ok=True)

    fm = {
        "handoff_id": handoff_id,
        "reviewer": reviewer,
        "review_target_completed_at": completed_at,
        "verdict": "ship-as-is",
        "findings_count": 0,
    }
    fm_yaml = yaml.safe_dump(fm, default_flow_style=False, sort_keys=True)
    md_text = f"---\n{fm_yaml}---\n\nReview body.\n"
    artifact = per_hid / "2026-04-26T11:00:00Z.md"
    artifact.write_text(md_text)

    # Set mtime strictly > completed_at (completed_at + 2 seconds)
    epoch = datetime.strptime(completed_at, "%Y-%m-%dT%H:%M:%SZ").replace(
        tzinfo=timezone.utc).timestamp()
    os.utime(str(artifact), (epoch + 2, epoch + 2))
    return artifact


def _plant_adv_reviews_symlink(tmp_root, outside_dir):
    """Remove the real adversarial-reviews dir and plant a symlink to outside_dir."""
    import shutil
    adv_reviews = tmp_root / ".orchestrator" / "runtime" / "adversarial-reviews"
    if adv_reviews.is_dir() and not adv_reviews.is_symlink():
        shutil.rmtree(str(adv_reviews))
    elif adv_reviews.exists() or adv_reviews.is_symlink():
        adv_reviews.unlink()
    adv_reviews.symlink_to(outside_dir)
    return adv_reviews


def _full_approve_setup(tmp_root, handoff_id, completed_at="2026-04-26T10:00:00Z"):
    """Write a fully-valid delegate handoff (no review artifact)."""
    _write_handoff(tmp_root, handoff_id,
                   to="team-lead-peer",
                   completed_by="team-lead-peer",
                   completed_at=completed_at)


# ---------------------------------------------------------------------------
# Test 1 (Codex P2 repro): symlinked adversarial-reviews root blocks approve
# ---------------------------------------------------------------------------

def test_rework4_codex_p2_runtime_adversarial_reviews_symlink_blocks_approve(
    tmp_root, tmp_path, capsys
):
    """Codex P2 repro: runtime/adversarial-reviews symlinked to outside dir.

    Outside dir contains a fully valid ship-as-is artifact:
    - handoff_id matches
    - verdict == ship-as-is
    - review_target_completed_at == handoff timestamps.completed_at
    - reviewer == cto (not assignee/completer)
    - mtime strictly > completed_at

    Pre-fix: approve PASSES (BUG — realpath containment silently follows).
    Post-fix: approve BLOCKS with 'missing: adversarial review'.
    Outside dir must be untouched after both calls.
    """
    handoff_id = "h-rw4-p2"
    completed_at = "2026-04-26T10:00:00Z"

    _full_approve_setup(tmp_root, handoff_id, completed_at)

    outside = tmp_path / "outside-repo-dir"
    outside.mkdir()
    artifact = _make_outside_adv_artifact(outside, handoff_id, completed_at, reviewer="cto")

    # Plant runtime/adversarial-reviews as a symlink to outside
    _plant_adv_reviews_symlink(tmp_root, outside)

    with pytest.raises(SystemExit) as exc:
        _approve(handoff_id)
    assert exc.value.code == 1
    captured = capsys.readouterr()
    assert "adversarial review" in captured.err.lower()

    # Outside dir and its artifact must be untouched
    assert artifact.exists()
    assert artifact.read_text().startswith("---\n")


# ---------------------------------------------------------------------------
# Test 2 (positive symmetry): RUNTIME_DIR symlink with real adversarial-reviews
# ---------------------------------------------------------------------------

def test_rework4_runtime_dir_symlink_succeeds_with_real_adversarial_reviews(
    tmp_root, tmp_path
):
    """Above-runtime symlinks must continue to succeed (rework-2 F1 invariant).

    RUNTIME_DIR itself is under a symlinked path. The adversarial-reviews child
    and <hid> dir are REAL directories (not symlinks). Helper write + approve
    must both succeed.
    """
    from lib import storage as _storage

    handoff_id = "h-rw4-pos"
    completed_at = "2026-04-26T10:00:00Z"

    # Build a real runtime dir under tmp_path and a symlinked alias to it
    real_rt = tmp_path / "real-runtime"
    real_rt.mkdir()
    sym_rt = tmp_path / "sym-runtime"
    sym_rt.symlink_to(real_rt)

    original_runtime = _storage.RUNTIME_DIR
    _storage.RUNTIME_DIR = str(sym_rt)
    try:
        _full_approve_setup(tmp_root, handoff_id, completed_at)

        # Write via helper (must succeed with symlinked RUNTIME_DIR)
        body_file = _make_body_file(tmp_path, "pos-body.md", "review body\n")
        args = _add_review_args(handoff_id, body_file)
        cmd_handoff_add_adversarial_review(args)

        # Confirm artifact landed in REAL dir
        adv_dir = real_rt / "adversarial-reviews" / handoff_id
        artifacts = list(adv_dir.glob("*.md"))
        assert len(artifacts) == 1, f"Expected 1 artifact under real dir, got {len(artifacts)}"

        # Bump mtime so it passes strict mtime gate
        from datetime import datetime, timezone
        epoch = datetime.strptime(completed_at, "%Y-%m-%dT%H:%M:%SZ").replace(
            tzinfo=timezone.utc).timestamp()
        os.utime(str(artifacts[0]), (epoch + 5, epoch + 5))

        # Approve must succeed (adversarial-reviews is a REAL dir via symlinked RUNTIME_DIR)
        _approve(handoff_id)
        state = storage.read_state(_handoff_path(tmp_root, handoff_id))
        assert state["review"]["outcome"] == "approved"
    finally:
        _storage.RUNTIME_DIR = original_runtime


# ---------------------------------------------------------------------------
# Test 3 (reader/writer symmetry): both reject symlinked adversarial-reviews
# ---------------------------------------------------------------------------

def test_rework4_reader_writer_symmetric_at_adversarial_reviews_child(
    tmp_root, tmp_path, capsys
):
    """Reader and writer are symmetric: both reject symlinked adversarial-reviews.

    Setup: runtime/adversarial-reviews is a symlink to an outside dir that
    contains a fully valid artifact.

    - Writer (cmd_handoff_add_adversarial_review) must fail (O_NOFOLLOW).
    - Reader (_find_valid_adversarial_review via cmd_handoff_approve) must fail
      (new islink check added in rework-4).

    Both fail → symmetric at this boundary component.
    """
    handoff_id = "h-rw4-sym"
    completed_at = "2026-04-26T10:00:00Z"

    _full_approve_setup(tmp_root, handoff_id, completed_at)

    outside = tmp_path / "outside-sym-check"
    outside.mkdir()
    # Pre-plant a valid artifact in outside dir (reader would accept if not for islink)
    _make_outside_adv_artifact(outside, handoff_id, completed_at, reviewer="cto")

    # Plant runtime/adversarial-reviews as symlink to outside
    _plant_adv_reviews_symlink(tmp_root, outside)

    # Writer must fail
    body_file = _make_body_file(tmp_path, "sym-body.md", "review body\n")
    args = _add_review_args(handoff_id, body_file)
    with pytest.raises(SystemExit) as exc_write:
        cmd_handoff_add_adversarial_review(args)
    assert exc_write.value.code == 1
    capsys.readouterr()  # flush

    # Reader must also fail
    with pytest.raises(SystemExit) as exc_read:
        _approve(handoff_id)
    assert exc_read.value.code == 1
    captured = capsys.readouterr()
    assert "adversarial review" in captured.err.lower()
