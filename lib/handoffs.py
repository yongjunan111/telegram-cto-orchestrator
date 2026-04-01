"""Handoff command logic."""
import os
import sys

from . import storage
from .validators import validate_slug, require_room, require_handoff, require_peer


def cmd_handoff_create(args):
    handoff_id = args.handoff_id
    room_id = args.room
    to = args.to
    task_desc = args.task
    priority = args.priority
    scope = args.scope or ""
    report_back = args.report_back or ""

    validate_slug(handoff_id, "handoff_id")
    require_room(room_id)
    require_peer(to)

    dest = storage.handoff_path(handoff_id)
    if os.path.exists(dest):
        print(f"Error: handoff '{handoff_id}' already exists.", file=sys.stderr)
        sys.exit(1)

    os.makedirs(storage.HANDOFFS_DIR, exist_ok=True)

    now = storage.now_iso()
    state = {
        "handoff": {
            "id": handoff_id,
            "room_id": room_id,
            "program_id": None,
            "from": "orchestrator",
            "to": to,
            "status": "open",
            "priority": priority,
        },
        "task": {
            "description": task_desc,
            "scope": scope,
            "constraints": [],
            "acceptance_criteria": [],
            "report_back": report_back,
        },
        "timestamps": {
            "created_at": now,
            "claimed_at": None,
            "completed_at": None,
        },
    }
    storage.write_state(dest, state)

    # Append handoff creation to room log
    log_entry = (
        f"\n## {now} — orchestrator\n"
        f"- Handoff `{handoff_id}` created -> {to}\n"
        f"- Task: {task_desc}\n"
    )
    storage.append_log(storage.room_log_path(room_id), log_entry)

    # Update room's updated_at
    storage.update_state(storage.room_state_path(room_id), {"room.updated_at": now})

    print(f"Handoff '{handoff_id}' created.")
    print(f"  room:     {room_id}")
    print(f"  to:       {to}")
    print(f"  priority: {priority}")
    print(f"  task:     {task_desc[:72]}")


def cmd_handoff_list(args):
    if not os.path.isdir(storage.HANDOFFS_DIR):
        print("No handoffs found.")
        return

    files = sorted(
        f for f in os.listdir(storage.HANDOFFS_DIR)
        if f.endswith(".yaml") and f != ".gitkeep"
    )

    if not files:
        print("No handoffs found.")
        return

    fmt = "{:<28} {:<20} {:<20} {:<10} {:<10}"
    print(fmt.format("ID", "ROOM", "TO", "STATUS", "PRIORITY"))
    print("-" * 90)
    for fname in files:
        path = os.path.join(storage.HANDOFFS_DIR, fname)
        try:
            state = storage.read_state(path)
            h = state.get("handoff", {})
            hid = str(h.get("id") or fname[:-5])[:27]
            room = str(h.get("room_id") or "")[:19]
            to = str(h.get("to") or "")[:19]
            status = str(h.get("status") or "")[:9]
            priority = str(h.get("priority") or "")[:9]
            print(fmt.format(hid, room, to, status, priority))
        except Exception:
            print(fmt.format(fname[:-5], "(parse error)", "-", "-", "-"))


def cmd_handoff_show(args):
    handoff_id = args.handoff_id
    require_handoff(handoff_id)
    path = storage.handoff_path(handoff_id)
    with open(path, "r") as f:
        print(f.read(), end="")


# ---------------------------------------------------------------------------
# Transition helpers
# ---------------------------------------------------------------------------

_VALID_TRANSITIONS = {
    "open": {"claimed"},
    "claimed": {"blocked", "completed"},
    "blocked": set(),
    "completed": set(),
}


def _load_handoff(handoff_id: str) -> dict:
    require_handoff(handoff_id)
    return storage.read_state(storage.handoff_path(handoff_id))


def _assert_transition(current: str, target: str) -> None:
    allowed = _VALID_TRANSITIONS.get(current, set())
    if target not in allowed:
        print(
            f"Error: Cannot transition from '{current}' to '{target}'.",
            file=sys.stderr,
        )
        sys.exit(1)


def _assert_assignee(state: dict, peer_id: str, handoff_id: str) -> None:
    assignee = state.get("handoff", {}).get("to", "")
    if peer_id != assignee:
        print(
            f"Error: Peer '{peer_id}' is not the assignee of handoff '{handoff_id}'. "
            f"Current assignee: '{assignee}'.",
            file=sys.stderr,
        )
        sys.exit(1)


def _write_transition(handoff_id: str, state: dict, updates: dict) -> None:
    """Merge updates into state and write atomically."""
    for dotkey, value in updates.items():
        parts = dotkey.split(".", 1)
        section, key = parts[0], parts[1] if len(parts) == 2 else None
        if key is None:
            state[section] = value
        else:
            if section not in state:
                state[section] = {}
            state[section][key] = value
    storage.write_state(storage.handoff_path(handoff_id), state)


def _log_transition(room_id: str, handoff_id: str, peer_id: str, action: str, extra: str, now: str) -> None:
    entry = (
        f"\n## {now} — {peer_id}\n"
        f"- Handoff `{handoff_id}` {action} by {peer_id}\n"
    )
    if extra:
        entry += f"- {extra}\n"
    storage.append_log(storage.room_log_path(room_id), entry)
    storage.update_state(storage.room_state_path(room_id), {"room.updated_at": now})


# ---------------------------------------------------------------------------
# claim
# ---------------------------------------------------------------------------

def cmd_handoff_claim(args):
    handoff_id = args.handoff_id
    peer_id = args.by

    require_peer(peer_id)
    state = _load_handoff(handoff_id)
    current = state.get("handoff", {}).get("status", "")
    _assert_transition(current, "claimed")
    _assert_assignee(state, peer_id, handoff_id)

    now = storage.now_iso()
    _write_transition(handoff_id, state, {
        "handoff.status": "claimed",
        "handoff.to": peer_id,
        "timestamps.claimed_at": now,
    })

    room_id = state["handoff"]["room_id"]
    _log_transition(room_id, handoff_id, peer_id, "claimed", "", now)

    print(f"Handoff '{handoff_id}' claimed by '{peer_id}'.")


# ---------------------------------------------------------------------------
# block
# ---------------------------------------------------------------------------

def cmd_handoff_block(args):
    handoff_id = args.handoff_id
    peer_id = args.by
    reason = args.reason

    require_peer(peer_id)
    state = _load_handoff(handoff_id)
    current = state.get("handoff", {}).get("status", "")
    _assert_transition(current, "blocked")
    _assert_assignee(state, peer_id, handoff_id)

    now = storage.now_iso()
    _write_transition(handoff_id, state, {
        "handoff.status": "blocked",
        "timestamps.blocked_at": now,
        "resolution.blocked_by": peer_id,
        "resolution.blocked_reason": reason,
    })

    room_id = state["handoff"]["room_id"]
    _log_transition(room_id, handoff_id, peer_id, "blocked", f"Reason: {reason}", now)

    print(f"Handoff '{handoff_id}' blocked by '{peer_id}'.")
    print(f"  reason: {reason}")


# ---------------------------------------------------------------------------
# complete
# ---------------------------------------------------------------------------

def cmd_handoff_complete(args):
    handoff_id = args.handoff_id
    peer_id = args.by
    summary = args.summary

    require_peer(peer_id)
    state = _load_handoff(handoff_id)
    current = state.get("handoff", {}).get("status", "")
    _assert_transition(current, "completed")
    _assert_assignee(state, peer_id, handoff_id)

    now = storage.now_iso()
    _write_transition(handoff_id, state, {
        "handoff.status": "completed",
        "timestamps.completed_at": now,
        "resolution.completed_by": peer_id,
        "resolution.summary": summary,
    })

    room_id = state["handoff"]["room_id"]
    _log_transition(room_id, handoff_id, peer_id, "completed", f"Summary: {summary}", now)

    print(f"Handoff '{handoff_id}' completed by '{peer_id}'.")
    print(f"  summary: {summary}")
