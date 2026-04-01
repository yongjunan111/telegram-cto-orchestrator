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
