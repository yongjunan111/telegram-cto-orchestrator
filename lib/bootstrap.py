"""Session bootstrap — derived startup packet."""
import os
import re
import sys

from . import storage
from .validators import require_session, validate_slug
from .handoffs import _get_handoff_kind, _derive_review_state


BOOTSTRAP_DIR = os.path.join(storage.RUNTIME_DIR, "bootstrap")
CHECKPOINTS_DIR = os.path.join(storage.RUNTIME_DIR, "checkpoints")
DISPATCHES_DIR = os.path.join(storage.RUNTIME_DIR, "dispatches")
WIKI_CURRENT_STATE = os.path.join(storage.ORCHESTRATOR_DIR, "wiki", "current-state.md")


def cmd_session_bootstrap(args):
    session_id = args.session_id
    require_session(session_id)

    # Load session state
    try:
        session_state = storage.read_state(storage.session_path(session_id))
        if not isinstance(session_state, dict) or "session" not in session_state:
            raise ValueError("missing 'session' section")
    except Exception as e:
        print(f"Error: session '{session_id}' state is malformed: {e}", file=sys.stderr)
        sys.exit(1)

    s = session_state.get("session", {})
    room_id = s.get("room_id") or ""
    handoff_id = s.get("handoff_id") or ""

    # Fix 3: revalidate internal references from session state before using them in file paths
    try:
        if room_id:
            validate_slug(room_id, "session.room_id")
    except SystemExit:
        print(f"Warning: session '{session_id}' has invalid room_id '{room_id}' — treating as unset.", file=sys.stderr)
        room_id = ""

    try:
        if handoff_id:
            validate_slug(handoff_id, "session.handoff_id")
    except SystemExit:
        print(f"Warning: session '{session_id}' has invalid handoff_id '{handoff_id}' — treating as unset.", file=sys.stderr)
        handoff_id = ""

    # Load room state (fallback None if missing/malformed)
    room_state = None
    if room_id:
        try:
            room_state = storage.read_state(storage.room_state_path(room_id))
            if not isinstance(room_state, dict):
                room_state = None
        except Exception:
            room_state = None

    # Load handoff state (fallback None if missing/malformed)
    handoff_state = None
    if handoff_id:
        try:
            handoff_state = storage.read_state(storage.handoff_path(handoff_id))
            if not isinstance(handoff_state, dict) or "handoff" not in handoff_state:
                handoff_state = None
        except Exception:
            handoff_state = None

    # Find latest relevant checkpoint
    checkpoint_path, checkpoint_snippet = _find_latest_checkpoint(
        session_id, handoff_id, room_id
    )

    # Build artifact
    now = storage.now_iso()
    content = _render_bootstrap(
        session_id, s, room_state, handoff_state,
        checkpoint_path, checkpoint_snippet, now
    )

    # Write
    try:
        # Defense-in-depth: validate session_id as slug for filename safety
        validate_slug(session_id, "session_id")
        artifact_path = os.path.join(BOOTSTRAP_DIR, f"{session_id}.md")
        storage.safe_write_text(BOOTSTRAP_DIR, artifact_path, content)
    except SystemExit:
        raise
    except Exception as e:
        print(f"Error: bootstrap write failed: {e}", file=sys.stderr)
        sys.exit(1)

    print(f"Bootstrap artifact written: {artifact_path}")


def _find_latest_checkpoint(session_id: str, handoff_id: str, room_id: str):
    """Return (path, brief_content) of latest relevant checkpoint, or (None, None).

    Priority:
      1. Same session_id (filename startswith session_id + '-')
      2. Same handoff_id (checkpoint metadata contains handoff ID)
      3. Same room_id (checkpoint metadata contains room ID)
      4. None

    Within each priority tier, pick newest by file mtime.
    """
    if not os.path.isdir(CHECKPOINTS_DIR):
        return None, None

    try:
        all_files = [
            f for f in os.listdir(CHECKPOINTS_DIR)
            if f.endswith(".md")
        ]
    except Exception:
        return None, None

    # Priority 1: same session_id
    matches_p1 = [f for f in all_files if f.startswith(session_id + "-")]
    if matches_p1:
        matches_p1.sort(
            key=lambda f: (os.path.getmtime(os.path.join(CHECKPOINTS_DIR, f)), f),
            reverse=True,
        )
        chosen = matches_p1[0]
        return _load_checkpoint_snippet(chosen)

    # Priority 2: same handoff_id (scan content)
    # Priority 3: same room_id (scan content)
    candidates_p2 = []
    candidates_p3 = []

    ho_pattern = re.compile(r'\*\*ID:\*\*\s*' + re.escape(handoff_id or "__NONE__"))
    room_pattern = re.compile(r'\*\*(?:Room|ID):\*\*\s*' + re.escape(room_id or "__NONE__"))

    for f in all_files:
        path = os.path.join(CHECKPOINTS_DIR, f)
        try:
            with open(path, "r") as fp:
                head = fp.read(4096)
        except Exception:
            continue

        if handoff_id and ho_pattern.search(head):
            candidates_p2.append((f, path))
        elif room_id and room_pattern.search(head):
            candidates_p3.append((f, path))

    if candidates_p2:
        candidates_p2.sort(key=lambda t: (os.path.getmtime(t[1]), t[0]), reverse=True)
        return _load_checkpoint_snippet(candidates_p2[0][0])

    if candidates_p3:
        candidates_p3.sort(key=lambda t: (os.path.getmtime(t[1]), t[0]), reverse=True)
        return _load_checkpoint_snippet(candidates_p3[0][0])

    return None, None


def _load_checkpoint_snippet(filename: str):
    """Return (absolute_path, first ~30 lines snippet)."""
    path = os.path.join(CHECKPOINTS_DIR, filename)
    try:
        with open(path, "r") as f:
            lines = f.readlines()
        snippet_lines = lines[:30]
        snippet = "".join(snippet_lines).rstrip()
        return path, snippet
    except Exception:
        return path, None


def _render_bootstrap(session_id, s, room_state, handoff_state,
                      checkpoint_path, checkpoint_snippet, now):
    def _fmt(v):
        if v is None or v == "":
            return "(none)"
        return str(v)

    lines = [
        f"# Session Bootstrap: {session_id}",
        "",
        f"- **Generated at:** {now}",
        "",
        "## Session",
        f"- **Peer ID:** {_fmt(s.get('peer_id'))}",
        f"- **Tmux session:** {_fmt(s.get('tmux_session'))}",
        f"- **Tmux target:** {_fmt(s.get('tmux_target'))}",
        f"- **Mode:** {_fmt(s.get('mode'))}",
        f"- **Status:** {_fmt(s.get('status'))}",
        f"- **CWD:** {_fmt(s.get('cwd'))}",
        f"- **Branch:** {_fmt(s.get('branch'))}",
        "",
        "## Handoff",
    ]

    if handoff_state:
        h = handoff_state.get("handoff", {})
        lines.append(f"- **ID:** {_fmt(h.get('id'))}")
        lines.append(f"- **Kind:** {_get_handoff_kind(handoff_state)}")
        lines.append(f"- **Status:** {_fmt(h.get('status'))}")
        lines.append(f"- **Review state:** {_derive_review_state(handoff_state)}")
    else:
        lines.append(f"- **ID:** {_fmt(s.get('handoff_id'))}")
        lines.append("- (handoff state not available)")

    lines.append("")
    lines.append("## Room")

    if room_state:
        room = room_state.get("room", {})
        context = room_state.get("context", {})
        lifecycle = room_state.get("lifecycle", {})
        discovery = room_state.get("discovery", {})

        lines.append(f"- **ID:** {_fmt(room.get('id'))}")

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
    handoff_id_val = s.get("handoff_id")
    lines.append("")
    lines.append("## Dispatch Artifact")
    if handoff_id_val:
        dispatch_path = os.path.join(DISPATCHES_DIR, f"{handoff_id_val}.md")
        if os.path.isfile(dispatch_path):
            lines.append(f"- **Path:** {dispatch_path}")
        else:
            lines.append("- (no dispatch artifact found)")
    else:
        lines.append("- (no handoff bound)")

    # Latest checkpoint
    lines.append("")
    lines.append("## Latest Relevant Checkpoint")
    if checkpoint_path:
        lines.append(f"- **Path:** {checkpoint_path}")
        if checkpoint_snippet:
            lines.append("")
            lines.append("```")
            lines.append(checkpoint_snippet)
            lines.append("```")
    else:
        lines.append("- (none)")

    # Wiki pointer
    lines.append("")
    lines.append("## Wiki")
    if os.path.isfile(WIKI_CURRENT_STATE):
        lines.append(f"- **Current state:** {WIKI_CURRENT_STATE}")
    else:
        lines.append("- (no wiki pointer)")

    lines.append("")
    lines.append("---")
    lines.append("*This file is a derived bootstrap packet. The source of truth is room/handoff/session YAML state and checkpoint artifacts. Do not edit manually.*")

    return "\n".join(lines)
