#!/usr/bin/env python3
"""Worker launcher helper — launches Claude in interactive mode with bootstrap context.

Called by dispatch via tmux send-keys. Keeps the tmux command line short and
avoids quoting issues by reading the bootstrap path as a CLI argument.

Uses positional prompt (NOT -p) so Claude starts an interactive session that
stays alive after the initial response. -p is one-shot mode.
"""
import os
import sys


def main():
    if len(sys.argv) < 2:
        print("Usage: worker_launch.py <bootstrap-path>", file=sys.stderr)
        sys.exit(1)

    bootstrap_path = sys.argv[1]

    if not os.path.isfile(bootstrap_path):
        print(f"Error: bootstrap file not found: {bootstrap_path}", file=sys.stderr)
        sys.exit(1)

    prompt = f"Read {bootstrap_path} for your assignment and execute the task described in it."

    os.execvp("claude", [
        "claude",
        "--dangerously-skip-permissions",
        prompt,
    ])


if __name__ == "__main__":
    main()
