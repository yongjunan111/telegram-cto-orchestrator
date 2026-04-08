"""Tests for reuse race hardening in lib.dispatch._execute_reuse_dispatch.

These tests use a temporary runtime directory and monkey-patch tmux helpers
so they never touch real .orchestrator/ state or real tmux. They drive
state drift directly via the file system to simulate what a concurrent
dispatch would do.
"""
import os
import shutil
import sys
import tempfile
import unittest
from unittest import mock

# Make repo root importable when running via `python -m unittest`
_REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), os.pardir))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

from lib import dispatch, storage  # noqa: E402


def _build_handoff_state(handoff_id="hf-test", room_id="room-test"):
    return {
        "handoff": {
            "id": handoff_id,
            "room_id": room_id,
            "to": "peer-test",
            "status": "open",
        }
    }


def _build_room_state(room_id="room-test"):
    return {
        "room": {"id": room_id},
        "lifecycle": {"current_phase": "execution"},
        "memory": {},
        "contract": {},
        "discovery": {},
    }


def _build_session_state(
    session_id="sess-test",
    peer_id="peer-test",
    tmux_name="orch-peer-test-sess",
    room_id="",
    handoff_id="",
    status="idle",
    dirty=False,
    reuse_count=0,
):
    return {
        "session": {
            "id": session_id,
            "peer_id": peer_id,
            "tmux_session": tmux_name,
            "mode": "ephemeral",
            "status": status,
            "room_id": room_id,
            "handoff_id": handoff_id,
            "cwd": "/tmp",
            "branch": None,
            "dirty": dirty,
            "reuse_count": reuse_count,
            "heartbeat_at": "2026-04-08T00:00:00Z",
            "lease_until": "2099-12-31T00:00:00Z",
            "last_active_at": "2026-04-08T00:00:00Z",
        }
    }


class ReuseRaceTestBase(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.mkdtemp(prefix="orch-reuse-race-")
        self.runtime_dir = os.path.join(self.tmpdir, "runtime")
        self.sessions_dir = os.path.join(self.runtime_dir, "sessions")
        self.locks_dir = os.path.join(self.runtime_dir, "locks")
        os.makedirs(self.sessions_dir, exist_ok=True)
        os.makedirs(self.locks_dir, exist_ok=True)

        # Patch storage paths so we never touch real .orchestrator/
        self._orig_runtime_dir = storage.RUNTIME_DIR
        self._orig_sessions_dir = storage.SESSIONS_DIR
        storage.RUNTIME_DIR = self.runtime_dir
        storage.SESSIONS_DIR = self.sessions_dir

        # Patch dispatch's LOCKS_DIR (computed at module import time)
        self._orig_locks_dir = dispatch.LOCKS_DIR
        dispatch.LOCKS_DIR = self.locks_dir

        # Records for side-effect calls
        self.tmux_exists_return = True
        self.tmux_send_keys_calls = []
        self.tmux_kill_calls = []
        self.inject_calls = []
        self.bootstrap_calls = []
        self.write_artifact_calls = []

        self._patches = [
            mock.patch.object(
                dispatch,
                "_tmux_session_exists",
                side_effect=lambda name: self.tmux_exists_return,
            ),
            mock.patch.object(
                dispatch,
                "_tmux_send_keys",
                side_effect=lambda name, keys: self.tmux_send_keys_calls.append((name, keys)),
            ),
            mock.patch.object(
                dispatch,
                "_tmux_kill_session",
                side_effect=lambda name: self.tmux_kill_calls.append(name),
            ),
            mock.patch.object(
                dispatch,
                "_inject_session_hooks",
                side_effect=lambda *a, **k: self.inject_calls.append((a, k)),
            ),
            mock.patch.object(
                dispatch,
                "_run_bootstrap_and_display",
                side_effect=lambda *a, **k: self.bootstrap_calls.append((a, k)),
            ),
            mock.patch.object(
                dispatch,
                "_write_dispatch_artifact",
                side_effect=lambda *a, **k: (
                    self.write_artifact_calls.append((a, k))
                    or os.path.join(self.runtime_dir, "dispatches", "fake.md")
                ),
            ),
        ]
        for p in self._patches:
            p.start()

    def tearDown(self):
        for p in self._patches:
            p.stop()
        storage.RUNTIME_DIR = self._orig_runtime_dir
        storage.SESSIONS_DIR = self._orig_sessions_dir
        dispatch.LOCKS_DIR = self._orig_locks_dir
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def _write_disk_state(self, sess_state):
        sid = sess_state["session"]["id"]
        path = storage.session_path(sid)
        storage.write_state(path, sess_state)
        return path

    def _read_disk_state(self, session_id):
        return storage.read_state(storage.session_path(session_id))

    def _call_reuse(
        self,
        chosen_session,
        handoff_id="hf-test",
        room_id="room-test",
        target_peer="peer-test",
    ):
        return dispatch._execute_reuse_dispatch(
            handoff_state=_build_handoff_state(handoff_id, room_id),
            room_state=_build_room_state(room_id),
            chosen_session=chosen_session,
            target_peer=target_peer,
            handoff_id=handoff_id,
            handoff_room=room_id,
            handoff_kind="implementation",
            now="2026-04-08T01:00:00Z",
            lease_until="2099-12-31T01:00:00Z",
        )


class TestReuseHappyPath(ReuseRaceTestBase):
    def test_happy_path_reuse_succeeds(self):
        snap = _build_session_state()
        self._write_disk_state(snap)

        result = self._call_reuse(snap)

        self.assertTrue(result["ok"], msg=f"reuse should succeed: {result}")
        self.assertEqual(result["session_id"], "sess-test")
        self.assertEqual(result["tmux_session"], "orch-peer-test-sess")

        disk = self._read_disk_state("sess-test")["session"]
        self.assertEqual(disk["status"], "busy")
        self.assertEqual(disk["handoff_id"], "hf-test")
        self.assertEqual(disk["room_id"], "room-test")
        self.assertEqual(disk["reuse_count"], 1)

        # Lock released after success
        lock_path = dispatch._session_lock_path("sess-test")
        self.assertFalse(
            os.path.exists(lock_path),
            f"lock file should be released after success: {lock_path}",
        )

    def test_happy_path_calls_artifact_inject_bootstrap(self):
        snap = _build_session_state()
        self._write_disk_state(snap)

        result = self._call_reuse(snap)
        self.assertTrue(result["ok"])
        self.assertEqual(len(self.write_artifact_calls), 1)
        self.assertEqual(len(self.inject_calls), 1)
        self.assertEqual(len(self.bootstrap_calls), 1)


class TestReuseRevalidationFailures(ReuseRaceTestBase):
    def test_reuse_fails_when_state_changed_to_busy(self):
        snap = _build_session_state()
        self._write_disk_state(snap)
        # Drift: another process flipped status to busy between decision and execution
        drifted = _build_session_state(status="busy")
        self._write_disk_state(drifted)

        result = self._call_reuse(snap)
        self.assertFalse(result["ok"])
        self.assertIn("eligible", result["error"])
        self.assertIn("busy", result["error"])

        # On-disk state must be unchanged from what the simulated other process wrote
        disk = self._read_disk_state("sess-test")["session"]
        self.assertEqual(disk["status"], "busy")
        self.assertEqual(disk["reuse_count"], 0)
        self.assertEqual(disk["handoff_id"], "")

        # No side effects
        self.assertEqual(self.write_artifact_calls, [])
        self.assertEqual(self.inject_calls, [])
        self.assertEqual(self.bootstrap_calls, [])
        self.assertEqual(self.tmux_send_keys_calls, [])

        # Lock released even on failure
        self.assertFalse(os.path.exists(dispatch._session_lock_path("sess-test")))

    def test_reuse_fails_when_state_went_dirty(self):
        snap = _build_session_state()
        self._write_disk_state(snap)
        drifted = _build_session_state(dirty=True)
        self._write_disk_state(drifted)

        result = self._call_reuse(snap)
        self.assertFalse(result["ok"])
        self.assertIn("dirty", result["error"])

        self.assertEqual(self.write_artifact_calls, [])
        self.assertEqual(self.inject_calls, [])
        self.assertEqual(self.bootstrap_calls, [])
        self.assertFalse(os.path.exists(dispatch._session_lock_path("sess-test")))

    def test_reuse_fails_when_tmux_dies(self):
        snap = _build_session_state()
        self._write_disk_state(snap)
        # Patch tmux exists to return False
        self.tmux_exists_return = False

        result = self._call_reuse(snap)
        self.assertFalse(result["ok"])
        self.assertIn("tmux", result["error"])

        disk = self._read_disk_state("sess-test")["session"]
        self.assertEqual(disk["status"], "idle")
        self.assertEqual(disk["reuse_count"], 0)
        self.assertEqual(self.write_artifact_calls, [])
        self.assertEqual(self.inject_calls, [])
        self.assertEqual(self.bootstrap_calls, [])
        self.assertFalse(os.path.exists(dispatch._session_lock_path("sess-test")))

    def test_reuse_fails_when_handoff_id_changed(self):
        snap = _build_session_state()
        self._write_disk_state(snap)
        drifted = _build_session_state(handoff_id="hf-other")
        self._write_disk_state(drifted)

        result = self._call_reuse(snap)
        self.assertFalse(result["ok"])
        self.assertIn("hf-other", result["error"])

        disk = self._read_disk_state("sess-test")["session"]
        self.assertEqual(disk["handoff_id"], "hf-other")
        self.assertEqual(disk["status"], "idle")
        self.assertEqual(disk["reuse_count"], 0)
        self.assertEqual(self.write_artifact_calls, [])
        self.assertEqual(self.inject_calls, [])
        self.assertEqual(self.bootstrap_calls, [])

    def test_reuse_fails_when_session_file_disappears(self):
        snap = _build_session_state()
        self._write_disk_state(snap)
        os.remove(storage.session_path("sess-test"))

        result = self._call_reuse(snap)
        self.assertFalse(result["ok"])
        self.assertIn("disappeared", result["error"])
        self.assertEqual(self.write_artifact_calls, [])
        self.assertEqual(self.inject_calls, [])
        self.assertEqual(self.bootstrap_calls, [])

    def test_reuse_fails_when_tmux_session_repointed(self):
        snap = _build_session_state(tmux_name="orch-peer-test-sess")
        self._write_disk_state(snap)
        # Drift: tmux_session field re-pointed to a different name
        drifted = _build_session_state(tmux_name="orch-peer-test-other")
        self._write_disk_state(drifted)

        result = self._call_reuse(snap)
        self.assertFalse(result["ok"])
        self.assertIn("tmux_session changed", result["error"])
        self.assertEqual(self.write_artifact_calls, [])
        self.assertEqual(self.inject_calls, [])

    def test_failed_revalidation_releases_lock(self):
        snap = _build_session_state()
        self._write_disk_state(snap)
        drifted = _build_session_state(status="busy")
        self._write_disk_state(drifted)

        # Sanity: make sure no lock exists before the call
        lock_path = dispatch._session_lock_path("sess-test")
        self.assertFalse(os.path.exists(lock_path))

        result = self._call_reuse(snap)
        self.assertFalse(result["ok"])
        # The lock acquired inside _execute_reuse_dispatch must be released in
        # the finally block, even though revalidation failed.
        self.assertFalse(
            os.path.exists(lock_path),
            "lock must be released even on revalidation failure",
        )


class TestReuseLockPathSafety(ReuseRaceTestBase):
    def test_locks_dir_symlink_refused(self):
        snap = _build_session_state()
        self._write_disk_state(snap)

        # Replace the pre-created locks dir with a symlink pointing outside
        victim_dir = os.path.join(self.tmpdir, "victim-locks")
        os.makedirs(victim_dir, exist_ok=True)
        shutil.rmtree(self.locks_dir)
        os.symlink(victim_dir, self.locks_dir)
        self.assertTrue(os.path.islink(self.locks_dir))

        result = self._call_reuse(snap)
        self.assertFalse(result["ok"])
        self.assertIn("symlink", result["error"])

        # Victim dir must remain empty — no lock file escaped
        self.assertEqual(os.listdir(victim_dir), [])

        # No state mutation, no side effects
        disk = self._read_disk_state("sess-test")["session"]
        self.assertEqual(disk["status"], "idle")
        self.assertEqual(disk["reuse_count"], 0)
        self.assertEqual(self.write_artifact_calls, [])
        self.assertEqual(self.inject_calls, [])
        self.assertEqual(self.bootstrap_calls, [])

    def test_lock_path_symlink_refused(self):
        snap = _build_session_state()
        self._write_disk_state(snap)

        # Create a victim file outside the runtime tree, then symlink the
        # lock_path to it. If _acquire_session_lock followed the symlink, the
        # victim file would get clobbered.
        victim_file = os.path.join(self.tmpdir, "victim.txt")
        with open(victim_file, "w") as f:
            f.write("original\n")
        lock_path = dispatch._session_lock_path("sess-test")
        os.symlink(victim_file, lock_path)
        self.assertTrue(os.path.islink(lock_path))

        result = self._call_reuse(snap)
        self.assertFalse(result["ok"])
        self.assertIn("symlink", result["error"])

        # Victim file contents unchanged
        with open(victim_file) as f:
            self.assertEqual(f.read(), "original\n")

        # Symlink still there (we did not remove it; operator would)
        self.assertTrue(os.path.islink(lock_path))

        # No state mutation, no side effects
        disk = self._read_disk_state("sess-test")["session"]
        self.assertEqual(disk["status"], "idle")
        self.assertEqual(disk["reuse_count"], 0)
        self.assertEqual(self.write_artifact_calls, [])
        self.assertEqual(self.inject_calls, [])
        self.assertEqual(self.bootstrap_calls, [])

    def test_payload_write_failure_cleans_up(self):
        snap = _build_session_state()
        self._write_disk_state(snap)

        # Monkey-patch os.write to simulate a write failure (e.g. ENOSPC).
        # Scope the patch to only the _call_reuse call to avoid affecting the
        # surrounding assertion/infrastructure code.
        with mock.patch("os.write", side_effect=OSError("simulated ENOSPC")):
            result = self._call_reuse(snap)

        self.assertFalse(result["ok"])
        self.assertIn("payload write failed", result["error"])

        # No stale lock file on disk — _acquire_session_lock must clean up
        # the partially-created lock file when the payload write fails.
        lock_path = dispatch._session_lock_path("sess-test")
        self.assertFalse(
            os.path.exists(lock_path),
            f"lock file must be cleaned up after payload write failure: {lock_path}",
        )

        # No state mutation, no side effects
        disk = self._read_disk_state("sess-test")["session"]
        self.assertEqual(disk["status"], "idle")
        self.assertEqual(disk["reuse_count"], 0)
        self.assertEqual(self.write_artifact_calls, [])
        self.assertEqual(self.inject_calls, [])
        self.assertEqual(self.bootstrap_calls, [])


class TestReuseLockHeld(ReuseRaceTestBase):
    def test_reuse_fails_when_lock_already_held(self):
        snap = _build_session_state()
        self._write_disk_state(snap)
        # Pre-create the lock file (simulates another process holding it)
        lock_path = dispatch._session_lock_path("sess-test")
        with open(lock_path, "w") as f:
            f.write("pid=99999 ts=fake\n")
        self.assertTrue(os.path.exists(lock_path))

        result = self._call_reuse(snap)
        self.assertFalse(result["ok"])
        self.assertIn("lock", result["error"].lower())

        # No state mutation
        disk = self._read_disk_state("sess-test")["session"]
        self.assertEqual(disk["status"], "idle")
        self.assertEqual(disk["reuse_count"], 0)
        self.assertEqual(disk["handoff_id"], "")

        # No side effects
        self.assertEqual(self.write_artifact_calls, [])
        self.assertEqual(self.inject_calls, [])
        self.assertEqual(self.bootstrap_calls, [])
        self.assertEqual(self.tmux_send_keys_calls, [])

        # Pre-existing lock file must still be there (we did not acquire it,
        # so we must not delete it).
        self.assertTrue(
            os.path.exists(lock_path),
            "pre-existing lock file must not be removed by failed reuse",
        )


if __name__ == "__main__":
    unittest.main()
