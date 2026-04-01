"""Atomic YAML storage layer."""
import os
import tempfile
import yaml

ORCHESTRATOR_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), ".orchestrator")
ROOMS_DIR = os.path.join(ORCHESTRATOR_DIR, "rooms")
TEMPLATE_DIR = os.path.join(ROOMS_DIR, "TEMPLATE")
HANDOFFS_DIR = os.path.join(ORCHESTRATOR_DIR, "handoffs")
PEER_REGISTRY_PATH = os.path.join(ORCHESTRATOR_DIR, "peer_registry.yaml")


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


def room_dir(room_id: str) -> str:
    return os.path.join(ROOMS_DIR, room_id)


def room_state_path(room_id: str) -> str:
    return os.path.join(room_dir(room_id), "state.yaml")


def room_log_path(room_id: str) -> str:
    return os.path.join(room_dir(room_id), "log.md")


def handoff_path(handoff_id: str) -> str:
    return os.path.join(HANDOFFS_DIR, f"{handoff_id}.yaml")
