"""Tests for `session upsert --tmux-target` CLI boundary validation.

The tmux_target field is now a structural concern of authoritative session
state. The upsert CLI must reject invalid values BEFORE any state file is
created or modified, so a stray operator/script invocation cannot persist
a structurally invalid tmux_target.
"""
import argparse
import os
import shutil
import sys
import tempfile
import unittest

# Make repo root importable when running via `python -m unittest`
_REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), os.pardir))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

from lib import sessions, storage  # noqa: E402


def _make_args(session_id, **overrides):
    """Build an argparse-style Namespace with all upsert fields defaulted to
    None, except those overridden by the caller. Mirrors the orchctl parser
    so cmd_session_upsert can be invoked directly without going through CLI."""
    base = dict(
        session_id=session_id,
        peer_id=None,
        tmux_session=None,
        tmux_target=None,
        mode=None,
        status=None,
        room_id=None,
        handoff_id=None,
        cwd=None,
        branch=None,
        dirty=None,
        reuse_count=None,
        heartbeat_at=None,
        lease_until=None,
        last_active_at=None,
    )
    base.update(overrides)
    return argparse.Namespace(**base)


class SessionUpsertTargetTestBase(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.mkdtemp(prefix="orch-upsert-target-")
        self.runtime_dir = os.path.join(self.tmpdir, "runtime")
        self.sessions_dir = os.path.join(self.runtime_dir, "sessions")
        os.makedirs(self.sessions_dir, exist_ok=True)

        self._orig_runtime_dir = storage.RUNTIME_DIR
        self._orig_sessions_dir = storage.SESSIONS_DIR
        storage.RUNTIME_DIR = self.runtime_dir
        storage.SESSIONS_DIR = self.sessions_dir

    def tearDown(self):
        storage.RUNTIME_DIR = self._orig_runtime_dir
        storage.SESSIONS_DIR = self._orig_sessions_dir
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def _state_path(self, session_id):
        return storage.session_path(session_id)


class TestSessionUpsertTmuxTargetValid(SessionUpsertTargetTestBase):
    def test_valid_pane_id_succeeds(self):
        args = _make_args("sess-test", tmux_target="%12")
        sessions.cmd_session_upsert(args)
        path = self._state_path("sess-test")
        self.assertTrue(os.path.isfile(path))
        state = storage.read_state(path)
        self.assertEqual(state["session"]["tmux_target"], "%12")

    def test_valid_large_pane_id_succeeds(self):
        args = _make_args("sess-test", tmux_target="%9999")
        sessions.cmd_session_upsert(args)
        state = storage.read_state(self._state_path("sess-test"))
        self.assertEqual(state["session"]["tmux_target"], "%9999")

    def test_valid_zero_pane_id_succeeds(self):
        args = _make_args("sess-test", tmux_target="%0")
        sessions.cmd_session_upsert(args)
        state = storage.read_state(self._state_path("sess-test"))
        self.assertEqual(state["session"]["tmux_target"], "%0")


class TestSessionUpsertTmuxTargetInvalid(SessionUpsertTargetTestBase):
    def _assert_rejects(self, bad_value):
        args = _make_args("sess-test", tmux_target=bad_value)
        with self.assertRaises(SystemExit) as ctx:
            sessions.cmd_session_upsert(args)
        self.assertEqual(ctx.exception.code, 1)
        # Authoritative state must NOT be created or modified
        self.assertFalse(
            os.path.isfile(self._state_path("sess-test")),
            f"state file should not exist after rejecting tmux_target={bad_value!r}",
        )

    def test_plain_word_rejected(self):
        self._assert_rejects("foo")

    def test_shell_metacharacters_rejected(self):
        self._assert_rejects("abc;rm -rf /")

    def test_bare_percent_rejected(self):
        self._assert_rejects("%")

    def test_percent_letters_rejected(self):
        self._assert_rejects("%abc")

    def test_pane_dash_number_rejected(self):
        self._assert_rejects("pane-1")

    def test_empty_string_rejected(self):
        self._assert_rejects("")

    def test_whitespace_padded_rejected(self):
        self._assert_rejects(" %12 ")

    def test_pre_existing_state_not_clobbered_on_invalid(self):
        # Pre-create a session state with a valid tmux_target. An invalid
        # upsert call must NOT touch this file.
        args_seed = _make_args("sess-test", tmux_target="%5")
        sessions.cmd_session_upsert(args_seed)
        seed_state = storage.read_state(self._state_path("sess-test"))
        self.assertEqual(seed_state["session"]["tmux_target"], "%5")

        bad_args = _make_args("sess-test", tmux_target="garbage")
        with self.assertRaises(SystemExit):
            sessions.cmd_session_upsert(bad_args)

        # Original state preserved
        post_state = storage.read_state(self._state_path("sess-test"))
        self.assertEqual(post_state["session"]["tmux_target"], "%5")


class TestSessionUpsertOtherFieldsUnaffected(SessionUpsertTargetTestBase):
    def test_upsert_without_tmux_target_still_works(self):
        # Sanity / regression: upsert with only --tmux-session must not be
        # affected by the new tmux_target validator.
        args = _make_args("sess-test", tmux_session="orch-foo")
        sessions.cmd_session_upsert(args)
        state = storage.read_state(self._state_path("sess-test"))
        self.assertEqual(state["session"]["tmux_session"], "orch-foo")
        self.assertNotIn("tmux_target", state["session"])

    def test_upsert_combined_valid_target_and_other_fields(self):
        args = _make_args(
            "sess-test",
            tmux_session="orch-foo",
            tmux_target="%7",
            mode="ephemeral",
        )
        sessions.cmd_session_upsert(args)
        state = storage.read_state(self._state_path("sess-test"))
        self.assertEqual(state["session"]["tmux_session"], "orch-foo")
        self.assertEqual(state["session"]["tmux_target"], "%7")
        self.assertEqual(state["session"]["mode"], "ephemeral")


if __name__ == "__main__":
    unittest.main()
