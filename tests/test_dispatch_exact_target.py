"""Tests for exact tmux pane/window targeting hardening.

These tests cover:
- Fresh dispatch capturing a pane target after tmux new-session and storing
  it in authoritative session state.
- Fresh dispatch rolling back tmux when capture fails (no state / artifact /
  hooks left behind).
- Reuse dispatch revalidating tmux_target drift, missing/unsafe target, and
  dead target — all fail-closed.
- Legacy session handling in the decision phase: same-handoff legacy sessions
  with live tmux still trigger wait_for_existing_assignment for duplicate
  blocking, but legacy sessions are NEVER reusable.

All tests are hermetic: tmp runtime dir + monkeypatched tmux helpers; no real
.orchestrator/ state and no real tmux are touched.
"""
import os
import sys
import unittest
from unittest import mock

# Make repo root importable
_REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), os.pardir))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

from lib import dispatch, storage  # noqa: E402

# Reuse the hermetic test base from the reuse race test file
from tests.test_dispatch_reuse_race import (  # noqa: E402
    ReuseRaceTestBase,
    _build_handoff_state,
    _build_room_state,
    _build_session_state,
)


# ---------------------------------------------------------------------------
# Fresh dispatch — exact pane target capture + rollback
# ---------------------------------------------------------------------------


class FreshDispatchTestBase(ReuseRaceTestBase):
    """Extends the hermetic reuse race base with the extra tmux helpers needed
    by fresh dispatch: tmux_create_session and tmux_capture_pane_target."""

    def setUp(self):
        super().setUp()

        # Default behaviors — overridden per-test as needed
        self.create_session_return = (True, "")
        self.capture_pane_return = (True, "%42")
        self.create_session_calls = []
        self.capture_pane_calls = []

        self._fresh_patches = [
            mock.patch.object(
                dispatch,
                "_tmux_create_session",
                side_effect=lambda name, cwd: (
                    self.create_session_calls.append((name, cwd))
                    or self.create_session_return
                ),
            ),
            mock.patch.object(
                dispatch,
                "_tmux_capture_pane_target",
                side_effect=lambda name: (
                    self.capture_pane_calls.append(name)
                    or self.capture_pane_return
                ),
            ),
        ]
        for p in self._fresh_patches:
            p.start()

    def tearDown(self):
        for p in self._fresh_patches:
            p.stop()
        super().tearDown()

    def _call_fresh(
        self,
        handoff_id="hf-test",
        room_id="room-test",
        target_peer="peer-test",
    ):
        return dispatch._execute_fresh_dispatch(
            handoff_state=_build_handoff_state(handoff_id, room_id),
            room_state=_build_room_state(room_id),
            target_peer=target_peer,
            handoff_id=handoff_id,
            handoff_room=room_id,
            handoff_kind="implementation",
            cwd="/tmp",
            now="2026-04-08T01:00:00Z",
            lease_until="2099-12-31T01:00:00Z",
        )


class TestFreshDispatchCaptureSuccess(FreshDispatchTestBase):
    def test_fresh_capture_stores_tmux_target_in_state(self):
        # Fresh path; no pre-existing tmux session, no pre-existing state file.
        self.tmux_exists_return = False  # collision check returns false
        self.capture_pane_return = (True, "%99")

        result = self._call_fresh()

        self.assertTrue(result["ok"], msg=f"fresh dispatch should succeed: {result}")
        self.assertEqual(result["tmux_target"], "%99")

        # Capture must have been called with the new tmux session name
        self.assertEqual(len(self.capture_pane_calls), 1)
        self.assertEqual(self.capture_pane_calls[0], result["tmux_session"])

        # Authoritative session state on disk has tmux_target stored
        disk_path = storage.session_path(result["session_id"])
        self.assertTrue(os.path.isfile(disk_path))
        disk = storage.read_state(disk_path)["session"]
        self.assertEqual(disk["tmux_target"], "%99")
        self.assertEqual(disk["status"], "busy")

    def test_fresh_calls_inject_and_bootstrap_with_target(self):
        self.tmux_exists_return = False
        self.capture_pane_return = (True, "%77")

        result = self._call_fresh()
        self.assertTrue(result["ok"])

        # _inject_session_hooks called with tmux_target as first arg
        self.assertEqual(len(self.inject_calls), 1)
        inject_args, _ = self.inject_calls[0]
        self.assertEqual(inject_args[0], "%77")

        # _run_bootstrap_and_display called with tmux_target as first arg
        self.assertEqual(len(self.bootstrap_calls), 1)
        boot_args, _ = self.bootstrap_calls[0]
        self.assertEqual(boot_args[0], "%77")

        # Dispatch artifact was written
        self.assertEqual(len(self.write_artifact_calls), 1)


class TestFreshDispatchCaptureRollback(FreshDispatchTestBase):
    def test_capture_failure_kills_tmux_and_rolls_back(self):
        self.tmux_exists_return = False
        # Simulate capture failure (e.g. tmux died between create and capture)
        self.capture_pane_return = (False, "tmux display-message failed: dead")

        result = self._call_fresh()

        self.assertFalse(result["ok"])
        self.assertIn("tmux pane target capture failed", result["error"])
        self.assertIn("rolled back", result["error"])

        # tmux must have been killed
        self.assertEqual(len(self.tmux_kill_calls), 1)

        # No session state file written
        # (we don't know the exact session_id without recomputing, but the
        #  sessions dir should be empty of any .yaml files)
        leftover = [
            f for f in os.listdir(self.sessions_dir)
            if f.endswith(".yaml")
        ]
        self.assertEqual(leftover, [], f"no session state should be written, got {leftover}")

        # No dispatch artifact, no hook inject, no bootstrap, no send-keys
        self.assertEqual(self.write_artifact_calls, [])
        self.assertEqual(self.inject_calls, [])
        self.assertEqual(self.bootstrap_calls, [])
        self.assertEqual(self.tmux_send_keys_calls, [])

    def test_capture_returns_unsafe_target(self):
        # Even if tmux returns "success" with a malformed pane id, fresh
        # dispatch must reject it. _tmux_capture_pane_target itself enforces
        # _is_safe_tmux_target on the captured value, so this exercises that
        # contract by simulating the helper returning False with that reason.
        self.tmux_exists_return = False
        self.capture_pane_return = (False, "captured pane target 'garbage' is not a safe pane id")

        result = self._call_fresh()
        self.assertFalse(result["ok"])
        self.assertIn("rolled back", result["error"])
        self.assertEqual(len(self.tmux_kill_calls), 1)
        self.assertEqual(self.write_artifact_calls, [])
        self.assertEqual(self.inject_calls, [])
        self.assertEqual(self.bootstrap_calls, [])


# ---------------------------------------------------------------------------
# Reuse dispatch — exact pane target revalidation
# ---------------------------------------------------------------------------


class TestReuseDispatchExactTarget(ReuseRaceTestBase):
    def test_reuse_fails_when_tmux_target_drift(self):
        snap = _build_session_state(tmux_target="%12")
        self._write_disk_state(snap)
        # Drift: on-disk state now has a different tmux_target
        drifted = _build_session_state(tmux_target="%99")
        self._write_disk_state(drifted)

        result = self._call_reuse(snap)
        self.assertFalse(result["ok"])
        self.assertIn("tmux_target changed", result["error"])
        self.assertEqual(self.write_artifact_calls, [])
        self.assertEqual(self.inject_calls, [])
        self.assertEqual(self.bootstrap_calls, [])

    def test_reuse_fails_when_snapshot_target_missing(self):
        # Legacy snapshot — no tmux_target at all. Reuse must bail BEFORE
        # acquiring the lock, so no lock file is created.
        snap = _build_session_state(tmux_target="")
        self._write_disk_state(snap)

        result = self._call_reuse(snap)
        self.assertFalse(result["ok"])
        self.assertIn("legacy session", result["error"])

        # Lock should NOT have been acquired (early return before lock)
        lock_path = dispatch._session_lock_path("sess-test")
        self.assertFalse(os.path.exists(lock_path))

        # No state mutation, no side effects
        disk = self._read_disk_state("sess-test")["session"]
        self.assertEqual(disk["status"], "idle")
        self.assertEqual(disk["reuse_count"], 0)
        self.assertEqual(self.write_artifact_calls, [])
        self.assertEqual(self.inject_calls, [])
        self.assertEqual(self.bootstrap_calls, [])

    def test_reuse_fails_when_snapshot_target_unsafe(self):
        snap = _build_session_state(tmux_target="not-a-pane-id")
        self._write_disk_state(snap)

        result = self._call_reuse(snap)
        self.assertFalse(result["ok"])
        self.assertIn("legacy session", result["error"])
        self.assertEqual(self.write_artifact_calls, [])
        self.assertEqual(self.inject_calls, [])

    def test_reuse_fails_when_tmux_target_dead(self):
        snap = _build_session_state(tmux_target="%12")
        self._write_disk_state(snap)
        # tmux session is alive but the exact target pane is dead
        self.tmux_exists_return = True
        self.tmux_target_exists_return = False

        result = self._call_reuse(snap)
        self.assertFalse(result["ok"])
        self.assertIn("tmux_target", result["error"])

        # No state mutation, no side effects
        disk = self._read_disk_state("sess-test")["session"]
        self.assertEqual(disk["status"], "idle")
        self.assertEqual(disk["reuse_count"], 0)
        self.assertEqual(self.write_artifact_calls, [])
        self.assertEqual(self.inject_calls, [])
        self.assertEqual(self.bootstrap_calls, [])

    def test_reuse_inject_and_bootstrap_use_target(self):
        snap = _build_session_state(tmux_target="%55")
        self._write_disk_state(snap)

        result = self._call_reuse(snap)
        self.assertTrue(result["ok"])
        self.assertEqual(result["tmux_target"], "%55")

        # _inject_session_hooks first arg is the target, not the session name
        self.assertEqual(len(self.inject_calls), 1)
        inject_args, _ = self.inject_calls[0]
        self.assertEqual(inject_args[0], "%55")

        # _run_bootstrap_and_display first arg is the target
        self.assertEqual(len(self.bootstrap_calls), 1)
        boot_args, _ = self.bootstrap_calls[0]
        self.assertEqual(boot_args[0], "%55")


# ---------------------------------------------------------------------------
# Decision phase — legacy session handling
# ---------------------------------------------------------------------------


class TestLegacySessionDecision(unittest.TestCase):
    """Decision-phase tests: legacy sessions (no tmux_target) must NOT be
    reusable, but if a legacy session is bound to the SAME handoff with a live
    tmux_session, duplicate dispatch must still be blocked via wait."""

    def setUp(self):
        # Mock both existence helpers; tests configure return values directly
        self.tmux_exists_return = True
        self.tmux_target_exists_return = True

        self._patches = [
            mock.patch.object(
                dispatch,
                "_tmux_session_exists",
                side_effect=lambda name: self.tmux_exists_return,
            ),
            mock.patch.object(
                dispatch,
                "_tmux_target_exists",
                side_effect=lambda target: self.tmux_target_exists_return,
            ),
        ]
        for p in self._patches:
            p.start()

    def tearDown(self):
        for p in self._patches:
            p.stop()

    def _make_legacy_session(self, handoff_id="", status="idle"):
        # Legacy: tmux_session set, tmux_target absent
        return _build_session_state(
            session_id="sess-legacy",
            tmux_target="",
            handoff_id=handoff_id,
            status=status,
        )

    def _peer_entry(self):
        return {"id": "peer-test", "type": "executor", "cwd": "/tmp"}

    def _decide(self, peer_sessions, handoff_id="hf-test"):
        target_peer = "peer-test"
        handoff_room = "room-test"
        h = {
            "id": handoff_id,
            "to": target_peer,
            "room_id": handoff_room,
            "status": "open",
        }
        evaluations = [
            {
                "state": s,
                "verdict": dispatch._evaluate_session_eligibility(
                    s, target_peer, handoff_room, handoff_id, "implementation"
                )[0],
                "reason": dispatch._evaluate_session_eligibility(
                    s, target_peer, handoff_room, handoff_id, "implementation"
                )[1],
            }
            for s in peer_sessions
        ]
        return dispatch._compute_dispatch_decision(
            h=h,
            peer_entry=self._peer_entry(),
            target_peer=target_peer,
            handoff_room=handoff_room,
            handoff_id=handoff_id,
            handoff_status="open",
            handoff_kind="implementation",
            review_state="none",
            peer_sessions=peer_sessions,
            session_evaluations=evaluations,
            session_parse_errors=[],
            room_blocker_summary="",
            room_blocked_by="",
        )

    def test_legacy_session_same_handoff_blocks_duplicate(self):
        # Legacy session is bound to the same handoff and has a live tmux
        # session name. Duplicate-blocking via wait_for_existing_assignment
        # must still fire even though the legacy session is not reusable.
        legacy = self._make_legacy_session(handoff_id="hf-test", status="busy")
        self.tmux_exists_return = True

        decision = self._decide([legacy], handoff_id="hf-test")
        self.assertEqual(decision["outcome"], "wait_for_existing_assignment")

    def test_legacy_session_same_handoff_dead_tmux_skipped(self):
        # Legacy session bound to same handoff but its tmux session is dead.
        # The wait check skips dead bindings; outcome should be fresh_session.
        legacy = self._make_legacy_session(handoff_id="hf-test", status="busy")
        self.tmux_exists_return = False

        decision = self._decide([legacy], handoff_id="hf-test")
        self.assertEqual(decision["outcome"], "fresh_session")

    def test_legacy_session_unrelated_handoff_falls_through_to_fresh(self):
        # Legacy session is for the same peer/room/idle but bound to a
        # DIFFERENT handoff. It must be ineligible for reuse (no tmux_target)
        # AND it does not trigger wait. Outcome: fresh_session.
        legacy = self._make_legacy_session(handoff_id="hf-other", status="idle")
        self.tmux_exists_return = True

        decision = self._decide([legacy], handoff_id="hf-test")
        self.assertEqual(decision["outcome"], "fresh_session")

    def test_legacy_session_idle_unbound_not_reusable(self):
        # Pure legacy session: idle, unbound, peer/room match. Without
        # tmux_target it must NOT be reusable, so the decision falls back to
        # fresh_session.
        legacy = self._make_legacy_session(handoff_id="", status="idle")
        self.tmux_exists_return = True

        decision = self._decide([legacy], handoff_id="hf-test")
        self.assertEqual(decision["outcome"], "fresh_session")

    def test_modern_session_idle_with_target_is_reusable(self):
        # Sanity / regression: a modern session WITH tmux_target is still
        # reusable in the decision phase.
        modern = _build_session_state(
            session_id="sess-modern",
            tmux_target="%88",
            handoff_id="",
            status="idle",
        )
        self.tmux_exists_return = True
        self.tmux_target_exists_return = True

        decision = self._decide([modern], handoff_id="hf-test")
        self.assertEqual(decision["outcome"], "reuse_existing_session")


if __name__ == "__main__":
    unittest.main()
