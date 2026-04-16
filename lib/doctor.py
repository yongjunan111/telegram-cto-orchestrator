"""orchctl doctor — environment and configuration health check."""
import os
import shutil
import subprocess
import sys

from . import storage
from .config import CONFIG_PATH, load_config


def cmd_doctor(args):
    """Run all health checks and report results."""
    checks = [
        ("Python version", _check_python),
        ("PyYAML importable", _check_pyyaml),
        ("tmux available", _check_tmux),
        ("claude CLI available", _check_claude),
        (".orchestrator/ directory", _check_orchestrator_dir),
        ("Room template", _check_template),
        ("Peer registry", _check_peer_registry),
        ("Handoffs directory", _check_handoffs_dir),
        ("Runtime directories", _check_runtime_dirs),
        ("Config file", _check_config),
    ]

    total = len(checks)
    passed = 0
    warnings = 0
    failed = 0

    print("orchctl doctor")
    print("=" * 50)
    print()

    for name, check_fn in checks:
        status, detail = check_fn()
        icon = {"ok": "OK", "warn": "WARN", "fail": "FAIL"}[status]
        print(f"  [{icon:4s}] {name}")
        if detail:
            for line in detail.splitlines():
                print(f"         {line}")

        if status == "ok":
            passed += 1
        elif status == "warn":
            warnings += 1
        else:
            failed += 1

    print()
    print(f"Result: {passed} passed, {warnings} warnings, {failed} failed (of {total})")

    if failed > 0:
        print()
        print("Fix the FAIL items above to use orchctl. Run 'orchctl init' to set up missing directories.")
        sys.exit(1)
    elif warnings > 0:
        print()
        print("Warnings are non-blocking but may affect some features.")


def _check_python():
    v = sys.version_info
    if v >= (3, 10):
        return "ok", f"Python {v.major}.{v.minor}.{v.micro}"
    return "fail", f"Python {v.major}.{v.minor}.{v.micro} — requires 3.10+"


def _check_pyyaml():
    try:
        import yaml
        return "ok", f"PyYAML {yaml.__version__}"
    except ImportError:
        return "fail", "PyYAML not installed. Run: uv sync"


def _check_tmux():
    path = shutil.which("tmux")
    if not path:
        return "fail", "tmux not found in PATH. Install: apt install tmux (or brew install tmux)"
    try:
        result = subprocess.run(["tmux", "-V"], capture_output=True, text=True, timeout=5)
        version = result.stdout.strip() if result.returncode == 0 else "unknown version"
        return "ok", f"{version} ({path})"
    except Exception:
        return "warn", f"tmux found at {path} but version check failed"


def _check_claude():
    config = load_config()
    claude_bin = config.get("worker", {}).get("claude_bin", "claude")

    path = shutil.which(claude_bin)
    if not path:
        if claude_bin != "claude":
            return "fail", f"'{claude_bin}' (from config) not found in PATH"
        return "warn", "claude CLI not found in PATH. Worker dispatch will not auto-launch."
    try:
        result = subprocess.run([path, "--version"], capture_output=True, text=True, timeout=10)
        version = result.stdout.strip() if result.returncode == 0 else "installed"
        return "ok", f"{version} ({path})"
    except Exception:
        return "ok", f"Found at {path} (version check skipped)"


def _check_orchestrator_dir():
    if os.path.isdir(storage.ORCHESTRATOR_DIR):
        return "ok", storage.ORCHESTRATOR_DIR
    return "fail", f"Missing: {storage.ORCHESTRATOR_DIR}\nRun: orchctl init"


def _check_template():
    template_state = os.path.join(storage.TEMPLATE_DIR, "state.yaml")
    if os.path.isfile(template_state):
        return "ok", storage.TEMPLATE_DIR
    return "fail", f"Missing: {template_state}\nRoom creation will fail. Run: orchctl init"


def _check_peer_registry():
    if os.path.isfile(storage.PEER_REGISTRY_PATH):
        try:
            from . import storage as st
            reg = st.read_state(storage.PEER_REGISTRY_PATH)
            peers = reg.get("peers") or []
            count = len(peers)
            if count == 0:
                return "warn", "Peer registry exists but has no peers. Add one: orchctl peer add <id> --type worker"
            return "ok", f"{count} peer(s) registered"
        except Exception as e:
            return "fail", f"Peer registry exists but cannot be parsed: {e}"
    return "warn", f"Missing: {storage.PEER_REGISTRY_PATH}\nRun: orchctl init"


def _check_handoffs_dir():
    if os.path.isdir(storage.HANDOFFS_DIR):
        return "ok", storage.HANDOFFS_DIR
    return "warn", f"Missing: {storage.HANDOFFS_DIR}\nWill be created on first handoff, or run: orchctl init"


def _check_runtime_dirs():
    issues = []
    for name, path in [
        ("runtime/", storage.RUNTIME_DIR),
        ("runtime/sessions/", storage.SESSIONS_DIR),
    ]:
        if not os.path.isdir(path):
            issues.append(f"Missing: {name}")
    if issues:
        return "warn", "\n".join(issues) + "\nWill be created on first dispatch, or run: orchctl init"
    return "ok", "runtime/ and runtime/sessions/ exist"


def _check_config():
    if os.path.isfile(CONFIG_PATH):
        try:
            config = load_config()
            mode = config.get("worker", {}).get("permissions_mode", "normal")
            return "ok", f"permissions_mode={mode}"
        except Exception as e:
            return "warn", f"Config exists but has errors: {e}"
    return "warn", f"No config.yaml found (using defaults). Copy config.example.yaml to config.yaml to customize."
