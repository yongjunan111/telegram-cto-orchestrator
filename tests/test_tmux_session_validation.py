"""Tests for `session upsert --tmux-session` CLI boundary validation.

The tmux_session field eventually flows into tmux subprocess calls at dispatch
time. The upsert CLI must reject structurally unsafe names BEFORE any state
file is created or modified, so a stray operator/script invocation cannot
persist a value containing shell metacharacters, whitespace, or empty strings
into runtime/sessions/<id>.yaml.

Also covers the `_tmux_session_exists` narrow prefilter in lib/dispatch.py:
unsafe names must be rejected before spawning a tmux subprocess at all.
"""
import argparse
import os
import shutil
import sys
import tempfile
import unittest

_REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), os.pardir))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

from lib import sessions, storage, dispatch  # noqa: E402


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


class SessionUpsertTmuxSessionTestBase(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.mkdtemp(prefix="orch-upsert-tmux-session-")
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


class TestSessionUpsertTmuxSessionValid(SessionUpsertTmuxSessionTestBase):
    def test_valid_name_with_hyphens_succeeds(self):
        args = _make_args("sess-test", tmux_session="orch-worker-a-founda")
        sessions.cmd_session_upsert(args)
        state = storage.read_state(self._state_path("sess-test"))
        self.assertEqual(state["session"]["tmux_session"], "orch-worker-a-founda")

    def test_valid_name_with_underscore_succeeds(self):
        args = _make_args("sess-test", tmux_session="orch_worker_1")
        sessions.cmd_session_upsert(args)
        state = storage.read_state(self._state_path("sess-test"))
        self.assertEqual(state["session"]["tmux_session"], "orch_worker_1")

    def test_valid_digits_only_succeeds(self):
        args = _make_args("sess-test", tmux_session="123456")
        sessions.cmd_session_upsert(args)
        state = storage.read_state(self._state_path("sess-test"))
        self.assertEqual(state["session"]["tmux_session"], "123456")


class TestSessionUpsertTmuxSessionInvalid(SessionUpsertTmuxSessionTestBase):
    def _assert_rejects(self, bad_value):
        args = _make_args("sess-test", tmux_session=bad_value)
        with self.assertRaises(SystemExit) as ctx:
            sessions.cmd_session_upsert(args)
        self.assertEqual(ctx.exception.code, 1)
        # Authoritative state must NOT be created or modified
        self.assertFalse(
            os.path.isfile(self._state_path("sess-test")),
            f"state file should not exist after rejecting tmux_session={bad_value!r}",
        )

    def test_empty_rejected(self):
        self._assert_rejects("")

    def test_shell_metacharacters_rejected(self):
        self._assert_rejects("foo;rm -rf /")

    def test_backtick_rejected(self):
        self._assert_rejects("foo`whoami`")

    def test_dollar_paren_rejected(self):
        self._assert_rejects("foo$(id)")

    def test_whitespace_rejected(self):
        self._assert_rejects("foo bar")

    def test_leading_space_rejected(self):
        self._assert_rejects(" foo")

    def test_dot_rejected(self):
        self._assert_rejects("foo.bar")

    def test_slash_rejected(self):
        self._assert_rejects("foo/bar")

    def test_colon_rejected(self):
        self._assert_rejects("foo:bar")

    def test_pre_existing_state_not_clobbered_on_invalid(self):
        # Seed a valid tmux_session.
        sessions.cmd_session_upsert(_make_args("sess-test", tmux_session="orch-foo"))
        seed = storage.read_state(self._state_path("sess-test"))
        self.assertEqual(seed["session"]["tmux_session"], "orch-foo")

        # Invalid upsert must not touch the file.
        with self.assertRaises(SystemExit):
            sessions.cmd_session_upsert(_make_args("sess-test", tmux_session="foo;bar"))

        post = storage.read_state(self._state_path("sess-test"))
        self.assertEqual(post["session"]["tmux_session"], "orch-foo")


class TestTmuxSessionExistsPrefilter(unittest.TestCase):
    """`_tmux_session_exists` must refuse to spawn a tmux subprocess for names
    that are structurally unsafe. These values can only reach the helper from
    an on-disk YAML that was created before CLI validation existed, or via
    direct tampering — either way we short-circuit to 'does not exist'.
    """

    def test_unsafe_name_returns_false_without_subprocess(self):
        calls = []
        orig_run = dispatch.subprocess.run

        def _spy(*args, **kwargs):
            calls.append(args)
            return orig_run(*args, **kwargs)

        dispatch.subprocess.run = _spy
        try:
            self.assertFalse(dispatch._tmux_session_exists("foo;rm -rf /"))
            self.assertFalse(dispatch._tmux_session_exists(""))
            self.assertFalse(dispatch._tmux_session_exists("foo bar"))
            self.assertFalse(dispatch._tmux_session_exists("foo$(id)"))
            # No subprocess call should have been made for any of the unsafe names.
            self.assertEqual(
                calls, [],
                f"expected no subprocess calls for unsafe names, got {calls!r}",
            )
        finally:
            dispatch.subprocess.run = orig_run

    def test_safe_name_proceeds_to_subprocess(self):
        observed = []

        class _Result:
            returncode = 1

        def _fake_run(argv, **kwargs):
            observed.append(argv)
            return _Result()

        orig_run = dispatch.subprocess.run
        dispatch.subprocess.run = _fake_run
        try:
            # Safe name: subprocess IS invoked (returncode=1 => does not exist).
            result = dispatch._tmux_session_exists("orch-worker-a")
            self.assertFalse(result)
            self.assertEqual(len(observed), 1)
            self.assertEqual(observed[0][0], "tmux")
            self.assertEqual(observed[0][1], "has-session")
            self.assertEqual(observed[0][3], "orch-worker-a")
        finally:
            dispatch.subprocess.run = orig_run


if __name__ == "__main__":
    unittest.main()
