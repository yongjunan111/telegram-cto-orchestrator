"""Atomic YAML storage layer."""
import os
import tempfile
import yaml

ORCHESTRATOR_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), ".orchestrator")
ROOMS_DIR = os.path.join(ORCHESTRATOR_DIR, "rooms")
TEMPLATE_DIR = os.path.join(ROOMS_DIR, "TEMPLATE")
HANDOFFS_DIR = os.path.join(ORCHESTRATOR_DIR, "handoffs")
PEER_REGISTRY_PATH = os.path.join(ORCHESTRATOR_DIR, "peer_registry.yaml")
RUNTIME_DIR = os.path.join(ORCHESTRATOR_DIR, "runtime")
SESSIONS_DIR = os.path.join(RUNTIME_DIR, "sessions")


def session_path(session_id: str) -> str:
    return os.path.join(SESSIONS_DIR, f"{session_id}.yaml")


def now_iso() -> str:
    from datetime import datetime, timezone
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def read_state(path: str) -> dict:
    """Load YAML file. Returns empty dict if file is empty or missing."""
    with open(path, "r") as f:
        return yaml.safe_load(f) or {}


def write_state(path: str, state: dict) -> None:
    """Atomic write: temp file -> fsync -> rename."""
    dir_path = os.path.dirname(path)
    fd, tmp_path = tempfile.mkstemp(dir=dir_path, suffix=".yaml.tmp")
    try:
        with os.fdopen(fd, "w") as f:
            yaml.dump(state, f, default_flow_style=False, allow_unicode=True, sort_keys=False)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp_path, path)
    except:
        if os.path.exists(tmp_path):
            os.unlink(tmp_path)
        raise


def update_state(path: str, updates: dict) -> None:
    """Read, apply dot-path updates, atomic write back."""
    state = read_state(path)
    for dotkey, value in updates.items():
        parts = dotkey.split(".", 1)
        if len(parts) == 2:
            section, key = parts
            if section not in state:
                state[section] = {}
            state[section][key] = value
    write_state(path, state)


def append_log(path: str, entry: str) -> None:
    """Append text to a log file."""
    with open(path, "a") as f:
        f.write(entry)


def ensure_safe_runtime_dir(base_dir: str) -> str:
    """Ensure a runtime directory is safe to use: exists, is a directory,
    and is not a symlink. Create it if missing.

    Returns realpath(base_dir) on success.
    Raises ValueError if base_dir is empty or a symlink (checked before and
    after makedirs). Raises OSError if it cannot be created or is not a
    directory. Does NOT call sys.exit.

    Does NOT check runtime-root containment — that is the caller's
    responsibility. This helper is the shared dir-safety primitive used by
    non-file-write runtime artifacts (e.g. per-session lock files) that
    cannot route through safe_write_text.
    """
    if not base_dir:
        raise ValueError("base_dir must not be empty")
    if os.path.islink(base_dir):
        raise ValueError(f"base_dir '{base_dir}' is a symlink; refusing to follow")
    os.makedirs(base_dir, exist_ok=True)
    # Race defense: re-check after makedirs
    if os.path.islink(base_dir):
        raise ValueError(f"base_dir '{base_dir}' became a symlink; refusing to follow")
    if not os.path.isdir(base_dir):
        raise OSError(f"base_dir '{base_dir}' is not a directory")
    return os.path.realpath(base_dir)


def safe_write_text(base_dir: str, target_path: str, content: str) -> None:
    """Write text content to target_path, enforcing containment under base_dir.

    Rules:
    - base_dir must not be a symlink. If it exists as a symlink, refuse.
    - target_path must resolve inside base_dir (no escape via ../, symlinks, etc).
    - If target_path already exists as a symlink, refuse.
    - Any intermediate directory between base_dir and target_parent must not be a symlink.
    - Uses a temp file in the same directory + os.replace for atomic rename.
    - Raises OSError or ValueError on failure. Does NOT call sys.exit.
    - Callers are responsible for translating errors to command-level semantics.
    """
    import tempfile

    if not base_dir:
        raise ValueError("base_dir must not be empty")
    if not target_path:
        raise ValueError("target_path must not be empty")

    # Reject base_dir if it exists and is a symlink
    if os.path.islink(base_dir):
        raise ValueError(f"base_dir '{base_dir}' is a symlink; refusing to follow")

    os.makedirs(base_dir, exist_ok=True)

    # Race defense: check again after makedirs
    if os.path.islink(base_dir):
        raise ValueError(f"base_dir '{base_dir}' became a symlink; refusing to follow")

    if not os.path.isdir(base_dir):
        raise OSError(f"base_dir '{base_dir}' is not a directory")

    base_real = os.path.realpath(base_dir)
    if not os.path.isdir(base_real):
        raise OSError(f"base directory '{base_dir}' is not a directory after realpath resolution")

    # Determine target parent
    target_parent = os.path.dirname(target_path) or base_dir

    # If target_parent differs from base_dir logical path, ensure the whole chain
    # from base_dir down to target_parent does not include any symlink.
    if os.path.abspath(target_parent) != os.path.abspath(base_dir):
        _check_parent_chain_no_symlinks(base_dir, target_parent)

    os.makedirs(target_parent, exist_ok=True)

    # Double-check after makedirs: target_parent itself must not be a symlink
    if os.path.islink(target_parent):
        raise ValueError(f"target parent '{target_parent}' is a symlink; refusing to follow")

    target_parent_real = os.path.realpath(target_parent)

    # Parent must stay inside base
    if not (target_parent_real == base_real or target_parent_real.startswith(base_real + os.sep)):
        raise ValueError(
            f"target parent '{target_parent}' escapes base '{base_dir}'"
        )

    # If target already exists as a symlink, refuse.
    if os.path.islink(target_path):
        raise ValueError(f"target '{target_path}' is a symlink; refusing to follow")

    # Compute target basename and construct the final safe path under the real parent.
    target_basename = os.path.basename(target_path)
    if not target_basename or target_basename in (".", ".."):
        raise ValueError(f"invalid target filename: '{target_basename}'")

    final_path = os.path.join(target_parent_real, target_basename)

    # Defense-in-depth: if the file already exists, ensure it is a regular file.
    if os.path.exists(final_path) and not os.path.isfile(final_path):
        raise ValueError(f"target '{final_path}' exists and is not a regular file")

    # Write to a temp file in the same parent directory, then atomic rename.
    tmp_fd, tmp_path = tempfile.mkstemp(
        prefix=".{}.tmp.".format(target_basename),
        dir=target_parent_real,
    )
    try:
        with os.fdopen(tmp_fd, "w") as f:
            f.write(content)
        # Final containment check on the tmp path's parent
        tmp_parent_real = os.path.realpath(os.path.dirname(tmp_path))
        if not (tmp_parent_real == base_real or tmp_parent_real.startswith(base_real + os.sep)):
            raise ValueError(
                f"tmp parent '{tmp_parent_real}' escapes base '{base_dir}'"
            )
        os.replace(tmp_path, final_path)
        tmp_path = None
    finally:
        if tmp_path is not None and os.path.exists(tmp_path):
            try:
                os.remove(tmp_path)
            except OSError:
                pass


def _check_parent_chain_no_symlinks(base_dir: str, target_parent: str) -> None:
    """Walk from target_parent up to base_dir, rejecting any symlink in the chain.

    Raises ValueError if any intermediate component is a symlink.
    """
    base_abs = os.path.abspath(base_dir)
    current = os.path.abspath(target_parent)

    # Walk up until we reach base_abs or hit filesystem root
    visited = set()
    while current != base_abs:
        if current in visited:
            raise ValueError(f"loop detected while walking parent chain from '{target_parent}' to '{base_dir}'")
        visited.add(current)

        if os.path.islink(current):
            raise ValueError(f"intermediate directory '{current}' is a symlink; refusing to follow")

        parent = os.path.dirname(current)
        if parent == current:
            # Reached filesystem root without finding base_abs
            raise ValueError(f"target parent '{target_parent}' is not under base '{base_dir}'")
        current = parent


def room_dir(room_id: str) -> str:
    return os.path.join(ROOMS_DIR, room_id)


def room_state_path(room_id: str) -> str:
    return os.path.join(room_dir(room_id), "state.yaml")


def room_log_path(room_id: str) -> str:
    return os.path.join(room_dir(room_id), "log.md")


def handoff_path(handoff_id: str) -> str:
    return os.path.join(HANDOFFS_DIR, f"{handoff_id}.yaml")
