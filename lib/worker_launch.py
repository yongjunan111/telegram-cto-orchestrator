#!/usr/bin/env python3
"""Worker launcher helper — launches Claude in interactive mode with bootstrap context.

Called by dispatch via tmux send-keys. Keeps the tmux command line short and
avoids quoting issues by reading the bootstrap path as a CLI argument.

Uses positional prompt (NOT -p) so Claude starts an interactive session that
stays alive after the initial response. -p is one-shot mode.

This script runs as a standalone process (not as a package import), so it
reads config directly from .orchestrator/config.yaml rather than using
relative imports.
"""
import os
import sys


def _load_config_standalone():
    """Load config.yaml from the .orchestrator dir relative to this script's repo root."""
    try:
        import yaml
    except ImportError:
        return {}

    # Repo root is two levels up from lib/worker_launch.py
    repo_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    config_path = os.path.join(repo_root, ".orchestrator", "config.yaml")

    if not os.path.isfile(config_path):
        return {}
    try:
        with open(config_path, "r") as f:
            return yaml.safe_load(f) or {}
    except Exception:
        return {}


def build_cmd(bootstrap_path: str, config: dict) -> list:
    """Build the claude CLI command from config. Separated for testability."""
    claude_bin = config.get("worker", {}).get("claude_bin", "claude") or "claude"
    mode = config.get("worker", {}).get("permissions_mode", "normal")

    cmd = [claude_bin]
    if mode == "skip-permissions":
        cmd.append("--dangerously-skip-permissions")
    elif mode == "auto":
        cmd.extend(["--permission-mode", "auto"])

    # Set model from config (e.g., "opus" for team lead sessions)
    model = config.get("worker", {}).get("model")
    if model:
        cmd.extend(["--model", str(model)])

    # MCP servers: claude-peers is typically configured globally via 'claude mcp add'.
    # Workers inherit global MCP config automatically.

    prompt = f"Read {bootstrap_path} for your assignment and execute the task described in it."
    cmd.append(prompt)
    return cmd


def main():
    if len(sys.argv) < 2:
        print("Usage: worker_launch.py <bootstrap-path>", file=sys.stderr)
        sys.exit(1)

    bootstrap_path = sys.argv[1]

    if not os.path.isfile(bootstrap_path):
        print(f"Error: bootstrap file not found: {bootstrap_path}", file=sys.stderr)
        sys.exit(1)

    config = _load_config_standalone()
    cmd = build_cmd(bootstrap_path, config)
    claude_bin = cmd[0]

    os.execvp(claude_bin, cmd)


if __name__ == "__main__":
    main()
