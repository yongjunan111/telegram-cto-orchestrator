"""Session checkpoint — derived memory artifact."""
import os
import re
import sys

from . import storage
from .validators import require_session
from .handoffs import _get_handoff_kind, _derive_review_state


CHECKPOINTS_DIR = os.path.join(storage.RUNTIME_DIR, "checkpoints")

_VALID_EVENT_RE = re.compile(r'^[A-Za-z0-9_-]+$')


def _validate_event(event: str) -> None:
    """Exit with error if event is not safe for filename use."""
    if not event:
        print("Error: --event cannot be empty.", file=sys.stderr)
        sys.exit(1)
    if not _VALID_EVENT_RE.match(event):
        print(
            f"Error: invalid checkpoint event '{event}'; "
            f"use only letters, numbers, '-' or '_'.",
            file=sys.stderr,
        )
        sys.exit(1)


def cmd_session_checkpoint(args):
    session_id = args.session_id
    event = args.event
    note = args.note or ""

    _validate_event(event)
    # Defense in depth: also validate session_id component
    if not _VALID_EVENT_RE.match(session_id):
        print(
            f"Error: session_id '{session_id}' contains unsafe characters.",
            file=sys.stderr,
        )
        sys.exit(1)

    require_session(session_id)

    # Load session state (controlled error on malformed)
    try:
        session_state = storage.read_state(storage.session_path(session_id))
        if not isinstance(session_state, dict) or "session" not in session_state:
            raise ValueError("missing 'session' section")
    except Exception as e:
        print(f"Error: session '{session_id}' state is malformed: {e}", file=sys.stderr)
        sys.exit(1)

    s = session_state.get("session", {})
    room_id = s.get("room_id")
    handoff_id = s.get("handoff_id")

    # Load room state if available
    room_state = None
    if room_id:
        try:
            room_state = storage.read_state(storage.room_state_path(room_id))
            if not isinstance(room_state, dict):
                room_state = None
        except Exception:
            room_state = None

    # Load handoff state if available
    handoff_state = None
    if handoff_id:
        try:
            handoff_state = storage.read_state(storage.handoff_path(handoff_id))
            if not isinstance(handoff_state, dict) or "handoff" not in handoff_state:
                handoff_state = None
        except Exception:
            handoff_state = None

    # Render artifact
    now = storage.now_iso()
    content = _render_checkpoint(
        session_id, event, note, s, room_state, handoff_state, now
    )

    # Write to checkpoint file
    try:
        os.makedirs(CHECKPOINTS_DIR, exist_ok=True)
        # Filename-safe timestamp
        ts_safe = now.replace(":", "-").replace(".", "-")
        filename = f"{session_id}-{event}-{ts_safe}.md"
        artifact_path = os.path.join(CHECKPOINTS_DIR, filename)

        # Defense in depth: ensure resolved path is inside CHECKPOINTS_DIR
        checkpoints_real = os.path.realpath(CHECKPOINTS_DIR)
        artifact_real = os.path.realpath(artifact_path)
        if not (artifact_real == checkpoints_real or
                artifact_real.startswith(checkpoints_real + os.sep)):
            print(
                f"Error: checkpoint path '{artifact_path}' escapes checkpoints directory.",
                file=sys.stderr,
            )
            sys.exit(1)

        with open(artifact_path, "w") as f:
            f.write(content)
    except SystemExit:
        raise
    except Exception as e:
        print(f"Error: checkpoint write failed: {e}", file=sys.stderr)
        sys.exit(1)

    print(f"Checkpoint artifact written: {artifact_path}")


def _render_checkpoint(session_id, event, note, s, room_state, handoff_state, now):
    def _fmt(v):
        if v is None or v == "":
            return "(none)"
        return str(v)

    lines = [
        f"# Session Checkpoint: {session_id}",
        "",
        f"- **Event:** {event}",
        f"- **Generated at:** {now}",
        f"- **Note:** {_fmt(note)}",
        "",
        "## Session",
        f"- **Peer ID:** {_fmt(s.get('peer_id'))}",
        f"- **Tmux session:** {_fmt(s.get('tmux_session'))}",
        f"- **Mode:** {_fmt(s.get('mode'))}",
        f"- **Status:** {_fmt(s.get('status'))}",
        f"- **Dirty:** {'yes' if s.get('dirty') else 'no'}",
        f"- **Reuse count:** {_fmt(s.get('reuse_count'))}",
        f"- **CWD:** {_fmt(s.get('cwd'))}",
        f"- **Branch:** {_fmt(s.get('branch'))}",
        f"- **Lease until:** {_fmt(s.get('lease_until'))}",
        f"- **Last active at:** {_fmt(s.get('last_active_at'))}",
    ]

    # Handoff summary
    lines.append("")
    lines.append("## Handoff")
    if handoff_state:
        h = handoff_state.get("handoff", {})
        resolution = handoff_state.get("resolution", {})
        lines.append(f"- **ID:** {_fmt(h.get('id'))}")
        lines.append(f"- **Room:** {_fmt(h.get('room_id'))}")
        lines.append(f"- **To:** {_fmt(h.get('to'))}")
        lines.append(f"- **Kind:** {_get_handoff_kind(handoff_state)}")
        lines.append(f"- **Status:** {_fmt(h.get('status'))}")
        lines.append(f"- **Review state:** {_derive_review_state(handoff_state)}")
        if resolution and resolution.get("summary"):
            lines.append(f"- **Summary:** {resolution.get('summary')}")
    else:
        lines.append(f"- **ID:** {_fmt(s.get('handoff_id'))}")
        lines.append("- (handoff state not available)")

    # Room summary
    lines.append("")
    lines.append("## Room")
    if room_state:
        room = room_state.get("room", {})
        context = room_state.get("context", {})
        lifecycle = room_state.get("lifecycle", {})
        discovery = room_state.get("discovery", {})

        lines.append(f"- **ID:** {_fmt(room.get('id'))}")
        lines.append(f"- **Goal:** {_fmt(context.get('goal'))}")

        lines.append("")
        lines.append("### Lifecycle")
        lines.append(f"- **Phase:** {_fmt(lifecycle.get('current_phase'))}")
        lines.append(f"- **Next action:** {_fmt(lifecycle.get('next_action'))}")
        lines.append(f"- **Blocker summary:** {_fmt(lifecycle.get('blocker_summary'))}")
        lines.append(f"- **Blocked by:** {_fmt(lifecycle.get('blocked_by'))}")

        lines.append("")
        lines.append("### Memory")
        lines.append(f"- **Request summary:** {_fmt(context.get('request_summary'))}")
        lines.append(f"- **Current summary:** {_fmt(context.get('current_summary'))}")
        open_q = context.get("open_questions") or []
        if open_q:
            lines.append("- **Open questions:**")
            for q in open_q:
                lines.append(f"  - {q}")
        else:
            lines.append("- **Open questions:** (none)")

        lines.append("")
        lines.append("### Discovery")
        lines.append(f"- **Problem statement:** {_fmt(discovery.get('problem_statement'))}")
        lines.append(f"- **Chosen direction:** {_fmt(discovery.get('chosen_direction'))}")
        unknowns = discovery.get("implementation_unknowns") or []
        if unknowns:
            lines.append("- **Implementation unknowns:**")
            for u in unknowns:
                lines.append(f"  - {u}")
        else:
            lines.append("- **Implementation unknowns:** (none)")
    else:
        lines.append(f"- **ID:** {_fmt(s.get('room_id'))}")
        lines.append("- (room state not available)")

    # Dispatch artifact pointer
    dispatches_dir = os.path.join(storage.RUNTIME_DIR, "dispatches")
    handoff_id_val = s.get("handoff_id")
    if handoff_id_val:
        dispatch_path = os.path.join(dispatches_dir, f"{handoff_id_val}.md")
        lines.append("")
        lines.append("## Dispatch Artifact")
        if os.path.isfile(dispatch_path):
            lines.append(f"- **Path:** {dispatch_path}")
        else:
            lines.append("- (no dispatch artifact found)")

    lines.append("")
    lines.append("---")
    lines.append("*This file is a derived checkpoint. The source of truth is room, handoff, and session YAML state. Do not edit manually.*")

    return "\n".join(lines)
