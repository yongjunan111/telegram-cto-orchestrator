"""Log append command logic."""
from . import storage
from .validators import require_room


def cmd_log_append(args):
    room_id = args.room_id
    actor = args.actor
    message = args.message
    require_room(room_id)

    now = storage.now_iso()
    entry = f"\n## {now} — {actor}\n- {message}\n"

    storage.append_log(storage.room_log_path(room_id), entry)

    # Update updated_at in state.yaml
    storage.update_state(storage.room_state_path(room_id), {"room.updated_at": now})

    print(f"Appended log entry to room '{room_id}'.")
