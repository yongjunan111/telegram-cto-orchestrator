"""Room command logic."""
import os
import shutil
import sys

from . import storage
from .validators import validate_slug, require_room
from .handoffs import scan_room_handoffs


def cmd_room_memory(args):
    room_id = args.room_id
    require_room(room_id)

    # Validate conflicting options
    if args.open_questions is not None and args.clear_open_questions:
        print(
            "Error: --open-question and --clear-open-questions cannot be used together.",
            file=sys.stderr,
        )
        sys.exit(1)

    if args.blocker_summary is not None and args.clear_blocker:
        print(
            "Error: --blocker-summary and --clear-blocker cannot be used together.",
            file=sys.stderr,
        )
        sys.exit(1)

    if args.blocked_by is not None and args.clear_blocker:
        print(
            "Error: --blocked-by and --clear-blocker cannot be used together.",
            file=sys.stderr,
        )
        sys.exit(1)

    # Collect updates
    updates = {}
    log_changes = []

    if args.request_summary is not None:
        updates["context.request_summary"] = args.request_summary
        log_changes.append("request_summary updated")

    if args.current_summary is not None:
        updates["context.current_summary"] = args.current_summary
        log_changes.append("current_summary updated")

    if args.next_action is not None:
        updates["lifecycle.next_action"] = args.next_action
        log_changes.append("next_action updated")

    if args.blocker_summary is not None:
        updates["lifecycle.blocker_summary"] = args.blocker_summary
        log_changes.append("blocker_summary updated")

    if args.blocked_by is not None:
        updates["lifecycle.blocked_by"] = args.blocked_by
        log_changes.append("blocked_by updated")

    if args.open_questions is not None:
        updates["context.open_questions"] = args.open_questions
        log_changes.append(f"open_questions set ({len(args.open_questions)} items)")

    if args.clear_open_questions:
        updates["context.open_questions"] = []
        log_changes.append("open_questions cleared")

    if args.clear_blocker:
        updates["lifecycle.blocker_summary"] = ""
        updates["lifecycle.blocked_by"] = None
        log_changes.append("blocker cleared (blocker_summary + blocked_by)")

    if args.phase is not None:
        updates["lifecycle.current_phase"] = args.phase
        log_changes.append(f"phase updated to: {args.phase}")

    if not updates:
        print("Error: No changes specified. Use --help to see available options.", file=sys.stderr)
        sys.exit(1)

    # Apply updates: read full state, apply in-memory, write back atomically
    now = storage.now_iso()
    updates["room.updated_at"] = now

    state_file = storage.room_state_path(room_id)
    state = storage.read_state(state_file)

    for dotkey, value in updates.items():
        parts = dotkey.split(".", 1)
        section, key = parts[0], parts[1]
        if section not in state:
            state[section] = {}
        state[section][key] = value

    storage.write_state(state_file, state)

    # Append to log
    log_entry = (
        f"\n## {now} — orchestrator\n"
        f"- Room memory updated: {', '.join(log_changes)}\n"
    )
    storage.append_log(storage.room_log_path(room_id), log_entry)

    print(f"Room '{room_id}' memory updated.")
    for change in log_changes:
        print(f"  - {change}")


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
    state = storage.read_state(state_file)

    room = state.get("room", {})
    context = state.get("context", {})
    lifecycle = state.get("lifecycle", {})

    print(f"Room: {room.get('id', '')}")
    print(f"  Name:    {room.get('name', '')}")
    print(f"  Status:  {room.get('status', '')}")
    print(f"  Program: {room.get('program_id') or '(none)'}")
    print(f"  Created: {room.get('created_at', '')}")
    print(f"  Updated: {room.get('updated_at', '')}")
    print()
    print("Context:")
    print(f"  Goal:              {context.get('goal', '') or '(none)'}")
    print(f"  Request summary:   {context.get('request_summary', '') or '(none)'}")
    print(f"  Current summary:   {context.get('current_summary', '') or '(none)'}")

    oq = context.get("open_questions") or []
    if oq:
        print(f"  Open questions:")
        for q in oq:
            print(f"    - {q}")
    else:
        print(f"  Open questions:    (none)")

    constraints = context.get("constraints") or []
    if constraints:
        print(f"  Constraints:")
        for c in constraints:
            print(f"    - {c}")
    else:
        print(f"  Constraints:       (none)")

    criteria = context.get("acceptance_criteria") or []
    if criteria:
        print(f"  Acceptance criteria:")
        for c in criteria:
            print(f"    - {c}")
    else:
        print(f"  Acceptance criteria: (none)")

    print()
    print("Lifecycle:")
    print(f"  Phase:             {lifecycle.get('current_phase', '')}")
    print(f"  Next action:       {lifecycle.get('next_action', '') or '(none)'}")
    print(f"  Blocker summary:   {lifecycle.get('blocker_summary', '') or '(none)'}")
    print(f"  Blocked by:        {lifecycle.get('blocked_by') or '(none)'}")

    # Derived handoff summary
    print()
    print("Handoff Summary (derived):")
    handoffs, parse_errors = scan_room_handoffs(room_id)

    if parse_errors:
        print(f"  WARNING: {len(parse_errors)} handoff file(s) could not be parsed: {', '.join(parse_errors)}")
        print(f"  Summary below may be incomplete.")
        print()

    if not handoffs and not parse_errors:
        print("  No handoffs found for this room.")
    elif not handoffs and parse_errors:
        pass  # warning already printed, no valid handoffs to show
    else:
        status_groups = {}
        for ho_state in handoffs:
            h = ho_state.get("handoff", {})
            s = h.get("status", "unknown")
            ho_id = h.get("id", "?")
            status_groups.setdefault(s, []).append(ho_id)

        for status in ["open", "claimed", "blocked", "completed"]:
            ids = status_groups.get(status, [])
            if ids:
                print(f"  {status}: {len(ids)} — {', '.join(ids)}")
            else:
                print(f"  {status}: 0")

        # Show any unexpected statuses
        for status, ids in status_groups.items():
            if status not in ("open", "claimed", "blocked", "completed"):
                print(f"  {status}: {len(ids)} — {', '.join(ids)}")
