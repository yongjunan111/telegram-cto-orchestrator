"""Room command logic."""
import os
import shutil
import sys

from . import storage
from .validators import validate_slug, require_room


def cmd_room_create(args):
    room_id = args.room_id
    name = args.name
    goal = args.goal

    validate_slug(room_id, "room_id")

    dest = storage.room_dir(room_id)
    if os.path.exists(dest):
        print(f"Error: room '{room_id}' already exists.", file=sys.stderr)
        sys.exit(1)

    # Copy template
    shutil.copytree(storage.TEMPLATE_DIR, dest)

    # Update state.yaml
    now = storage.now_iso()
    state_file = storage.room_state_path(room_id)
    storage.update_state(state_file, {
        "room.id": room_id,
        "room.name": name,
        "room.status": "active",
        "room.created_at": now,
        "room.updated_at": now,
        "context.goal": goal,
        "lifecycle.current_phase": "triage",
    })

    # Append to log.md
    log_file = storage.room_log_path(room_id)
    entry = (
        f"\n## {now} — orchestrator\n"
        f"- Created room `{room_id}`\n"
        f"- Name: {name}\n"
        f"- Goal: {goal}\n"
    )
    storage.append_log(log_file, entry)

    print(f"Room '{room_id}' created.")
    print(f"  name:  {name}")
    print(f"  goal:  {goal}")
    print(f"  phase: triage")
    print(f"  path:  {dest}")


def cmd_room_list(args):
    if not os.path.isdir(storage.ROOMS_DIR):
        print("No rooms directory found.")
        return

    rooms = [
        d for d in sorted(os.listdir(storage.ROOMS_DIR))
        if d != "TEMPLATE" and os.path.isdir(os.path.join(storage.ROOMS_DIR, d))
    ]

    if not rooms:
        print("No rooms found.")
        return

    # Header
    fmt = "{:<24} {:<28} {:<12} {:<12}"
    print(fmt.format("ID", "NAME", "STATUS", "PHASE"))
    print("-" * 78)
    for room_id in rooms:
        state_file = storage.room_state_path(room_id)
        if not os.path.exists(state_file):
            print(fmt.format(room_id, "(no state.yaml)", "-", "-"))
            continue
        try:
            state = storage.read_state(state_file)
            r = state.get("room", {})
            lc = state.get("lifecycle", {})
            name = (r.get("name") or "")[:27]
            status = (str(r.get("status") or ""))[:11]
            phase = (str(lc.get("current_phase") or ""))[:11]
            print(fmt.format(room_id, name, status, phase))
        except Exception:
            print(fmt.format(room_id, "(parse error)", "-", "-"))


def cmd_room_show(args):
    room_id = args.room_id
    require_room(room_id)
    state_file = storage.room_state_path(room_id)
    with open(state_file, "r") as f:
        print(f.read(), end="")
