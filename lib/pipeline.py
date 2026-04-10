"""Task pipeline — single-command room+handoff+dispatch automation."""
import os
import sys
from datetime import datetime, timezone

from . import storage
from .validators import validate_slug, is_slug_safe


def _generate_task_id() -> str:
    """Generate a timestamped task id like 'task-20260411-183045'."""
    now = datetime.now(timezone.utc)
    return now.strftime("task-%Y%m%d-%H%M%S")


def cmd_task_run(args):
    message = args.message
    execution_cwd = args.cwd
    peer_id = args.peer or "worker-1"
    priority = args.priority or "medium"

    # Validate inputs
    if not message or not message.strip():
        print("Error: --message is required and must not be empty.", file=sys.stderr)
        sys.exit(1)

    if not execution_cwd or not execution_cwd.strip():
        print("Error: --cwd is required (worker execution directory).", file=sys.stderr)
        sys.exit(1)

    if not os.path.isdir(execution_cwd):
        print(f"Error: --cwd '{execution_cwd}' does not exist.", file=sys.stderr)
        sys.exit(1)

    if not is_slug_safe(peer_id):
        print(f"Error: peer id '{peer_id}' is not slug-safe.", file=sys.stderr)
        sys.exit(1)

    task_id = _generate_task_id()
    room_id = task_id
    handoff_id = f"{task_id}-impl"

    # Validate generated IDs
    validate_slug(room_id, "room_id")
    validate_slug(handoff_id, "handoff_id")

    print(f"Pipeline: {task_id}")
    print(f"  message:  {message[:80]}")
    print(f"  cwd:      {execution_cwd}")
    print(f"  peer:     {peer_id}")
    print(f"  priority: {priority}")
    print()

    # Step 1: Create room
    print("[1/4] Creating room...")
    try:
        _step_create_room(room_id, message, execution_cwd)
        print(f"  room '{room_id}' created.")
    except Exception as e:
        print(f"Error at step 1 (room create): {e}", file=sys.stderr)
        sys.exit(1)

    # Step 2: Set room memory
    print("[2/4] Setting room memory...")
    try:
        _step_set_room_memory(room_id, message, execution_cwd)
        print(f"  room memory set.")
    except Exception as e:
        print(f"Error at step 2 (room memory): {e}", file=sys.stderr)
        sys.exit(1)

    # Step 3: Create handoff
    print("[3/4] Creating handoff...")
    try:
        _step_create_handoff(handoff_id, room_id, peer_id, message, priority)
        print(f"  handoff '{handoff_id}' created.")
    except Exception as e:
        print(f"Error at step 3 (handoff create): {e}", file=sys.stderr)
        sys.exit(1)

    # Step 4: Dispatch
    print("[4/4] Dispatching...")
    try:
        from .dispatch import cmd_handoff_dispatch
        # Build a minimal args namespace for dispatch
        dispatch_args = _SimpleNamespace(handoff_id=handoff_id)
        cmd_handoff_dispatch(dispatch_args)
    except SystemExit as e:
        if e.code and e.code != 0:
            print(f"Error at step 4 (dispatch): dispatch exited with code {e.code}", file=sys.stderr)
            sys.exit(1)
    except Exception as e:
        print(f"Error at step 4 (dispatch): {e}", file=sys.stderr)
        sys.exit(1)

    print()
    print(f"Pipeline complete: {task_id}")
    print(f"  room:     {room_id}")
    print(f"  handoff:  {handoff_id}")


class _SimpleNamespace:
    """Minimal namespace for passing args to subcommands."""
    def __init__(self, **kwargs):
        self.__dict__.update(kwargs)


def _step_create_room(room_id, message, execution_cwd):
    import shutil

    dest = storage.room_dir(room_id)
    if os.path.exists(dest):
        raise RuntimeError(f"room '{room_id}' already exists")

    shutil.copytree(storage.TEMPLATE_DIR, dest)

    now = storage.now_iso()
    state_file = storage.room_state_path(room_id)
    storage.update_state(state_file, {
        "room.id": room_id,
        "room.name": message[:60],
        "room.status": "active",
        "room.created_at": now,
        "room.updated_at": now,
        "context.goal": message,
        "lifecycle.current_phase": "execution",
    })

    log_file = storage.room_log_path(room_id)
    entry = (
        f"\n## {now} — pipeline\n"
        f"- Created room `{room_id}` via task run\n"
        f"- Goal: {message}\n"
    )
    storage.append_log(log_file, entry)


def _step_set_room_memory(room_id, message, execution_cwd):
    now = storage.now_iso()
    state_file = storage.room_state_path(room_id)
    storage.update_state(state_file, {
        "context.request_summary": message,
        "context.execution_cwd": execution_cwd,
        "room.updated_at": now,
    })

    log_entry = (
        f"\n## {now} — pipeline\n"
        f"- Room memory set: request_summary, execution_cwd={execution_cwd}\n"
    )
    storage.append_log(storage.room_log_path(room_id), log_entry)


def _step_create_handoff(handoff_id, room_id, peer_id, message, priority):
    dest = storage.handoff_path(handoff_id)
    if os.path.exists(dest):
        raise RuntimeError(f"handoff '{handoff_id}' already exists")

    os.makedirs(storage.HANDOFFS_DIR, exist_ok=True)

    now = storage.now_iso()
    state = {
        "handoff": {
            "id": handoff_id,
            "room_id": room_id,
            "program_id": None,
            "from": "orchestrator",
            "to": peer_id,
            "status": "open",
            "priority": priority,
            "kind": "implementation",
        },
        "task": {
            "description": message,
            "scope": "",
            "constraints": [],
            "acceptance_criteria": [],
            "report_back": "",
            "non_goals": [],
            "invariants": [],
            "failure_examples": [],
            "validation": [],
        },
        "timestamps": {
            "created_at": now,
            "claimed_at": None,
            "completed_at": None,
        },
    }
    storage.write_state(dest, state)

    log_entry = (
        f"\n## {now} — pipeline\n"
        f"- Handoff `{handoff_id}` created -> {peer_id}\n"
        f"- Task: {message}\n"
    )
    storage.append_log(storage.room_log_path(room_id), log_entry)
    storage.update_state(storage.room_state_path(room_id), {"room.updated_at": now})
