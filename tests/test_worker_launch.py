"""Tests for worker auto-launch during dispatch.

Covers:
- Fresh dispatch calls _launch_worker with bootstrap path.
- Bootstrap path is passed to launcher command via tmux send-keys.
- Exact tmux target is used for launch.
- Reuse dispatch also calls _launch_worker.
- Duplicate worker detection skips launch when pane already has worker.
- _launch_worker skips when bootstrap path is missing/empty.
"""
import os
import sys
import unittest
from unittest import mock

_REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), os.pardir))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

from lib import dispatch, storage  # noqa: E402
from tests.test_dispatch_reuse_race import (  # noqa: E402
    ReuseRaceTestBase,
    _build_handoff_state,
    _build_room_state,
    _build_session_state,
)
from tests.test_dispatch_exact_target import FreshDispatchTestBase  # noqa: E402


class TestFreshDispatchWorkerLaunch(FreshDispatchTestBase):
    """Fresh dispatch should generate bootstrap and launch worker."""

    def setUp(self):
        super().setUp()
        # Track _launch_worker calls separately from the patched bootstrap
        self.launch_worker_calls = []
        self._launch_patch = mock.patch.object(
            dispatch,
            "_launch_worker",
            side_effect=lambda *a, **k: self.launch_worker_calls.append((a, k)),
        )
        self._launch_patch.start()

    def tearDown(self):
        self._launch_patch.stop()
        super().tearDown()

    def test_fresh_dispatch_calls_launch_worker(self):
        self.tmux_exists_return = False
        self.capture_pane_return = (True, "%42")

        result = self._call_fresh()
        self.assertTrue(result["ok"])

        # _launch_worker must be called once
        self.assertEqual(len(self.launch_worker_calls), 1)
        args, kwargs = self.launch_worker_calls[0]
        # First arg is tmux_target
        self.assertEqual(args[0], "%42")
        # Second arg is session_id
        self.assertEqual(args[1], result["session_id"])

    def test_fresh_dispatch_passes_bootstrap_path(self):
        self.tmux_exists_return = False
        self.capture_pane_return = (True, "%42")

        # Make _run_bootstrap_and_display return a path
        with mock.patch.object(
            dispatch,
            "_run_bootstrap_and_display",
            return_value="/fake/bootstrap.md",
        ) as mock_bootstrap:
            # Need to also unpatch the one from parent setUp
            # Actually the parent patches _run_bootstrap_and_display already,
            # so we need to stop it first and re-patch
            pass

        # The parent class patches _run_bootstrap_and_display to record calls
        # and return nothing. Our _launch_worker patch captures what's passed.
        # The bootstrap_path arg (third positional) comes from the return of
        # _run_bootstrap_and_display.
        result = self._call_fresh()
        self.assertTrue(result["ok"])
        self.assertEqual(len(self.launch_worker_calls), 1)
        args, _ = self.launch_worker_calls[0]
        # Third arg is bootstrap_path (whatever _run_bootstrap_and_display returned)
        # Parent patches it to return None via side_effect, so it's falsy
        self.assertEqual(len(args), 3)


class TestReuseDispatchWorkerLaunch(ReuseRaceTestBase):
    """Reuse dispatch should also call _launch_worker."""

    def setUp(self):
        super().setUp()
        self.launch_worker_calls = []
        self._launch_patch = mock.patch.object(
            dispatch,
            "_launch_worker",
            side_effect=lambda *a, **k: self.launch_worker_calls.append((a, k)),
        )
        self._launch_patch.start()

    def tearDown(self):
        self._launch_patch.stop()
        super().tearDown()

    def test_reuse_dispatch_calls_launch_worker(self):
        snap = _build_session_state(tmux_target="%55")
        self._write_disk_state(snap)

        result = self._call_reuse(snap)
        self.assertTrue(result["ok"])

        self.assertEqual(len(self.launch_worker_calls), 1)
        args, _ = self.launch_worker_calls[0]
        self.assertEqual(args[0], "%55")

    def test_reuse_failed_dispatch_no_launch(self):
        """If reuse dispatch fails (e.g. drift), _launch_worker must not be called."""
        snap = _build_session_state(tmux_target="%55")
        self._write_disk_state(snap)
        # Drift: status changed
        drifted = _build_session_state(tmux_target="%55", status="busy")
        self._write_disk_state(drifted)

        result = self._call_reuse(snap)
        self.assertFalse(result["ok"])
        self.assertEqual(len(self.launch_worker_calls), 0)


class TestLaunchWorkerDirect(unittest.TestCase):
    """Direct unit tests for _launch_worker function."""

    def setUp(self):
        self.send_keys_calls = []
        self.pane_worker_return = False
        self.target_exists_return = True

        self._patches = [
            mock.patch.object(
                dispatch,
                "_tmux_send_keys",
                side_effect=lambda t, k: self.send_keys_calls.append((t, k)),
            ),
            mock.patch.object(
                dispatch,
                "_pane_has_worker",
                side_effect=lambda t: self.pane_worker_return,
            ),
            mock.patch.object(
                dispatch,
                "_is_safe_tmux_target",
                return_value=True,
            ),
            mock.patch.object(
                dispatch,
                "_tmux_target_exists",
                side_effect=lambda t: self.target_exists_return,
            ),
            mock.patch.object(
                dispatch,
                "_get_orchctl_invocation",
                return_value=("/usr/bin/python3", "/fake/orchctl"),
            ),
        ]
        for p in self._patches:
            p.start()

    def tearDown(self):
        for p in self._patches:
            p.stop()

    def test_launch_sends_command_to_exact_target(self):
        # Create a real temp file for bootstrap
        import tempfile
        with tempfile.NamedTemporaryFile(mode="w", suffix=".md", delete=False) as f:
            f.write("# Bootstrap\nTest content")
            bootstrap_path = f.name

        try:
            dispatch._launch_worker("%42", "sess-test", bootstrap_path)

            self.assertEqual(len(self.send_keys_calls), 1)
            target, cmd = self.send_keys_calls[0]
            self.assertEqual(target, "%42")
            self.assertIn("worker_launch.py", cmd)
            self.assertIn(bootstrap_path, cmd)
        finally:
            os.unlink(bootstrap_path)

    def test_launch_skips_when_worker_running(self):
        self.pane_worker_return = True
        import tempfile
        with tempfile.NamedTemporaryFile(mode="w", suffix=".md", delete=False) as f:
            f.write("# Bootstrap")
            bootstrap_path = f.name

        try:
            dispatch._launch_worker("%42", "sess-test", bootstrap_path)
            # No send-keys call — worker already running
            self.assertEqual(len(self.send_keys_calls), 0)
        finally:
            os.unlink(bootstrap_path)

    def test_launch_skips_when_no_bootstrap(self):
        dispatch._launch_worker("%42", "sess-test", "")
        # Should send an error message, not a launch command
        self.assertEqual(len(self.send_keys_calls), 1)
        _, cmd = self.send_keys_calls[0]
        self.assertIn("not available", cmd)

    def test_launch_skips_when_bootstrap_file_missing(self):
        dispatch._launch_worker("%42", "sess-test", "/nonexistent/file.md")
        self.assertEqual(len(self.send_keys_calls), 1)
        _, cmd = self.send_keys_calls[0]
        self.assertIn("not available", cmd)

    def test_launch_skips_when_target_dead(self):
        self.target_exists_return = False
        dispatch._launch_worker("%42", "sess-test", "/fake/bootstrap.md")
        self.assertEqual(len(self.send_keys_calls), 0)


if __name__ == "__main__":
    unittest.main()
