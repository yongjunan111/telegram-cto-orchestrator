"""Tests for checkpoint session-ref slug validation.

`cmd_session_checkpoint` reads `room_id` and `handoff_id` from the session
YAML and used to pass them directly into `storage.room_state_path()` /
`storage.handoff_path()`. A corrupt or tampered session YAML could therefore
smuggle `../` sequences or shell-hostile bytes into a filesystem path that
`read_state` opens.

Invariant exercised here:
- Invalid room_id / handoff_id from session YAML must fail slug validation
  BEFORE path construction.
- Shell-exit checkpoints (the primary high-volume path) must warn to stderr
  and write exactly one checkpoint marker — no crash, no duplicate artifacts.
"""
import argparse
import io
import os
import shutil
import sys
import tempfile
import unittest
from contextlib import redirect_stderr

_REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), os.pardir))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

from lib import checkpoints, storage  # noqa: E402


def _make_args(session_id, event, note=None):
    return argparse.Namespace(session_id=session_id, event=event, note=note)


class CheckpointRefValidationTestBase(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.mkdtemp(prefix="orch-checkpoint-ref-")
        self.orch_dir = os.path.join(self.tmpdir, ".orchestrator")
        self.rooms_dir = os.path.join(self.orch_dir, "rooms")
        self.handoffs_dir = os.path.join(self.orch_dir, "handoffs")
        self.runtime_dir = os.path.join(self.orch_dir, "runtime")
        self.sessions_dir = os.path.join(self.runtime_dir, "sessions")
        self.checkpoints_dir = os.path.join(self.runtime_dir, "checkpoints")

        for d in (self.rooms_dir, self.handoffs_dir, self.sessions_dir, self.checkpoints_dir):
            os.makedirs(d, exist_ok=True)

        self._orig_orch = storage.ORCHESTRATOR_DIR
        self._orig_rooms = storage.ROOMS_DIR
        self._orig_handoffs = storage.HANDOFFS_DIR
        self._orig_runtime = storage.RUNTIME_DIR
        self._orig_sessions = storage.SESSIONS_DIR
        self._orig_checkpoints_dir = checkpoints.CHECKPOINTS_DIR

        storage.ORCHESTRATOR_DIR = self.orch_dir
        storage.ROOMS_DIR = self.rooms_dir
        storage.HANDOFFS_DIR = self.handoffs_dir
        storage.RUNTIME_DIR = self.runtime_dir
        storage.SESSIONS_DIR = self.sessions_dir
        checkpoints.CHECKPOINTS_DIR = self.checkpoints_dir

    def tearDown(self):
        storage.ORCHESTRATOR_DIR = self._orig_orch
        storage.ROOMS_DIR = self._orig_rooms
        storage.HANDOFFS_DIR = self._orig_handoffs
        storage.RUNTIME_DIR = self._orig_runtime
        storage.SESSIONS_DIR = self._orig_sessions
        checkpoints.CHECKPOINTS_DIR = self._orig_checkpoints_dir
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def _write_session(self, session_id, room_id=None, handoff_id=None):
        state = {
            "session": {
                "id": session_id,
                "peer_id": "test-peer",
                "tmux_session": "orch-test",
                "tmux_target": "%1",
                "mode": "ephemeral",
                "status": "busy",
            }
        }
        if room_id is not None:
            state["session"]["room_id"] = room_id
        if handoff_id is not None:
            state["session"]["handoff_id"] = handoff_id
        storage.write_state(storage.session_path(session_id), state)

    def _write_room(self, room_id):
        room_dir = os.path.join(self.rooms_dir, room_id)
        os.makedirs(room_dir, exist_ok=True)
        storage.write_state(
            os.path.join(room_dir, "state.yaml"),
            {
                "room": {"id": room_id, "status": "active"},
                "context": {"goal": "test"},
                "lifecycle": {"current_phase": "execution"},
                "discovery": {},
            },
        )

    def _write_handoff(self, handoff_id, room_id="test-room"):
        storage.write_state(
            storage.handoff_path(handoff_id),
            {
                "handoff": {
                    "id": handoff_id,
                    "room_id": room_id,
                    "from": "orchestrator",
                    "to": "test-peer",
                    "status": "open",
                    "kind": "implementation",
                }
            },
        )

    def _checkpoint_count(self, session_id):
        return len([
            f for f in os.listdir(self.checkpoints_dir)
            if f.startswith(session_id + "-") and f.endswith(".md")
        ])


class TestCheckpointValidRefs(CheckpointRefValidationTestBase):
    def test_valid_refs_produce_loaded_sections(self):
        self._write_room("test-room")
        self._write_handoff("test-handoff", room_id="test-room")
        self._write_session("sess-a", room_id="test-room", handoff_id="test-handoff")

        checkpoints.cmd_session_checkpoint(_make_args("sess-a", "manual-checkpoint"))

        self.assertEqual(self._checkpoint_count("sess-a"), 1)
        # Find and read the checkpoint file
        files = [
            f for f in os.listdir(self.checkpoints_dir)
            if f.startswith("sess-a-")
        ]
        with open(os.path.join(self.checkpoints_dir, files[0])) as f:
            content = f.read()
        # Real handoff + room content present (not the "not available" fallbacks)
        self.assertIn("test-handoff", content)
        self.assertIn("test-room", content)
        self.assertNotIn("(handoff state not available)", content)
        self.assertNotIn("(room state not available)", content)


class TestCheckpointInvalidRefs(CheckpointRefValidationTestBase):
    def _assert_single_marker_with_warning(self, session_id, expected_warning_fragment):
        stderr_buf = io.StringIO()
        with redirect_stderr(stderr_buf):
            checkpoints.cmd_session_checkpoint(
                _make_args(session_id, "shell-exit")
            )
        self.assertEqual(
            self._checkpoint_count(session_id), 1,
            "shell-exit checkpoint must write exactly one marker",
        )
        stderr = stderr_buf.getvalue()
        self.assertIn(
            expected_warning_fragment, stderr,
            f"expected stderr to contain {expected_warning_fragment!r}, got: {stderr!r}",
        )

    def test_path_traversal_room_id_rejected_before_path_construction(self):
        # No room on disk under this id; if the value reached
        # storage.room_state_path it would resolve outside rooms/. We assert
        # the validation short-circuits BEFORE read_state is called by
        # patching room_state_path to blow up if invoked.
        original_room_state_path = storage.room_state_path
        called_with = []

        def _guard(room_id):
            called_with.append(room_id)
            return original_room_state_path(room_id)

        storage.room_state_path = _guard
        try:
            self._write_session(
                "sess-traverse-room",
                room_id="../../../etc/passwd",
                handoff_id=None,
            )
            self._assert_single_marker_with_warning(
                "sess-traverse-room", "unsafe room_id"
            )
            # Guard must never have been invoked with the unsafe value.
            self.assertNotIn("../../../etc/passwd", called_with)
        finally:
            storage.room_state_path = original_room_state_path

    def test_path_traversal_handoff_id_rejected_before_path_construction(self):
        original_handoff_path = storage.handoff_path
        called_with = []

        def _guard(handoff_id):
            called_with.append(handoff_id)
            return original_handoff_path(handoff_id)

        storage.handoff_path = _guard
        try:
            self._write_session(
                "sess-traverse-handoff",
                room_id=None,
                handoff_id="../../etc/passwd",
            )
            self._assert_single_marker_with_warning(
                "sess-traverse-handoff", "unsafe handoff_id"
            )
            self.assertNotIn("../../etc/passwd", called_with)
        finally:
            storage.handoff_path = original_handoff_path

    def test_shell_metacharacter_room_id_warns_and_writes_single_marker(self):
        self._write_session(
            "sess-shell-room",
            room_id="evil;rm -rf /",
            handoff_id=None,
        )
        self._assert_single_marker_with_warning(
            "sess-shell-room", "unsafe room_id"
        )

    def test_both_refs_invalid_writes_single_marker(self):
        self._write_session(
            "sess-both-bad",
            room_id="../rooms",
            handoff_id="evil$(id)",
        )
        stderr_buf = io.StringIO()
        with redirect_stderr(stderr_buf):
            checkpoints.cmd_session_checkpoint(
                _make_args("sess-both-bad", "shell-exit")
            )
        self.assertEqual(self._checkpoint_count("sess-both-bad"), 1)
        stderr = stderr_buf.getvalue()
        self.assertIn("unsafe room_id", stderr)
        self.assertIn("unsafe handoff_id", stderr)

    def test_empty_refs_produce_no_warning(self):
        # Missing / empty refs are not invalid — they simply skip the lookup.
        self._write_session("sess-no-refs")
        stderr_buf = io.StringIO()
        with redirect_stderr(stderr_buf):
            checkpoints.cmd_session_checkpoint(
                _make_args("sess-no-refs", "shell-exit")
            )
        self.assertEqual(self._checkpoint_count("sess-no-refs"), 1)
        self.assertEqual(stderr_buf.getvalue(), "")


class TestCheckpointManualFailClosed(CheckpointRefValidationTestBase):
    """Manual (non-shell-exit) checkpoint must fail-closed on unsafe refs.

    Unlike the shell-exit trap, the manual path is driven by a human/CI on a
    visible stdout. Corruption must not be silently skipped: surface the error
    and refuse to write any checkpoint marker.
    """

    def _assert_fail_closed(self, session_id, expected_error_fragment,
                            event="manual-checkpoint"):
        stderr_buf = io.StringIO()
        with redirect_stderr(stderr_buf):
            with self.assertRaises(SystemExit) as ctx:
                checkpoints.cmd_session_checkpoint(
                    _make_args(session_id, event)
                )
        self.assertEqual(
            ctx.exception.code, 1,
            "manual checkpoint must exit 1 on unsafe ref",
        )
        self.assertEqual(
            self._checkpoint_count(session_id), 0,
            "manual checkpoint must NOT write a marker on unsafe ref",
        )
        stderr = stderr_buf.getvalue()
        self.assertIn(
            expected_error_fragment, stderr,
            f"expected stderr to contain {expected_error_fragment!r}, "
            f"got: {stderr!r}",
        )
        # Manual path reports an error, not a warning.
        self.assertIn("Error:", stderr)
        self.assertNotIn("Warning:", stderr)

    def test_manual_path_traversal_room_id_fails_closed(self):
        self._write_session(
            "sess-manual-room",
            room_id="../../../etc/passwd",
            handoff_id=None,
        )
        self._assert_fail_closed("sess-manual-room", "unsafe room_id")

    def test_manual_path_traversal_handoff_id_fails_closed(self):
        self._write_session(
            "sess-manual-handoff",
            room_id=None,
            handoff_id="../../etc/passwd",
        )
        self._assert_fail_closed("sess-manual-handoff", "unsafe handoff_id")

    def test_manual_shell_metacharacter_handoff_id_fails_closed(self):
        self._write_session(
            "sess-manual-shell",
            room_id=None,
            handoff_id="evil$(id)",
        )
        self._assert_fail_closed("sess-manual-shell", "unsafe handoff_id")

    def test_manual_unsafe_room_id_fails_before_path_construction(self):
        # Guard: storage.room_state_path must never be called with the unsafe
        # value on the manual path (same invariant as shell-exit).
        original_room_state_path = storage.room_state_path
        called_with = []

        def _guard(room_id):
            called_with.append(room_id)
            return original_room_state_path(room_id)

        storage.room_state_path = _guard
        try:
            self._write_session(
                "sess-manual-guard",
                room_id="../../../etc/passwd",
                handoff_id=None,
            )
            self._assert_fail_closed("sess-manual-guard", "unsafe room_id")
            self.assertNotIn("../../../etc/passwd", called_with)
        finally:
            storage.room_state_path = original_room_state_path

    def test_manual_custom_event_also_fails_closed(self):
        # Any non-"shell-exit" event should take the fail-closed manual path.
        self._write_session(
            "sess-manual-custom",
            room_id="../rooms",
            handoff_id=None,
        )
        self._assert_fail_closed(
            "sess-manual-custom", "unsafe room_id",
            event="pre-compact",
        )

    def test_manual_valid_refs_still_produce_marker(self):
        # Regression: manual path must still succeed on safe refs.
        self._write_room("ok-room")
        self._write_handoff("ok-handoff", room_id="ok-room")
        self._write_session(
            "sess-manual-ok",
            room_id="ok-room",
            handoff_id="ok-handoff",
        )
        checkpoints.cmd_session_checkpoint(
            _make_args("sess-manual-ok", "manual-checkpoint")
        )
        self.assertEqual(self._checkpoint_count("sess-manual-ok"), 1)


if __name__ == "__main__":
    unittest.main()
