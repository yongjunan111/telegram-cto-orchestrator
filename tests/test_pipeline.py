"""Tests for the task pipeline (orchctl task run).

Covers:
- Full pipeline creates room, handoff, and calls dispatch.
- Pipeline fails cleanly at each step with clear error.
- Generated IDs are slug-safe.
- Room gets execution_cwd set.
- Handoff is created without require_peer.
"""
import os
import shutil
import sys
import tempfile
import unittest
from unittest import mock

_REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), os.pardir))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

from lib import pipeline, storage  # noqa: E402
from lib.validators import is_slug_safe  # noqa: E402


class PipelineTestBase(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.mkdtemp(prefix="orch-pipeline-")
        self.orch_dir = os.path.join(self.tmpdir, ".orchestrator")
        self.rooms_dir = os.path.join(self.orch_dir, "rooms")
        self.handoffs_dir = os.path.join(self.orch_dir, "handoffs")
        self.template_dir = os.path.join(self.rooms_dir, "TEMPLATE")
        self.worker_cwd = os.path.join(self.tmpdir, "worker-repo")

        os.makedirs(self.rooms_dir, exist_ok=True)
        os.makedirs(self.handoffs_dir, exist_ok=True)
        os.makedirs(self.worker_cwd, exist_ok=True)

        # Copy real template
        real_template = os.path.join(
            os.path.dirname(os.path.dirname(__file__)),
            ".orchestrator", "rooms", "TEMPLATE"
        )
        if os.path.isdir(real_template):
            shutil.copytree(real_template, self.template_dir)
        else:
            # Fallback: create minimal template
            os.makedirs(self.template_dir, exist_ok=True)
            storage.write_state(
                os.path.join(self.template_dir, "state.yaml"),
                {
                    "room": {"id": "TEMPLATE"},
                    "context": {"execution_cwd": ""},
                    "lifecycle": {"current_phase": "triage"},
                    "discovery": {},
                },
            )
            with open(os.path.join(self.template_dir, "log.md"), "w") as f:
                f.write("# Room Log\n")

        # Patch storage paths
        self._orig = {
            "ORCHESTRATOR_DIR": storage.ORCHESTRATOR_DIR,
            "ROOMS_DIR": storage.ROOMS_DIR,
            "HANDOFFS_DIR": storage.HANDOFFS_DIR,
            "TEMPLATE_DIR": storage.TEMPLATE_DIR,
        }
        storage.ORCHESTRATOR_DIR = self.orch_dir
        storage.ROOMS_DIR = self.rooms_dir
        storage.HANDOFFS_DIR = self.handoffs_dir
        storage.TEMPLATE_DIR = self.template_dir

    def tearDown(self):
        storage.ORCHESTRATOR_DIR = self._orig["ORCHESTRATOR_DIR"]
        storage.ROOMS_DIR = self._orig["ROOMS_DIR"]
        storage.HANDOFFS_DIR = self._orig["HANDOFFS_DIR"]
        storage.TEMPLATE_DIR = self._orig["TEMPLATE_DIR"]
        shutil.rmtree(self.tmpdir, ignore_errors=True)


class TestPipelineSteps(PipelineTestBase):
    def test_step_create_room(self):
        pipeline._step_create_room("test-room", "Fix login bug", self.worker_cwd)

        state = storage.read_state(storage.room_state_path("test-room"))
        self.assertEqual(state["room"]["id"], "test-room")
        self.assertEqual(state["room"]["status"], "active")
        self.assertEqual(state["context"]["goal"], "Fix login bug")
        self.assertEqual(state["lifecycle"]["current_phase"], "execution")

    def test_step_set_room_memory(self):
        pipeline._step_create_room("test-room", "Fix login bug", self.worker_cwd)
        pipeline._step_set_room_memory("test-room", "Fix login bug", self.worker_cwd)

        state = storage.read_state(storage.room_state_path("test-room"))
        self.assertEqual(state["context"]["request_summary"], "Fix login bug")
        self.assertEqual(state["context"]["execution_cwd"], self.worker_cwd)

    def test_step_create_handoff(self):
        pipeline._step_create_room("test-room", "Fix login bug", self.worker_cwd)
        pipeline._step_create_handoff(
            "test-room-impl", "test-room", "worker-1", "Fix login bug", "medium"
        )

        state = storage.read_state(storage.handoff_path("test-room-impl"))
        self.assertEqual(state["handoff"]["id"], "test-room-impl")
        self.assertEqual(state["handoff"]["room_id"], "test-room")
        self.assertEqual(state["handoff"]["to"], "worker-1")
        self.assertEqual(state["handoff"]["status"], "open")

    def test_generated_ids_are_slug_safe(self):
        task_id = pipeline._generate_task_id()
        self.assertTrue(is_slug_safe(task_id), f"task_id '{task_id}' not slug-safe")

        handoff_id = f"{task_id}-impl"
        self.assertTrue(is_slug_safe(handoff_id), f"handoff_id '{handoff_id}' not slug-safe")


class TestPipelineIntegration(PipelineTestBase):
    def test_full_pipeline_creates_room_and_handoff(self):
        """Pipeline steps 1-3 create room + memory + handoff correctly.
        Step 4 (dispatch) is mocked because it needs tmux."""
        dispatch_called = []

        def fake_dispatch(args):
            dispatch_called.append(args.handoff_id)

        args = pipeline._SimpleNamespace(
            message="Fix the login bug",
            cwd=self.worker_cwd,
            peer="worker-1",
            priority="high",
        )

        with mock.patch.object(
            pipeline, "_generate_task_id", return_value="task-test-001"
        ), mock.patch(
            "lib.dispatch.cmd_handoff_dispatch",
            side_effect=fake_dispatch,
        ):
            pipeline.cmd_task_run(args)

        # Verify room was created
        state = storage.read_state(storage.room_state_path("task-test-001"))
        self.assertEqual(state["room"]["id"], "task-test-001")
        self.assertEqual(state["context"]["goal"], "Fix the login bug")
        self.assertEqual(state["context"]["execution_cwd"], self.worker_cwd)

        # Verify handoff was created
        ho = storage.read_state(storage.handoff_path("task-test-001-impl"))
        self.assertEqual(ho["handoff"]["to"], "worker-1")
        self.assertEqual(ho["handoff"]["priority"], "high")

        # Verify dispatch was called
        self.assertEqual(dispatch_called, ["task-test-001-impl"])

    def test_pipeline_fails_on_bad_cwd(self):
        args = pipeline._SimpleNamespace(
            message="Fix something",
            cwd="/nonexistent/dir/xyz",
            peer="worker-1",
            priority="medium",
        )
        with self.assertRaises(SystemExit) as ctx:
            pipeline.cmd_task_run(args)
        self.assertNotEqual(ctx.exception.code, 0)

    def test_pipeline_fails_on_empty_message(self):
        args = pipeline._SimpleNamespace(
            message="",
            cwd=self.worker_cwd,
            peer="worker-1",
            priority="medium",
        )
        with self.assertRaises(SystemExit) as ctx:
            pipeline.cmd_task_run(args)
        self.assertNotEqual(ctx.exception.code, 0)


class TestHandoffCreateNoPeerRequired(PipelineTestBase):
    """Verify handoff create no longer requires peer to exist in registry."""

    def test_handoff_create_with_nonexistent_peer_succeeds(self):
        pipeline._step_create_room("test-room", "Test", self.worker_cwd)
        # This should NOT fail even though "ghost-peer" is not in registry
        pipeline._step_create_handoff(
            "test-handoff", "test-room", "ghost-peer", "Test task", "medium"
        )
        ho = storage.read_state(storage.handoff_path("test-handoff"))
        self.assertEqual(ho["handoff"]["to"], "ghost-peer")


if __name__ == "__main__":
    unittest.main()
