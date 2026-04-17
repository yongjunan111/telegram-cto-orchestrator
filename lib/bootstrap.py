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


def _render_team_lead_protocol() -> str:
    # Compute absolute path to sub-handoff format spec
    repo_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    spec_path = os.path.join(repo_root, "docs", "sub-handoff-format.md")

    return f"""\
## Team Lead Protocol

You are a **team lead**, not a solo implementer. Your primary job is to execute the handoff.
When the task is large enough to benefit from decomposition, you decompose it into sub-tasks,
delegate to sub-agents, verify their results, and report a curated summary to the CTO.

### Execution Sequence

1. Read the **Dispatch Artifact** linked below for your full task description
2. Extract the parent handoff's contract: task acceptance criteria, room acceptance criteria, validation steps, invariants, non-goals, failure examples
3. Assess: can you complete this directly, or does it benefit from decomposition?
4. If decomposing: create structured sub-handoffs per `{spec_path}`
5. Verify all results against parent acceptance criteria
6. Report to CTO using the structured report format below

### When to Decompose vs. Do Directly

**Do directly** (default):
- Single focused change in 1-2 files
- Task takes under 5 minutes of focused work
- Steps are sequential and depend on each other's output
- Files are tightly coupled (shared state, imports)

**Decompose into sub-handoffs** (only when ALL conditions hold):
- Task involves 2+ distinct, disjoint concerns
- Concerns touch separate files/modules with no shared edits
- Sub-tasks can be verified independently

### How to Delegate

Use Claude Code's Agent tool with a structured sub-handoff as the prompt.
The full format specification is in `{spec_path}`. Key rules:

1. **No free-text delegation.** Every sub-agent gets a structured sub-handoff.
2. **One task per sub-handoff.** Never bundle unrelated work.
3. **Carry parent contract down.** Each sub-handoff must include `must_preserve` (parent invariants/non-goals) and `must_not_do` (parent failure examples).
4. **Set file ownership.** Use `owned_files` to prevent sub-agents from editing each other's files.
5. **Include `escalate_if`** so the sub-agent knows when to stop and ask.
6. **Map to parent criteria.** Use `covers_parent_criteria` to track which parent items each sub-task addresses. Use prefixes: `TA1` (task acceptance), `RA1` (room acceptance), `V1` (validation).

### How to Verify

After each sub-agent returns:
1. Check every acceptance criterion — did the sub-agent provide concrete evidence (commands run, test output)?
2. Distinguish **evidence** from **claims**. "I fixed it" is a claim. "pytest passed (12/12)" is evidence.
3. If evidence is missing or insufficient, send a targeted rework (not the full sub-handoff).
4. Run integration checks that span sub-task boundaries (sub-agents only see their slice).
5. Map sub-task evidence back to parent acceptance criteria.

### Rework Loop

When a sub-agent result is insufficient:
- Identify the specific gap (which criterion failed, what evidence is missing)
- Send ONLY the delta: "Criterion X not met because Y. Fix Z and re-verify."
- Do NOT resend the full sub-handoff. Targeted rework is faster and clearer.
- Max 2 rework attempts per criterion. After that, escalate to CTO.

### Escalation

Escalate to CTO (report as blocker) when:
- Sub-agent fails same criterion 2+ times after rework
- Task requires a design decision not in the handoff contract
- Handoff scope is wrong or incomplete
- Access/permissions not available in current session

### Reporting to CTO

When the handoff is complete, report using this structure:
- **Summary:** 1-3 sentences on what was accomplished
- **Subtask ledger:** table — sub-task title | outcome (accepted/reworked/escalated) | attempts
- **Evidence:** key verification results (test output, commands run)
- **Parent criterion coverage:** map sub-task evidence → parent items using prefixes (TA=task acceptance, RA=room acceptance, V=validation)
- **Risks:** anything that might break or needs monitoring
- **Unresolved:** questions or issues discovered but not addressed
- **Recommendation:** what should happen next

Do NOT forward raw sub-agent output to CTO. Curate and summarize.
*This is an internal QA report. Official handoff review is pending CTO/reviewer decision.*

### Before Checkpoint or Compact

If you need to save a checkpoint or your session is about to compact, include the **subtask ledger** in the checkpoint `--note` field so the next session can resume without re-running completed sub-tasks. This is your responsibility — the checkpoint system does not automatically capture sub-task state.

### Discovery Handoffs

If the handoff kind is `discovery`, sub-agents should **research and report**, not write code:
- Gather evidence, compare options, map uncertainties
- Deliverables are findings documents, not code changes
- Acceptance criteria are about completeness of analysis, not passing tests"""


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

    # Team lead protocol
    lines.append("")
    lines.append(_render_team_lead_protocol())

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
