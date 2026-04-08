"""ID validation and referential integrity."""
import os
import re
import sys

from . import storage

# Slug: lowercase letters, digits, hyphens. 1-64 chars. No leading/trailing hyphen.
_SLUG_RE = re.compile(r'^[a-z0-9](?:[a-z0-9-]{0,62}[a-z0-9])?$')

# tmux pane target: pane id format `%N` where N is one or more digits.
# Mirrors lib/dispatch.py's _TMUX_TARGET_RE — both must stay in sync.
_TMUX_TARGET_RE = re.compile(r'^%[0-9]+$')


def is_slug_safe(value: str) -> bool:
    """Return True if value matches the slug pattern, False otherwise."""
    return bool(value) and bool(_SLUG_RE.match(value))


def validate_slug(value: str, label: str) -> None:
    """Exit with error if value is not a valid slug."""
    if not _SLUG_RE.match(value):
        print(
            f"Error: {label} '{value}' is invalid. "
            f"Use lowercase letters, digits, and hyphens (1-64 chars, no leading/trailing hyphen).",
            file=sys.stderr,
        )
        sys.exit(1)


def validate_tmux_target(value: str, label: str) -> None:
    """Exit with error if value is not a safe tmux pane target.

    A safe tmux pane target is `%N` where N is one or more digits (e.g. `%12`).
    Anything else — slugs, shell metacharacters, bare `%`, `%abc` — is rejected
    at the CLI boundary so authoritative session state never holds a structurally
    invalid tmux_target.
    """
    if not value or not _TMUX_TARGET_RE.match(value):
        print(
            f"Error: {label} '{value}' is invalid. "
            f"Use the tmux pane id format '%N' where N is one or more digits "
            f"(e.g. '%12').",
            file=sys.stderr,
        )
        sys.exit(1)


def require_room(room_id: str) -> None:
    """Exit if room doesn't exist or has no valid state.yaml."""
    validate_slug(room_id, "room_id")
    d = storage.room_dir(room_id)
    sp = storage.room_state_path(room_id)
    if not os.path.isdir(d):
        print(f"Error: room '{room_id}' does not exist.", file=sys.stderr)
        sys.exit(1)
    if not os.path.isfile(sp):
        print(f"Error: room '{room_id}' has no state.yaml.", file=sys.stderr)
        sys.exit(1)
    # Validate YAML is loadable
    try:
        state = storage.read_state(sp)
        if not isinstance(state, dict) or "room" not in state:
            raise ValueError("missing 'room' section")
    except Exception as e:
        print(f"Error: room '{room_id}' has invalid state.yaml: {e}", file=sys.stderr)
        sys.exit(1)


def require_handoff(handoff_id: str) -> None:
    """Exit if handoff doesn't exist."""
    validate_slug(handoff_id, "handoff_id")
    p = storage.handoff_path(handoff_id)
    if not os.path.isfile(p):
        print(f"Error: handoff '{handoff_id}' does not exist.", file=sys.stderr)
        sys.exit(1)


def require_peer(peer_id: str) -> None:
    """Exit if peer_id is not in peer_registry.yaml."""
    validate_slug(peer_id, "peer_id")
    if not os.path.isfile(storage.PEER_REGISTRY_PATH):
        print(f"Error: peer registry not found.", file=sys.stderr)
        sys.exit(1)
    reg = storage.read_state(storage.PEER_REGISTRY_PATH)
    peers = reg.get("peers") or []
    known_ids = {p.get("id") for p in peers if isinstance(p, dict)}
    if peer_id not in known_ids:
        print(f"Error: peer '{peer_id}' not found in peer_registry.yaml.", file=sys.stderr)
        sys.exit(1)


VALID_SESSION_MODES = {"ephemeral", "warm", "persistent"}
VALID_SESSION_STATUSES = {"offline", "idle", "busy"}


def require_session(session_id: str) -> None:
    """Exit if session does not exist."""
    validate_slug(session_id, "session_id")
    path = storage.session_path(session_id)
    if not os.path.isfile(path):
        print(f"Error: session '{session_id}' does not exist.", file=sys.stderr)
        sys.exit(1)
