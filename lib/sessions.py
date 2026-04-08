"""Session command logic — runtime/session authoritative state."""
import os
import sys

from . import storage
from .validators import (
    validate_slug, validate_tmux_target, require_session,
    require_peer, require_room, require_handoff,
    VALID_SESSION_MODES, VALID_SESSION_STATUSES,
)


def _parse_bool(val: str) -> bool:
    if val.lower() in ("true", "yes", "1"):
        return True
    if val.lower() in ("false", "no", "0"):
        return False
    raise ValueError(f"Invalid boolean: {val}")


def cmd_session_list(args):
    if not os.path.isdir(storage.SESSIONS_DIR):
        print("No sessions found.")
        return

    files = sorted(
        f for f in os.listdir(storage.SESSIONS_DIR)
        if f.endswith(".yaml") and f != ".gitkeep"
    )
    if not files:
        print("No sessions found.")
        return

    fmt = "{:<20} {:<16} {:<10} {:<8} {:<16} {:<20} {:<6} {:<10} {:<22}"
    print(fmt.format("ID", "PEER", "MODE", "STATUS", "ROOM", "HANDOFF", "DIRTY", "TARGET", "LEASE_UNTIL"))
    print("-" * 132)

    for fname in files:
        path = os.path.join(storage.SESSIONS_DIR, fname)
        try:
            state = storage.read_state(path)
            s = state.get("session", {})
            sid = str(s.get("id") or fname[:-5])[:19]
            peer = str(s.get("peer_id") or "")[:15]
            mode = str(s.get("mode") or "")[:9]
            status = str(s.get("status") or "")[:7]
            room = str(s.get("room_id") or "")[:15]
            handoff = str(s.get("handoff_id") or "")[:19]
            dirty = "yes" if s.get("dirty") else "no"
            target = str(s.get("tmux_target") or "")[:9]
            lease = str(s.get("lease_until") or "")[:21]
            print(fmt.format(sid, peer, mode, status, room, handoff, dirty, target, lease))
        except Exception:
            print(fmt.format(fname[:-5], "(parse error)", "-", "-", "-", "-", "-", "-", "-"))


def cmd_session_show(args):
    session_id = args.session_id
    require_session(session_id)
    try:
        state = storage.read_state(storage.session_path(session_id))
        if not isinstance(state, dict):
            raise ValueError("not a mapping")
    except Exception as e:
        print(
            f"Error: session '{session_id}' state is malformed: {e}",
            file=sys.stderr,
        )
        sys.exit(1)
    s = state.get("session", {})

    def _fmt(v):
        if v is None or v == "":
            return "(none)"
        return str(v)

    print(f"Session: {s.get('id', '')}")
    print(f"  Peer ID:        {_fmt(s.get('peer_id'))}")
    print(f"  Tmux session:   {_fmt(s.get('tmux_session'))}")
    print(f"  Tmux target:    {_fmt(s.get('tmux_target'))}")
    print(f"  Mode:           {_fmt(s.get('mode'))}")
    print(f"  Status:         {_fmt(s.get('status'))}")
    print(f"  Room ID:        {_fmt(s.get('room_id'))}")
    print(f"  Handoff ID:     {_fmt(s.get('handoff_id'))}")
    print(f"  CWD:            {_fmt(s.get('cwd'))}")
    print(f"  Branch:         {_fmt(s.get('branch'))}")
    print(f"  Dirty:          {'yes' if s.get('dirty') else 'no'}")
    print(f"  Reuse count:    {_fmt(s.get('reuse_count'))}")
    print(f"  Heartbeat at:   {_fmt(s.get('heartbeat_at'))}")
    print(f"  Lease until:    {_fmt(s.get('lease_until'))}")
    print(f"  Last active at: {_fmt(s.get('last_active_at'))}")


def cmd_session_upsert(args):
    session_id = args.session_id
    validate_slug(session_id, "session_id")

    # Validate enums
    if args.mode is not None and args.mode not in VALID_SESSION_MODES:
        print(f"Error: Invalid mode '{args.mode}'. Valid: {', '.join(sorted(VALID_SESSION_MODES))}.", file=sys.stderr)
        sys.exit(1)
    if args.status is not None and args.status not in VALID_SESSION_STATUSES:
        print(f"Error: Invalid status '{args.status}'. Valid: {', '.join(sorted(VALID_SESSION_STATUSES))}.", file=sys.stderr)
        sys.exit(1)

    # CLI boundary check: tmux_target must match the safe pane id format BEFORE
    # we collect updates and write authoritative state. Without this, an
    # operator could `session upsert ... --tmux-target foo` and persist a
    # structurally invalid value into runtime/sessions/<id>.yaml.
    if args.tmux_target is not None:
        validate_tmux_target(args.tmux_target, "tmux_target")

    # Validate reuse_count non-negative
    reuse_count = None
    if args.reuse_count is not None:
        try:
            reuse_count = int(args.reuse_count)
        except ValueError:
            print(f"Error: --reuse-count must be an integer, got '{args.reuse_count}'.", file=sys.stderr)
            sys.exit(1)
        if reuse_count < 0:
            print(f"Error: --reuse-count cannot be negative.", file=sys.stderr)
            sys.exit(1)

    # Parse dirty
    dirty = None
    if args.dirty is not None:
        try:
            dirty = _parse_bool(args.dirty)
        except ValueError as e:
            print(f"Error: {e}", file=sys.stderr)
            sys.exit(1)

    # Referential integrity checks (only when value provided)
    if args.peer_id is not None:
        require_peer(args.peer_id)
    if args.room_id is not None:
        require_room(args.room_id)
    if args.handoff_id is not None:
        require_handoff(args.handoff_id)

    # Collect updates
    updates = {}
    if args.peer_id is not None:
        updates["peer_id"] = args.peer_id
    if args.tmux_session is not None:
        updates["tmux_session"] = args.tmux_session
    if args.tmux_target is not None:
        updates["tmux_target"] = args.tmux_target
    if args.mode is not None:
        updates["mode"] = args.mode
    if args.status is not None:
        updates["status"] = args.status
    if args.room_id is not None:
        updates["room_id"] = args.room_id
    if args.handoff_id is not None:
        updates["handoff_id"] = args.handoff_id
    if args.cwd is not None:
        updates["cwd"] = args.cwd
    if args.branch is not None:
        updates["branch"] = args.branch
    if dirty is not None:
        updates["dirty"] = dirty
    if reuse_count is not None:
        updates["reuse_count"] = reuse_count
    if args.heartbeat_at is not None:
        updates["heartbeat_at"] = args.heartbeat_at
    if args.lease_until is not None:
        updates["lease_until"] = args.lease_until
    if args.last_active_at is not None:
        updates["last_active_at"] = args.last_active_at

    if not updates:
        print("Error: No fields specified. Use --help to see available options.", file=sys.stderr)
        sys.exit(1)

    os.makedirs(storage.SESSIONS_DIR, exist_ok=True)

    # Load existing or create new
    path = storage.session_path(session_id)
    if os.path.isfile(path):
        try:
            state = storage.read_state(path)
            if not isinstance(state, dict):
                raise ValueError("not a mapping")
        except Exception as e:
            print(f"Error: session '{session_id}' state is malformed: {e}", file=sys.stderr)
            sys.exit(1)
    else:
        state = {"session": {"id": session_id}}

    if "session" not in state:
        state["session"] = {"id": session_id}
    else:
        state["session"]["id"] = session_id

    for k, v in updates.items():
        state["session"][k] = v

    storage.write_state(path, state)

    print(f"Session '{session_id}' upserted.")
    for k, v in updates.items():
        print(f"  {k}: {v}")
