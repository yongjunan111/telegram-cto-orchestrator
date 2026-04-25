"""Tests for dispatch degraded-launch surfacing.

The handoff contract: dispatch success != worker success. When
bootstrap/hook/send/worker-launch steps partially fail, the dispatch result
must surface launch_status="degraded" with per-step detail while NOT rolling
back the already-created tmux/session state. Session YAML may only carry the
two compact fields last_launch_status and last_launch_at; verbose failure
reasons live in the dispatch command output and the in-memory result.

Launch status enum (rework-1): launched / degraded / skipped.
  - launched: all steps ok and worker actually started
  - degraded: any step failed
  - skipped:  operator disabled auto-launch, or pane already had a worker
              (both are intentional non-failures, distinct from "launched")

These tests are hermetic: tmp runtime + mocks only; no real tmux, no real
.orchestrator/ writes.
"""
import os
import sys
import tempfile
import unittest
from unittest import mock

_REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), os.pardir))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

from lib import dispatch, storage  # noqa: E402
from tests.test_dispatch_reuse_race import (  # noqa: E402
    ReuseRaceTestBase,
    _build_session_state,
)
from tests.test_dispatch_exact_target import FreshDispatchTestBase  # noqa: E402


# Fields that must NOT appear in session YAML after launch — the handoff
# constraint restricts session YAML to the compact two-field stamp.
_FORBIDDEN_VERBOSE_YAML_KEYS = frozenset({
    "last_launch_reasons",
    "launch_reasons",
    "last_launch_failure",
    "last_launch_error",
    "last_launch_parts",
    "last_launch_detail",
    "last_hooks_reason",
    "last_worker_reason",
    "last_bootstrap_reason",
})


# ---------------------------------------------------------------------------
# Pure unit tests: _aggregate_launch_status
# ---------------------------------------------------------------------------


class TestAggregateLaunchStatus(unittest.TestCase):
    """The aggregator encodes the contract between launch steps and
    launch_status/parts/reasons. Drive each branch directly."""

    def test_all_ok_returns_launched_with_empty_reasons(self):
        status, parts, reasons = dispatch._aggregate_launch_status(
            artifact_ok=True, artifact_reason="",
            hooks_raw=(True, ""),
            bootstrap_path="/tmp/boot.md",
            worker_raw=("launched", ""),
        )
        self.assertEqual(status, "launched")
        self.assertEqual(parts,
                         {"artifact": "ok", "hooks": "ok",
                          "bootstrap": "ok", "worker": "launched"})
        self.assertEqual(reasons, [])

    def test_legacy_none_tolerated_as_launched(self):
        # Backward-compat: older test fixtures / mocks may return None from
        # inject/launch side_effects. The aggregator must not crash and must
        # not spuriously flip to degraded.
        status, parts, _ = dispatch._aggregate_launch_status(
            artifact_ok=True, artifact_reason="",
            hooks_raw=None,
            bootstrap_path="/tmp/boot.md",
            worker_raw=None,
        )
        self.assertEqual(status, "launched")
        self.assertEqual(parts["hooks"], "ok")
        self.assertEqual(parts["worker"], "launched")

    def test_artifact_failure_marks_degraded(self):
        status, parts, reasons = dispatch._aggregate_launch_status(
            artifact_ok=False, artifact_reason="disk full",
            hooks_raw=(True, ""),
            bootstrap_path="/tmp/boot.md",
            worker_raw=("launched", ""),
        )
        self.assertEqual(status, "degraded")
        self.assertEqual(parts["artifact"], "failed")
        self.assertTrue(any("artifact" in r for r in reasons))

    def test_hooks_failure_marks_degraded(self):
        status, parts, reasons = dispatch._aggregate_launch_status(
            artifact_ok=True, artifact_reason="",
            hooks_raw=(False, "tmux send-keys failed (env exports)"),
            bootstrap_path="/tmp/boot.md",
            worker_raw=("launched", ""),
        )
        self.assertEqual(status, "degraded")
        self.assertEqual(parts["hooks"], "failed")
        self.assertTrue(any("hooks" in r and "send-keys" in r for r in reasons))

    def test_empty_bootstrap_marks_degraded(self):
        status, parts, reasons = dispatch._aggregate_launch_status(
            artifact_ok=True, artifact_reason="",
            hooks_raw=(True, ""),
            bootstrap_path="",
            worker_raw=("skipped_no_bootstrap", "bootstrap artifact not available"),
        )
        self.assertEqual(status, "degraded")
        self.assertEqual(parts["bootstrap"], "failed")
        self.assertEqual(parts["worker"], "skipped_no_bootstrap")

    def test_worker_failed_marks_degraded(self):
        status, parts, reasons = dispatch._aggregate_launch_status(
            artifact_ok=True, artifact_reason="",
            hooks_raw=(True, ""),
            bootstrap_path="/tmp/boot.md",
            worker_raw=("failed", "tmux send-keys failed for worker launch"),
        )
        self.assertEqual(status, "degraded")
        self.assertEqual(parts["worker"], "failed")
        self.assertTrue(any("worker" in r for r in reasons))

    def test_worker_skipped_disabled_maps_to_skipped(self):
        # auto_launch_worker=false is an operator choice; not degraded.
        status, parts, reasons = dispatch._aggregate_launch_status(
            artifact_ok=True, artifact_reason="",
            hooks_raw=(True, ""),
            bootstrap_path="/tmp/boot.md",
            worker_raw=("skipped_disabled", "auto_launch_worker disabled in config"),
        )
        self.assertEqual(status, "skipped")
        self.assertEqual(parts["worker"], "skipped_disabled")
        self.assertEqual(reasons, [])

    def test_worker_skipped_existing_maps_to_skipped(self):
        # Duplicate-avoidance (pane already has a worker) is not a failure.
        status, parts, reasons = dispatch._aggregate_launch_status(
            artifact_ok=True, artifact_reason="",
            hooks_raw=(True, ""),
            bootstrap_path="/tmp/boot.md",
            worker_raw=("skipped_existing", "pane already has a worker process"),
        )
        self.assertEqual(status, "skipped")
        self.assertEqual(parts["worker"], "skipped_existing")
        self.assertEqual(reasons, [])

    def test_multi_step_failure_lists_each_reason(self):
        status, parts, reasons = dispatch._aggregate_launch_status(
            artifact_ok=False, artifact_reason="disk full",
            hooks_raw=(False, "send-keys failed"),
            bootstrap_path="",
            worker_raw=("failed", "target dead"),
        )
        self.assertEqual(status, "degraded")
        # All four parts surfaced
        self.assertEqual(parts["artifact"], "failed")
        self.assertEqual(parts["hooks"], "failed")
        self.assertEqual(parts["bootstrap"], "failed")
        self.assertEqual(parts["worker"], "failed")
        joined = " | ".join(reasons)
        self.assertIn("artifact", joined)
        self.assertIn("hooks", joined)
        self.assertIn("bootstrap", joined)
        self.assertIn("worker", joined)


# ---------------------------------------------------------------------------
# Fresh dispatch end-to-end: launch status + no rollback
# ---------------------------------------------------------------------------


class _FreshLaunchStatusBase(FreshDispatchTestBase):
    """Extends FreshDispatchTestBase by replacing the parent's generic mocks
    for inject/bootstrap/artifact with ones that return typed values so we
    can drive specific failure modes."""

    def setUp(self):
        super().setUp()
        # Start empty — _patch_helpers layers on top.
        self._extra_patches = []

    def tearDown(self):
        for p in self._extra_patches:
            p.stop()
        super().tearDown()

    def _patch_helpers(
        self,
        hooks_return=(True, ""),
        bootstrap_return="/fake/bootstrap.md",
        worker_return=("launched", ""),
        artifact_raises=None,
    ):
        def inject_se(*a, **k):
            self.inject_calls.append((a, k))
            return hooks_return

        def bootstrap_se(*a, **k):
            self.bootstrap_calls.append((a, k))
            return bootstrap_return

        def artifact_se(*a, **k):
            self.write_artifact_calls.append((a, k))
            if artifact_raises is not None:
                raise artifact_raises
            return os.path.join(self.runtime_dir, "dispatches", "fake.md")

        def worker_se(*a, **k):
            return worker_return

        self._extra_patches = [
            mock.patch.object(dispatch, "_inject_session_hooks", side_effect=inject_se),
            mock.patch.object(dispatch, "_run_bootstrap_and_display", side_effect=bootstrap_se),
            mock.patch.object(dispatch, "_write_dispatch_artifact", side_effect=artifact_se),
            mock.patch.object(dispatch, "_launch_worker", side_effect=worker_se),
        ]
        for p in self._extra_patches:
            p.start()


class TestFreshDispatchAllOk(_FreshLaunchStatusBase):
    def test_all_steps_ok_sets_launch_status_launched(self):
        self.tmux_exists_return = False  # collision check on fresh allocation
        self.capture_pane_return = (True, "%77")
        self._patch_helpers()

        result = self._call_fresh()

        self.assertTrue(result["ok"])
        self.assertEqual(result["launch_status"], "launched")
        self.assertEqual(result["launch_parts"]["artifact"], "ok")
        self.assertEqual(result["launch_parts"]["hooks"], "ok")
        self.assertEqual(result["launch_parts"]["bootstrap"], "ok")
        self.assertEqual(result["launch_parts"]["worker"], "launched")
        self.assertEqual(result["launch_reasons"], [])

        disk = storage.read_state(storage.session_path(result["session_id"]))["session"]
        self.assertEqual(disk["last_launch_status"], "launched")
        self.assertIn("last_launch_at", disk)
        self.assertTrue(disk["last_launch_at"])
        self.assertEqual(disk["status"], "busy")


class TestFreshDispatchHooksFailure(_FreshLaunchStatusBase):
    def test_hooks_failure_marks_degraded_but_keeps_session(self):
        self.tmux_exists_return = False
        self.capture_pane_return = (True, "%77")
        self._patch_helpers(
            hooks_return=(False, "tmux send-keys failed (env exports)")
        )

        result = self._call_fresh()

        # Dispatch itself succeeded (session + tmux are live)
        self.assertTrue(result["ok"])
        self.assertEqual(result["launch_status"], "degraded")
        self.assertEqual(result["launch_parts"]["hooks"], "failed")
        self.assertTrue(any("hooks" in r for r in result["launch_reasons"]))

        # Session YAML stamp reflects the degraded outcome
        disk = storage.read_state(storage.session_path(result["session_id"]))["session"]
        self.assertEqual(disk["last_launch_status"], "degraded")
        self.assertEqual(disk["status"], "busy")

        # INVARIANT: no tmux rollback on worker-stage failure
        self.assertEqual(self.tmux_kill_calls, [])


class TestFreshDispatchBootstrapFailure(_FreshLaunchStatusBase):
    def test_empty_bootstrap_drives_skipped_no_bootstrap_and_degrades(self):
        self.tmux_exists_return = False
        self.capture_pane_return = (True, "%77")
        self._patch_helpers(
            bootstrap_return="",
            worker_return=("skipped_no_bootstrap", "bootstrap artifact not available"),
        )

        result = self._call_fresh()

        self.assertTrue(result["ok"])
        self.assertEqual(result["launch_status"], "degraded")
        self.assertEqual(result["launch_parts"]["bootstrap"], "failed")
        self.assertEqual(result["launch_parts"]["worker"], "skipped_no_bootstrap")

        disk = storage.read_state(storage.session_path(result["session_id"]))["session"]
        self.assertEqual(disk["last_launch_status"], "degraded")
        self.assertEqual(self.tmux_kill_calls, [])


class TestFreshDispatchWorkerFailure(_FreshLaunchStatusBase):
    def test_worker_failed_surfaces_degraded_no_rollback(self):
        self.tmux_exists_return = False
        self.capture_pane_return = (True, "%77")
        self._patch_helpers(
            worker_return=("failed", "tmux send-keys failed for worker launch")
        )

        result = self._call_fresh()

        # The dispatch layer — tmux + session state — remains valid.
        self.assertTrue(result["ok"])
        self.assertEqual(result["launch_status"], "degraded")
        self.assertEqual(result["launch_parts"]["worker"], "failed")
        self.assertTrue(any("worker" in r for r in result["launch_reasons"]))

        # INVARIANT: worker launch failure must not kill tmux nor drop the
        # session file. last_launch_status is last-attempt, not live health.
        self.assertEqual(self.tmux_kill_calls, [])
        disk_path = storage.session_path(result["session_id"])
        self.assertTrue(os.path.isfile(disk_path))
        disk = storage.read_state(disk_path)["session"]
        self.assertEqual(disk["status"], "busy")
        self.assertEqual(disk["last_launch_status"], "degraded")


class TestFreshDispatchArtifactFailure(_FreshLaunchStatusBase):
    def test_artifact_failure_degrades_launch_not_dispatch(self):
        self.tmux_exists_return = False
        self.capture_pane_return = (True, "%77")
        self._patch_helpers(artifact_raises=RuntimeError("simulated disk full"))

        result = self._call_fresh()

        self.assertTrue(result["ok"])
        self.assertEqual(result["artifact_path"], "(failed)")
        self.assertEqual(result["launch_status"], "degraded")
        self.assertEqual(result["launch_parts"]["artifact"], "failed")
        self.assertTrue(any("artifact" in r for r in result["launch_reasons"]))


class TestFreshDispatchSkippedPaths(_FreshLaunchStatusBase):
    def test_worker_skipped_disabled_maps_to_skipped(self):
        self.tmux_exists_return = False
        self.capture_pane_return = (True, "%77")
        self._patch_helpers(
            worker_return=("skipped_disabled", "auto_launch_worker disabled in config")
        )

        result = self._call_fresh()
        self.assertTrue(result["ok"])
        self.assertEqual(result["launch_status"], "skipped")
        disk = storage.read_state(storage.session_path(result["session_id"]))["session"]
        self.assertEqual(disk["last_launch_status"], "skipped")

    def test_worker_skipped_existing_maps_to_skipped(self):
        self.tmux_exists_return = False
        self.capture_pane_return = (True, "%77")
        self._patch_helpers(
            worker_return=("skipped_existing", "pane already has a worker process")
        )

        result = self._call_fresh()
        self.assertTrue(result["ok"])
        self.assertEqual(result["launch_status"], "skipped")


class TestFreshDispatchYamlCompactness(_FreshLaunchStatusBase):
    """Handoff constraint: session YAML must carry only last_launch_status and
    last_launch_at; detailed failure reasons must live in stdout/result, not
    YAML. This test drives a multi-step failure to make sure the temptation to
    dump reasons into YAML has not been indulged."""

    def test_only_two_new_fields_land_in_yaml_even_when_many_steps_fail(self):
        self.tmux_exists_return = False
        self.capture_pane_return = (True, "%77")
        self._patch_helpers(
            hooks_return=(False, "send-keys failed (env exports)"),
            bootstrap_return="",
            worker_return=("failed", "tmux send-keys failed for worker launch"),
            artifact_raises=RuntimeError("disk full"),
        )

        result = self._call_fresh()
        self.assertTrue(result["ok"])
        self.assertEqual(result["launch_status"], "degraded")

        disk = storage.read_state(storage.session_path(result["session_id"]))["session"]

        self.assertIn("last_launch_status", disk)
        self.assertIn("last_launch_at", disk)

        overlap = _FORBIDDEN_VERBOSE_YAML_KEYS & set(disk.keys())
        self.assertEqual(
            overlap, set(),
            f"session YAML must not store verbose launch failure fields; got {overlap}"
        )

        # Reasons must still be reachable — but only in the result dict.
        self.assertGreaterEqual(len(result["launch_reasons"]), 3)


# ---------------------------------------------------------------------------
# Reuse dispatch end-to-end: launch status + no rollback
# ---------------------------------------------------------------------------


class _ReuseLaunchStatusBase(ReuseRaceTestBase):
    def setUp(self):
        super().setUp()
        self._extra_patches = []

    def tearDown(self):
        for p in self._extra_patches:
            p.stop()
        super().tearDown()

    def _patch_helpers(
        self,
        hooks_return=(True, ""),
        bootstrap_return="/fake/bootstrap.md",
        worker_return=("launched", ""),
        artifact_raises=None,
    ):
        def inject_se(*a, **k):
            self.inject_calls.append((a, k))
            return hooks_return

        def bootstrap_se(*a, **k):
            self.bootstrap_calls.append((a, k))
            return bootstrap_return

        def artifact_se(*a, **k):
            self.write_artifact_calls.append((a, k))
            if artifact_raises is not None:
                raise artifact_raises
            return os.path.join(self.runtime_dir, "dispatches", "fake.md")

        def worker_se(*a, **k):
            return worker_return

        self._extra_patches = [
            mock.patch.object(dispatch, "_inject_session_hooks", side_effect=inject_se),
            mock.patch.object(dispatch, "_run_bootstrap_and_display", side_effect=bootstrap_se),
            mock.patch.object(dispatch, "_write_dispatch_artifact", side_effect=artifact_se),
            mock.patch.object(dispatch, "_launch_worker", side_effect=worker_se),
        ]
        for p in self._extra_patches:
            p.start()


class TestReuseDispatchAllOk(_ReuseLaunchStatusBase):
    def test_all_ok_sets_launch_status_launched_on_reuse(self):
        self._patch_helpers()
        snap = _build_session_state(tmux_target="%55")
        self._write_disk_state(snap)

        result = self._call_reuse(snap)
        self.assertTrue(result["ok"])
        self.assertEqual(result["launch_status"], "launched")
        self.assertEqual(result["launch_parts"]["worker"], "launched")

        disk = self._read_disk_state("sess-test")["session"]
        self.assertEqual(disk["last_launch_status"], "launched")
        self.assertIn("last_launch_at", disk)
        self.assertEqual(disk["status"], "busy")
        # reuse_count bumped as part of the normal reuse flow
        self.assertEqual(disk["reuse_count"], 1)


class TestReuseDispatchWorkerFailure(_ReuseLaunchStatusBase):
    def test_worker_failure_degrades_but_preserves_reuse_claim(self):
        self._patch_helpers(
            worker_return=("failed", "tmux send-keys failed for worker launch")
        )
        snap = _build_session_state(tmux_target="%55")
        self._write_disk_state(snap)

        result = self._call_reuse(snap)
        self.assertTrue(result["ok"])
        self.assertEqual(result["launch_status"], "degraded")
        self.assertEqual(result["launch_parts"]["worker"], "failed")

        # Session state reflects the busy claim; worker-stage failure does
        # not un-claim the session or kill tmux.
        disk = self._read_disk_state("sess-test")["session"]
        self.assertEqual(disk["status"], "busy")
        self.assertEqual(disk["reuse_count"], 1)
        self.assertEqual(disk["last_launch_status"], "degraded")
        self.assertEqual(self.tmux_kill_calls, [])


class TestReuseDispatchBootstrapFailure(_ReuseLaunchStatusBase):
    def test_bootstrap_failure_degrades_on_reuse(self):
        self._patch_helpers(
            bootstrap_return="",
            worker_return=("skipped_no_bootstrap", "bootstrap artifact not available"),
        )
        snap = _build_session_state(tmux_target="%55")
        self._write_disk_state(snap)

        result = self._call_reuse(snap)
        self.assertTrue(result["ok"])
        self.assertEqual(result["launch_status"], "degraded")
        self.assertEqual(result["launch_parts"]["bootstrap"], "failed")
        self.assertEqual(result["launch_parts"]["worker"], "skipped_no_bootstrap")


class TestReuseDispatchYamlCompactness(_ReuseLaunchStatusBase):
    def test_only_two_new_fields_land_in_yaml_on_reuse(self):
        self._patch_helpers(
            hooks_return=(False, "hook install failed"),
            worker_return=("failed", "tmux send-keys failed"),
        )
        snap = _build_session_state(tmux_target="%55")
        self._write_disk_state(snap)

        result = self._call_reuse(snap)
        self.assertTrue(result["ok"])

        disk = self._read_disk_state("sess-test")["session"]
        self.assertIn("last_launch_status", disk)
        self.assertIn("last_launch_at", disk)

        overlap = _FORBIDDEN_VERBOSE_YAML_KEYS & set(disk.keys())
        self.assertEqual(overlap, set())


# ---------------------------------------------------------------------------
# Helper unit tests: new return contracts
# ---------------------------------------------------------------------------


class TestInjectSessionHooksReturnContract(unittest.TestCase):
    """_inject_session_hooks must return (ok, reason) so callers can track
    degraded launch without inspecting stderr or tmux side effects."""

    def test_unsafe_target_returns_failure_tuple(self):
        ok, reason = dispatch._inject_session_hooks("not-a-pane-id", "s", "h", "r")
        self.assertFalse(ok)
        self.assertIn("unsafe", reason.lower())

    def test_dead_target_returns_failure_tuple(self):
        with mock.patch.object(dispatch, "_tmux_target_exists", return_value=False):
            ok, reason = dispatch._inject_session_hooks("%1", "s", "h", "r")
        self.assertFalse(ok)
        self.assertIn("not alive", reason)

    def test_send_keys_failure_propagates(self):
        with mock.patch.object(dispatch, "_tmux_target_exists", return_value=True), \
             mock.patch.object(dispatch, "_install_session_hook_file",
                               return_value=("/tmp/hook.sh", True, "")), \
             mock.patch.object(dispatch, "_get_orchctl_invocation",
                               return_value=("/usr/bin/python3", "/fake/orchctl")), \
             mock.patch.object(dispatch, "_tmux_send_keys", return_value=False):
            ok, reason = dispatch._inject_session_hooks("%1", "s", "h", "r")
        self.assertFalse(ok)
        self.assertIn("send-keys", reason)


class TestLaunchWorkerReturnContract(unittest.TestCase):
    """_launch_worker must return (status, reason) covering all documented
    branches. We mock enough of the environment to hit each branch without
    touching real tmux or real config."""

    def setUp(self):
        self._patches = [
            mock.patch.object(dispatch, "_is_safe_tmux_target", return_value=True),
            mock.patch.object(dispatch, "_tmux_target_exists", return_value=True),
            mock.patch.object(dispatch, "_pane_has_worker", return_value=False),
            mock.patch.object(dispatch, "_get_orchctl_invocation",
                              return_value=("/usr/bin/python3", "/fake/orchctl")),
            # Default config enables auto-launch
            mock.patch.object(dispatch, "load_config",
                              return_value={"dispatch": {"auto_launch_worker": True}}),
        ]
        for p in self._patches:
            p.start()

        # Real temp bootstrap file for the happy path
        self._tempf = tempfile.NamedTemporaryFile(mode="w", suffix=".md", delete=False)
        self._tempf.write("# Bootstrap\nfake content\n")
        self._tempf.close()
        self.bootstrap_path = self._tempf.name

    def tearDown(self):
        for p in self._patches:
            p.stop()
        try:
            os.unlink(self.bootstrap_path)
        except OSError:
            pass

    def test_launch_success_returns_launched(self):
        with mock.patch.object(dispatch, "_tmux_send_keys", return_value=True):
            status, reason = dispatch._launch_worker("%1", "sess-test", self.bootstrap_path)
        self.assertEqual(status, "launched")
        self.assertEqual(reason, "")

    def test_send_keys_failure_returns_failed(self):
        with mock.patch.object(dispatch, "_tmux_send_keys", return_value=False):
            status, reason = dispatch._launch_worker("%1", "sess-test", self.bootstrap_path)
        self.assertEqual(status, "failed")
        self.assertIn("send-keys", reason)

    def test_missing_bootstrap_returns_skipped_no_bootstrap(self):
        with mock.patch.object(dispatch, "_tmux_send_keys", return_value=True):
            status, reason = dispatch._launch_worker("%1", "sess-test", "")
        self.assertEqual(status, "skipped_no_bootstrap")
        self.assertIn("bootstrap", reason)

    def test_pane_has_worker_returns_skipped_existing(self):
        with mock.patch.object(dispatch, "_pane_has_worker", return_value=True), \
             mock.patch.object(dispatch, "_tmux_send_keys", return_value=True):
            status, reason = dispatch._launch_worker("%1", "sess-test", self.bootstrap_path)
        self.assertEqual(status, "skipped_existing")
        self.assertIn("worker", reason)

    def test_auto_launch_disabled_returns_skipped_disabled(self):
        with mock.patch.object(dispatch, "load_config",
                               return_value={"dispatch": {"auto_launch_worker": False}}):
            status, reason = dispatch._launch_worker("%1", "sess-test", self.bootstrap_path)
        self.assertEqual(status, "skipped_disabled")

    def test_dead_target_returns_failed(self):
        with mock.patch.object(dispatch, "_tmux_target_exists", return_value=False):
            status, reason = dispatch._launch_worker("%1", "sess-test", self.bootstrap_path)
        self.assertEqual(status, "failed")
        self.assertIn("not alive", reason)


class TestUpdateLastLaunchStatusIsBestEffort(unittest.TestCase):
    """The YAML stamp must never crash dispatch; it is derived info."""

    def test_missing_session_file_silently_noops(self):
        # Point storage.SESSIONS_DIR at a tmpdir containing no files. Should
        # not raise.
        tmpdir = tempfile.mkdtemp(prefix="orch-last-launch-")
        orig = storage.SESSIONS_DIR
        try:
            storage.SESSIONS_DIR = tmpdir
            # No file → _update_last_launch_status should silently return
            dispatch._update_last_launch_status("nonexistent-session", "launched")
        finally:
            storage.SESSIONS_DIR = orig
            try:
                os.rmdir(tmpdir)
            except OSError:
                pass


if __name__ == "__main__":
    unittest.main()
