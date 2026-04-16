"""orchctl init — bootstrap the orchestrator directory structure."""
import os
import shutil
import sys

from . import storage
from .config import CONFIG_PATH, CONFIG_EXAMPLE_PATH


def cmd_init(args):
    """Initialize the orchestrator directory structure.

    Safe to run multiple times — only creates what is missing.
    """
    print("orchctl init")
    print("=" * 50)
    print()

    created = 0
    skipped = 0

    # 1. .orchestrator/ root
    created, skipped = _ensure_dir(storage.ORCHESTRATOR_DIR, ".orchestrator/", created, skipped)

    # 2. rooms/ and TEMPLATE
    created, skipped = _ensure_dir(storage.ROOMS_DIR, ".orchestrator/rooms/", created, skipped)
    template_state = os.path.join(storage.TEMPLATE_DIR, "state.yaml")
    if not os.path.isfile(template_state):
        # Template should come from the repo — if missing, we create a minimal one
        os.makedirs(storage.TEMPLATE_DIR, exist_ok=True)
        _write_template_state(template_state)
        _write_template_log(os.path.join(storage.TEMPLATE_DIR, "log.md"))
        print(f"  [CREATED] .orchestrator/rooms/TEMPLATE/")
        created += 1
    else:
        print(f"  [  OK   ] .orchestrator/rooms/TEMPLATE/")
        skipped += 1

    # 3. handoffs/
    created, skipped = _ensure_dir(storage.HANDOFFS_DIR, ".orchestrator/handoffs/", created, skipped)
    gitkeep = os.path.join(storage.HANDOFFS_DIR, ".gitkeep")
    if not os.path.isfile(gitkeep):
        with open(gitkeep, "w") as f:
            pass

    # 4. runtime/ and runtime/sessions/
    created, skipped = _ensure_dir(storage.RUNTIME_DIR, ".orchestrator/runtime/", created, skipped)
    created, skipped = _ensure_dir(storage.SESSIONS_DIR, ".orchestrator/runtime/sessions/", created, skipped)

    # Also create dispatches/, checkpoints/, bootstrap/ under runtime
    for subdir in ("dispatches", "checkpoints", "bootstrap"):
        path = os.path.join(storage.RUNTIME_DIR, subdir)
        created, skipped = _ensure_dir(path, f".orchestrator/runtime/{subdir}/", created, skipped)

    # 5. peer_registry.yaml
    if not os.path.isfile(storage.PEER_REGISTRY_PATH):
        storage.write_state(storage.PEER_REGISTRY_PATH, {"peers": []})
        print(f"  [CREATED] .orchestrator/peer_registry.yaml")
        created += 1
    else:
        print(f"  [  OK   ] .orchestrator/peer_registry.yaml")
        skipped += 1

    # 6. active_programs.yaml
    programs_path = os.path.join(storage.ORCHESTRATOR_DIR, "active_programs.yaml")
    if not os.path.isfile(programs_path):
        storage.write_state(programs_path, {"programs": []})
        print(f"  [CREATED] .orchestrator/active_programs.yaml")
        created += 1
    else:
        print(f"  [  OK   ] .orchestrator/active_programs.yaml")
        skipped += 1

    # 7. Config file
    if not os.path.isfile(CONFIG_PATH):
        if os.path.isfile(CONFIG_EXAMPLE_PATH):
            shutil.copy2(CONFIG_EXAMPLE_PATH, CONFIG_PATH)
            print(f"  [CREATED] .orchestrator/config.yaml (from config.example.yaml)")
        else:
            # Write a minimal config
            with open(CONFIG_PATH, "w") as f:
                f.write("# orchctl configuration — see config.example.yaml for all options\n")
                f.write("worker:\n")
                f.write("  permissions_mode: normal\n")
            print(f"  [CREATED] .orchestrator/config.yaml")
        created += 1
    else:
        print(f"  [  OK   ] .orchestrator/config.yaml")
        skipped += 1

    print()
    print(f"Done: {created} created, {skipped} already existed.")

    if created > 0:
        print()
        print("Next steps:")
        print("  1. Edit .orchestrator/config.yaml if needed")
        print("  2. Register a peer:  orchctl peer add my-worker --type worker --cwd /path/to/project")
        print("  3. Run health check: orchctl doctor")
        print("  4. Try the demo:     See docs/quickstart.md")


def _ensure_dir(path, label, created, skipped):
    if not os.path.isdir(path):
        os.makedirs(path, exist_ok=True)
        print(f"  [CREATED] {label}")
        return created + 1, skipped
    print(f"  [  OK   ] {label}")
    return created, skipped + 1


def _write_template_state(path):
    content = """\
# Room State Template
# Copy this directory to create a new room.
# state.yaml is the authoritative state; log.md is append-only.

room:
  id: "TEMPLATE"
  name: ""
  status: draft              # draft | active | paused | completed | archived
  program_id: null           # link to parent program
  created_at: null
  updated_at: null

context:
  goal: ""
  request_summary: ""        # original request summary from user/CTO
  current_summary: ""        # current situation summary (updated as work progresses)
  open_questions: []          # unanswered questions that may affect direction
  constraints: []
  acceptance_criteria: []
  execution_cwd: ""           # worker execution directory (required for dispatch)

lifecycle:
  current_phase: triage      # triage | planning | execution | review | done
  next_action: ""
  blocker_summary: ""        # human-readable summary of current blockers
  blocked_by: null

discovery:
  problem_statement: ""
  confirmed_facts: []
  assumptions: []
  options_considered: []
  decisions_made: []
  dependencies: []
  implementation_unknowns: []
  chosen_direction: ""
  readiness_notes: ""
"""
    with open(path, "w") as f:
        f.write(content)


def _write_template_log(path):
    with open(path, "w") as f:
        f.write("# Room Log\n")
