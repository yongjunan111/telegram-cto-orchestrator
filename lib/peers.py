"""Peer registry management CLI commands."""
import os
import sys

import yaml

from . import storage
from .validators import validate_slug


def cmd_peer_add(args):
    """Register a new peer in peer_registry.yaml."""
    peer_id = args.peer_id
    validate_slug(peer_id, "peer_id")

    peer_type = args.peer_type or "worker"
    if peer_type not in ("worker", "reviewer", "specialist"):
        print(f"Error: --type must be worker, reviewer, or specialist (got '{peer_type}').", file=sys.stderr)
        sys.exit(1)

    reg = _load_registry()
    peers = reg.get("peers") or []

    # Check for duplicate
    for p in peers:
        if isinstance(p, dict) and p.get("id") == peer_id:
            print(f"Error: peer '{peer_id}' already exists. Use 'peer show {peer_id}' to inspect.", file=sys.stderr)
            sys.exit(1)

    entry = {
        "id": peer_id,
        "name": args.name or peer_id,
        "type": peer_type,
        "cwd": args.cwd or "",
        "capabilities": [c.strip() for c in args.capabilities.split(",")] if args.capabilities else [],
        "status": "available",
        "last_seen": None,
    }

    peers.append(entry)
    reg["peers"] = peers
    _save_registry(reg)

    print(f"Peer '{peer_id}' added ({peer_type}).")
    if entry["cwd"]:
        print(f"  cwd: {entry['cwd']}")
    if entry["capabilities"]:
        print(f"  capabilities: {', '.join(entry['capabilities'])}")


def cmd_peer_list(args):
    """List all registered peers."""
    reg = _load_registry()
    peers = reg.get("peers") or []

    if not peers:
        print("No peers registered. Add one: orchctl peer add <id> --type worker")
        return

    print(f"{'ID':<20s} {'TYPE':<12s} {'STATUS':<12s} {'CWD'}")
    print("-" * 70)
    for p in peers:
        if not isinstance(p, dict):
            continue
        pid = p.get("id", "?")
        ptype = p.get("type", "?")
        status = p.get("status", "?")
        cwd = p.get("cwd", "") or ""
        print(f"{pid:<20s} {ptype:<12s} {status:<12s} {cwd}")


def cmd_peer_show(args):
    """Show detailed info for a single peer."""
    peer_id = args.peer_id
    validate_slug(peer_id, "peer_id")

    reg = _load_registry()
    peers = reg.get("peers") or []

    for p in peers:
        if isinstance(p, dict) and p.get("id") == peer_id:
            print(yaml.dump(p, default_flow_style=False, allow_unicode=True, sort_keys=False).strip())
            return

    print(f"Error: peer '{peer_id}' not found.", file=sys.stderr)
    sys.exit(1)


def cmd_peer_remove(args):
    """Remove a peer from the registry."""
    peer_id = args.peer_id
    validate_slug(peer_id, "peer_id")

    reg = _load_registry()
    peers = reg.get("peers") or []

    new_peers = [p for p in peers if not (isinstance(p, dict) and p.get("id") == peer_id)]

    if len(new_peers) == len(peers):
        print(f"Error: peer '{peer_id}' not found.", file=sys.stderr)
        sys.exit(1)

    reg["peers"] = new_peers
    _save_registry(reg)
    print(f"Peer '{peer_id}' removed.")


def cmd_peer_update(args):
    """Update an existing peer's fields."""
    peer_id = args.peer_id
    validate_slug(peer_id, "peer_id")

    reg = _load_registry()
    peers = reg.get("peers") or []

    target = None
    for p in peers:
        if isinstance(p, dict) and p.get("id") == peer_id:
            target = p
            break

    if target is None:
        print(f"Error: peer '{peer_id}' not found.", file=sys.stderr)
        sys.exit(1)

    changed = False
    if args.name is not None:
        target["name"] = args.name
        changed = True
    if args.peer_type is not None:
        if args.peer_type not in ("worker", "reviewer", "specialist"):
            print(f"Error: --type must be worker, reviewer, or specialist.", file=sys.stderr)
            sys.exit(1)
        target["type"] = args.peer_type
        changed = True
    if args.cwd is not None:
        target["cwd"] = args.cwd
        changed = True
    if args.status is not None:
        if args.status not in ("available", "busy", "offline"):
            print(f"Error: --status must be available, busy, or offline.", file=sys.stderr)
            sys.exit(1)
        target["status"] = args.status
        changed = True
    if args.capabilities is not None:
        target["capabilities"] = [c.strip() for c in args.capabilities.split(",")]
        changed = True

    if not changed:
        print("Nothing to update. Provide at least one field to change.")
        return

    _save_registry(reg)
    print(f"Peer '{peer_id}' updated.")


def _load_registry() -> dict:
    if not os.path.isfile(storage.PEER_REGISTRY_PATH):
        return {"peers": []}
    return storage.read_state(storage.PEER_REGISTRY_PATH)


def _save_registry(reg: dict) -> None:
    os.makedirs(os.path.dirname(storage.PEER_REGISTRY_PATH), exist_ok=True)
    storage.write_state(storage.PEER_REGISTRY_PATH, reg)
