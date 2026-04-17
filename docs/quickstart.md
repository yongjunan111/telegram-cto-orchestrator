# Quickstart — Local Demo (No Telegram Required)

This guide walks you through a complete local orchestration demo in 5–10 minutes. No Telegram, no external services — just a tmux session and the CLI.

---

## Prerequisites

- Python 3.10+
- [uv](https://docs.astral.sh/uv/) (`pip install uv` or `curl -LsSf https://astral.sh/uv/install.sh | sh`)
- tmux (`apt install tmux` / `brew install tmux`)
- Optional: `claude` CLI (for automatic worker launch in dispatched sessions)

---

## 1. Install

```bash
git clone https://github.com/yongjunan111/telegram-cto-orchestrator.git && cd telegram-cto-orchestrator
uv sync
```

The `orchctl` script is the CLI entry point. Run it via the project venv:

```bash
# Activate venv (optional, but convenient)
source .venv/bin/activate

# Or prefix each command:
# .venv/bin/python orchctl <command>
```

The rest of this guide assumes the venv is active and you can run `orchctl` directly. If not, replace `orchctl` with `.venv/bin/python orchctl`.

---

## 2. Initialize

```bash
orchctl init
```

This creates the `.orchestrator/` directory structure:

```
.orchestrator/
  config.yaml          # Runtime config (permissions mode, etc.)
  peer_registry.yaml   # Known worker sessions
  active_programs.yaml # Active programs
  handoffs/            # Task delegation YAML files
  rooms/               # Isolated workspaces
  runtime/             # Derived runtime artifacts (sessions, dispatches, etc.)
```

If `.orchestrator/` already exists, `init` is a no-op for existing files.

---

## 3. Health Check

```bash
orchctl doctor
```

Checks that the directory structure is intact, config is valid, and the environment is ready. Review any warnings before proceeding.

---

## 4. Register a Worker Peer

A peer is a named worker session the orchestrator can dispatch work to.

```bash
orchctl peer add demo-worker --type worker --cwd /tmp/demo-project
```

This registers `demo-worker` in `.orchestrator/peer_registry.yaml` with working directory `/tmp/demo-project`.

List registered peers:

```bash
orchctl peer list
```

---

## 5. Create the Demo Project Directory

```bash
mkdir -p /tmp/demo-project
```

This is where the dispatched worker session will run.

---

## 6. Create a Room

A room is an isolated workspace for a task or conversation thread.

```bash
orchctl room create demo-task \
  --name "Demo Task" \
  --goal "Say hello from a worker session"
```

Verify it was created:

```bash
orchctl room list
orchctl room show demo-task
```

---

## 7. Set Room Memory

Room memory gives the orchestrator (and dispatched workers) operational context.

```bash
orchctl room memory demo-task \
  --execution-cwd /tmp/demo-project \
  --current-summary "Ready to dispatch"
```

---

## 8. Create a Handoff

A handoff is the structured delegation contract sent to a worker.

```bash
orchctl handoff create demo-impl \
  --room demo-task \
  --to demo-worker \
  --task "Create a hello.txt file with 'Hello from orchestrator'" \
  --priority low
```

Inspect the generated YAML:

```bash
orchctl handoff show demo-impl
```

---

## 9. Check the Dispatch Plan

Before dispatching, preview the allocation recommendation:

```bash
orchctl handoff dispatch-plan demo-impl
```

This outputs one of:
- `fresh_session` — no active session for this peer, a new tmux session will be created
- `reuse_existing_session` — an eligible idle session exists
- `wait_for_existing_assignment` — peer is already working on something
- `cannot_allocate` — a blocker or parse error prevents dispatch

For a clean run, you should see `fresh_session`.

---

## 10. Dispatch

```bash
orchctl handoff dispatch demo-impl
```

This creates a tmux session for `demo-worker` with the working directory set to `/tmp/demo-project`. The session receives shell environment hooks and a bootstrap document injected as context.

**Worker auto-launch:** If `claude` CLI is installed, a Claude worker will be launched automatically inside the tmux session. This is controlled by the global config option `dispatch.auto_launch_worker` (default: `true`). If `claude` is not installed, the tmux session is created but you must start the worker manually. To disable auto-launch, set `dispatch.auto_launch_worker: false` in `.orchestrator/config.yaml`.

**Permissions mode:** By default, Claude prompts for permission on tool use. This is a global setting in `.orchestrator/config.yaml` (not per-peer). For trusted local use:

```yaml
worker:
  permissions_mode: skip-permissions
```

---

## 11. Verify the Session

List active tmux sessions:

```bash
tmux list-sessions
```

You should see a session named after `demo-worker` (e.g. `orchctl-demo-worker` or similar). Attach to inspect it:

```bash
tmux attach -t <session-name>
```

Detach with `Ctrl-b d`.

---

## 12. Claim the Handoff

Before completing a handoff, the worker must claim it (transition from `open` to `claimed`):

```bash
orchctl handoff claim demo-impl --by demo-worker
```

Verify the status changed:

```bash
orchctl handoff show demo-impl
```

The status should now be `claimed`.

---

## 13. Complete the Handoff

Once the work is done (manually or by the worker), mark the handoff complete:

```bash
orchctl handoff complete demo-impl \
  --by demo-worker \
  --summary "Created hello.txt"
```

Check the final state:

```bash
orchctl handoff show demo-impl
orchctl room show demo-task
```

---

## 14. Clean Up

Kill the tmux session:

```bash
tmux kill-session -t <session-name>
```

Remove the room (deletes the room directory and state files):

```bash
# Rooms are directory-backed; remove manually if needed:
rm -rf .orchestrator/rooms/demo-task
```

Remove the peer registration:

```bash
orchctl peer remove demo-worker
```

---

## What's Next

- The full handoff lifecycle is: `create` → `dispatch` → `claim` → `complete` → `review` → `approve`. This guide covered the first five steps.
- Add a `type: reviewer` peer and try `orchctl handoff review` + `orchctl handoff approve` to complete the cycle
- Set `--kind discovery` on a handoff to use the discovery workflow
- Read `docs/architecture.md` for the full design
- Set up Telegram transport if you want remote access (see repository README)
- After `approve`, the orchestrator automatically suggests wiki updates based on the completed cycle. Run `orchctl handoff wiki-suggest <id>` to see suggestions manually.
