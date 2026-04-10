"""Tests for peer auto-registration during dispatch.

Covers:
- Peer auto-created when missing from registry, using room's execution_cwd.
- Dispatch fails explicitly when room has no execution_cwd.
- Auto-created peer is persisted to peer_registry.yaml.
- Existing peer is not overwritten.
- os.getcwd() is never used as fallback for worker cwd.
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

from lib import dispatch, storage  # noqa: E402


class PeerAutoRegisterTestBase(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.mkdtemp(prefix="orch-peer-auto-")
        self.orch_dir = os.path.join(self.tmpdir, ".orchestrator")
        self.runtime_dir = os.path.join(self.orch_dir, "runtime")
        self.sessions_dir = os.path.join(self.runtime_dir, "sessions")
        self.locks_dir = os.path.join(self.runtime_dir, "locks")
        self.rooms_dir = os.path.join(self.orch_dir, "rooms")
        self.peer_registry = os.path.join(self.orch_dir, "peer_registry.yaml")
        self.worker_cwd = os.path.join(self.tmpdir, "worker-repo")

        os.makedirs(self.sessions_dir, exist_ok=True)
        os.makedirs(self.locks_dir, exist_ok=True)
        os.makedirs(self.rooms_dir, exist_ok=True)
        os.makedirs(self.worker_cwd, exist_ok=True)

        # Write empty peer registry
        storage.write_state(self.peer_registry, {"peers": []})

        # Patch storage paths
        self._orig = {
            "ORCHESTRATOR_DIR": storage.ORCHESTRATOR_DIR,
            "RUNTIME_DIR": storage.RUNTIME_DIR,
            "SESSIONS_DIR": storage.SESSIONS_DIR,
            "PEER_REGISTRY_PATH": storage.PEER_REGISTRY_PATH,
            "ROOMS_DIR": storage.ROOMS_DIR,
            "LOCKS_DIR": dispatch.LOCKS_DIR,
        }
        storage.ORCHESTRATOR_DIR = self.orch_dir
        storage.RUNTIME_DIR = self.runtime_dir
        storage.SESSIONS_DIR = self.sessions_dir
        storage.PEER_REGISTRY_PATH = self.peer_registry
        storage.ROOMS_DIR = self.rooms_dir
        dispatch.LOCKS_DIR = self.locks_dir

    def tearDown(self):
        storage.ORCHESTRATOR_DIR = self._orig["ORCHESTRATOR_DIR"]
        storage.RUNTIME_DIR = self._orig["RUNTIME_DIR"]
        storage.SESSIONS_DIR = self._orig["SESSIONS_DIR"]
        storage.PEER_REGISTRY_PATH = self._orig["PEER_REGISTRY_PATH"]
        storage.ROOMS_DIR = self._orig["ROOMS_DIR"]
        dispatch.LOCKS_DIR = self._orig["LOCKS_DIR"]
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def _room_state(self, execution_cwd=""):
        return {
            "room": {"id": "room-test"},
            "context": {"execution_cwd": execution_cwd},
            "lifecycle": {"current_phase": "execution"},
            "discovery": {},
        }


class TestEnsurePeerAutoCreate(PeerAutoRegisterTestBase):
    def test_auto_creates_peer_when_missing(self):
        room_state = self._room_state(execution_cwd=self.worker_cwd)
        peer, err = dispatch._ensure_peer("worker-1", room_state)

        self.assertIsNone(err)
        self.assertIsNotNone(peer)
        self.assertEqual(peer["id"], "worker-1")
        self.assertEqual(peer["type"], "worker")
        self.assertEqual(peer["cwd"], self.worker_cwd)

    def test_auto_created_peer_persisted_to_registry(self):
        room_state = self._room_state(execution_cwd=self.worker_cwd)
        dispatch._ensure_peer("worker-1", room_state)

        reg = storage.read_state(self.peer_registry)
        peers = reg.get("peers", [])
        self.assertEqual(len(peers), 1)
        self.assertEqual(peers[0]["id"], "worker-1")
        self.assertEqual(peers[0]["cwd"], self.worker_cwd)

    def test_fails_when_no_execution_cwd(self):
        room_state = self._room_state(execution_cwd="")
        peer, err = dispatch._ensure_peer("worker-1", room_state)

        self.assertIsNone(peer)
        self.assertIn("execution_cwd", err)

    def test_fails_when_execution_cwd_not_exists(self):
        room_state = self._room_state(execution_cwd="/nonexistent/path/xyz")
        peer, err = dispatch._ensure_peer("worker-1", room_state)

        self.assertIsNone(peer)
        self.assertIn("does not exist", err)

    def test_existing_peer_not_overwritten(self):
        # Pre-register peer with specific capabilities
        existing = {
            "id": "worker-1",
            "name": "Custom Worker",
            "type": "specialist",
            "cwd": "/some/other/path",
            "capabilities": ["python", "rust"],
            "status": "available",
        }
        storage.write_state(self.peer_registry, {"peers": [existing]})

        room_state = self._room_state(execution_cwd=self.worker_cwd)
        peer, err = dispatch._ensure_peer("worker-1", room_state)

        self.assertIsNone(err)
        self.assertEqual(peer["name"], "Custom Worker")
        self.assertEqual(peer["type"], "specialist")
        self.assertEqual(peer["cwd"], "/some/other/path")

    def test_no_getcwd_fallback(self):
        """Ensure _ensure_peer never uses os.getcwd() as fallback."""
        room_state = self._room_state(execution_cwd="")

        with mock.patch("os.getcwd", side_effect=AssertionError("os.getcwd() should not be called")):
            peer, err = dispatch._ensure_peer("worker-1", room_state)

        self.assertIsNone(peer)
        self.assertIn("execution_cwd", err)


class TestDispatchCwdNoFallback(PeerAutoRegisterTestBase):
    """Verify dispatch uses peer cwd, never os.getcwd()."""

    def test_dispatch_decision_uses_peer_cwd(self):
        # Register a peer with known cwd
        peer = {
            "id": "worker-1",
            "name": "Worker 1",
            "type": "worker",
            "cwd": self.worker_cwd,
            "capabilities": [],
            "status": "available",
        }
        storage.write_state(self.peer_registry, {"peers": [peer]})

        loaded = dispatch._load_peer_entry("worker-1")
        self.assertIsNotNone(loaded)
        self.assertEqual(loaded["cwd"], self.worker_cwd)


if __name__ == "__main__":
    unittest.main()
