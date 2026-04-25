"""Room-level promotion audit (V1, read-only).

`orchctl room gc-audit <room-id>` scans sessions bound to a room, classifies
each as `promoted | at-risk | unbound | parse-error`, and writes a YAML audit
report under `.orchestrator/runtime/gc-audits/<room-id>/`.

V1 rules (locked by the gc-audit design debate, docs/gc-audit-design-debate.md):

- Read-only except report file creation. No YAML / wiki / tmux / provider
  context mutation. No archive command stub. No `git fetch`.
- `audit_verdict` is computed from Tier 1 (authoritative) signals only:
  room / handoff / review / session YAML + peer registry + local git state.
- Tier 2 runtime observations (tmux pane liveness, etc.) are reported under
  `runtime_observation` but do not affect `audit_verdict`. `stale_tmux` is
  never a Tier 1 at-risk reason in V1.
- V1 report MUST NOT include top-level green-light fields
  (`safe_to_archive`, `archive_ready`, `can_archive`). Those belong to V2.
- Unbound sessions are neutral for room-level coherent calculation but are
  surfaced prominently as cleanup-not-eligible under V1.
- Report filenames use microsecond UTC timestamp plus monotonic suffix
  (`-001`, `-002`, ...) on same-microsecond collision.
"""
import os
import subprocess
import sys
from datetime import datetime, timezone

import yaml

from . import storage
from .handoffs import _derive_review_state, scan_room_handoffs
from .validators import is_slug_safe, require_room


# Runtime report location is computed at call time so tests can monkeypatch
# storage.RUNTIME_DIR.
def _gc_audit_dir() -> str:
    return os.path.join(storage.RUNTIME_DIR, "gc-audits")


# Reason codes -------------------------------------------------------------

# Tier 1 at-risk reason codes — the V2 consumer reads these verbatim, so this
# set is the contracted lock list. Emitters must not produce any code outside
# it without coordinating a contract bump. `stale_tmux` is deliberately absent
# (V1 design decision: tmux liveness is runtime_observation only).
AT_RISK_REASONS = {
    "pending_review",
    "changes_requested_pending",
    "dirty_git",
    "cwd_missing",
    "cwd_not_absolute",
    "session_busy",
    "ahead_of_remote",
    "behind_remote",
    "detached_head",
    "inside_submodule",
    "no_git_dir",
    "foreign_owner",
    "parse-error",
}

# Unbound reason codes. Unbound sessions are neutral for room-level coherence
# (not blockers for archival), so these are not part of the V2 lock list.
UNBOUND_REASONS = {
    "no_handoff_binding",
    "handoff_not_in_room",
    "missing_handoff_ref",
}


# Session scanning ---------------------------------------------------------

def _scan_room_sessions(room_id: str):
    """Return (sessions_for_room, parse_error_session_ids).

    Walks `.orchestrator/runtime/sessions/*.yaml`. A parse error surfaces
    regardless of room binding — we cannot tell which room a malformed file
    belongs to, so an operator should see it either way.
    """
    sessions = []
    parse_errors = []
    if not os.path.isdir(storage.SESSIONS_DIR):
        return sessions, parse_errors

    for fname in sorted(os.listdir(storage.SESSIONS_DIR)):
        if not fname.endswith(".yaml") or fname == ".gitkeep":
            continue
        path = os.path.join(storage.SESSIONS_DIR, fname)
        try:
            state = storage.read_state(path)
            if not isinstance(state, dict):
                raise ValueError("not a mapping")
            s = state.get("session", {})
            if not isinstance(s, dict):
                raise ValueError("missing 'session' section")
            if s.get("room_id") == room_id:
                sessions.append(state)
        except Exception:
            parse_errors.append(fname[:-5])
    return sessions, parse_errors


def _load_peer_ids() -> set:
    if not os.path.isfile(storage.PEER_REGISTRY_PATH):
        return set()
    try:
        reg = storage.read_state(storage.PEER_REGISTRY_PATH)
        peers = reg.get("peers") or []
        return {
            p.get("id")
            for p in peers
            if isinstance(p, dict) and p.get("id")
        }
    except Exception:
        return set()


# Runtime observations (Tier 2) -------------------------------------------

def _observe_tmux(tmux_session, tmux_target) -> dict:
    """Runtime observation only. Never feeds into `audit_verdict`."""
    observation = {
        "tmux_session": tmux_session or None,
        "tmux_target": tmux_target or None,
        "tmux_alive": "unknown",
        "observation_method": "none",
        "observed_at": storage.now_iso(),
    }
    if not tmux_session:
        observation["observation_method"] = "no_tmux_session"
        return observation

    try:
        proc = subprocess.run(
            ["tmux", "has-session", "-t", tmux_session],
            capture_output=True,
            text=True,
            timeout=2,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired, OSError):
        observation["observation_method"] = "tmux_unavailable"
        return observation

    if proc.returncode != 0:
        observation["tmux_alive"] = "dead"
        observation["observation_method"] = "has_session"
        return observation

    if not tmux_target:
        observation["tmux_alive"] = "alive"
        observation["observation_method"] = "has_session"
        return observation

    # Verify exact pane id (matches dispatch's exact-pane invariant).
    try:
        pane_proc = subprocess.run(
            ["tmux", "display-message", "-p", "-t", tmux_target, "#{pane_id}"],
            capture_output=True,
            text=True,
            timeout=2,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired, OSError):
        observation["observation_method"] = "tmux_unavailable"
        return observation

    if pane_proc.returncode == 0 and pane_proc.stdout.strip() == tmux_target:
        observation["tmux_alive"] = "alive"
        observation["observation_method"] = "display_message_pane"
    else:
        observation["tmux_alive"] = "dead"
        observation["observation_method"] = "display_message_pane"
    return observation


# Git state (Tier 1) -------------------------------------------------------

def _run_git(cwd, args, timeout=3):
    return subprocess.run(
        ["git", "-C", cwd, *args],
        capture_output=True,
        text=True,
        timeout=timeout,
    )


def _check_git(cwd, cache) -> dict:
    """Local git state only. Never fetches. Used to compute Tier 1 reason codes.

    Returns a dict with `available` plus (when available) `dirty`, `branch`,
    `head`, `detached_head`, `inside_submodule`, `has_upstream`, `ahead`,
    `behind`. When `available` is False, `reason` is one of the contracted
    unavailability codes: `cwd_missing`, `cwd_not_absolute`, `no_git_dir`.
    Callers may forward the reason verbatim into the session reason list.
    """
    if not cwd:
        return {"available": False, "reason": "cwd_missing"}
    if cwd in cache:
        return cache[cwd]

    result = {"available": False}

    if not os.path.isabs(cwd):
        result["reason"] = "cwd_not_absolute"
        cache[cwd] = result
        return result

    if not os.path.isdir(cwd):
        result["reason"] = "cwd_missing"
        cache[cwd] = result
        return result

    try:
        r = _run_git(cwd, ["rev-parse", "--git-dir"], timeout=3)
    except (FileNotFoundError, subprocess.TimeoutExpired, OSError):
        # git binary unavailable or hung — we cannot verify this cwd as a
        # git worktree, which from the V2 consumer's POV is indistinguishable
        # from "not a git dir".
        result["reason"] = "no_git_dir"
        cache[cwd] = result
        return result

    if r.returncode != 0:
        result["reason"] = "no_git_dir"
        cache[cwd] = result
        return result

    result["available"] = True

    # Dirty worktree check
    try:
        r = _run_git(cwd, ["status", "--porcelain"], timeout=5)
        if r.returncode == 0:
            result["dirty"] = bool(r.stdout.strip())
        else:
            result["dirty_check_failed"] = True
    except Exception:
        result["dirty_check_failed"] = True

    # Current branch / detached HEAD
    try:
        r = _run_git(cwd, ["rev-parse", "--abbrev-ref", "HEAD"], timeout=3)
        if r.returncode == 0:
            branch = r.stdout.strip()
            result["branch"] = branch
            # `git rev-parse --abbrev-ref HEAD` prints the literal string "HEAD"
            # when the worktree is in detached-HEAD state (post-checkout by sha).
            result["detached_head"] = (branch == "HEAD")
    except Exception:
        pass

    # HEAD sha
    try:
        r = _run_git(cwd, ["rev-parse", "HEAD"], timeout=3)
        if r.returncode == 0:
            result["head"] = r.stdout.strip()
    except Exception:
        pass

    # Submodule check. `--show-superproject-working-tree` prints the
    # superproject path when the current repo is a submodule, and empty
    # otherwise (returncode 0 in both cases).
    try:
        r = _run_git(
            cwd, ["rev-parse", "--show-superproject-working-tree"], timeout=3
        )
        result["inside_submodule"] = bool(
            r.returncode == 0 and r.stdout.strip()
        )
    except Exception:
        result["inside_submodule"] = False

    # ahead/behind vs upstream (no fetch)
    try:
        r = _run_git(
            cwd,
            ["rev-list", "--left-right", "--count", "@{upstream}...HEAD"],
            timeout=5,
        )
        if r.returncode == 0:
            parts = r.stdout.split()
            if len(parts) == 2:
                try:
                    result["behind"] = int(parts[0])
                    result["ahead"] = int(parts[1])
                    result["has_upstream"] = True
                except ValueError:
                    result["has_upstream"] = False
        else:
            result["has_upstream"] = False
    except Exception:
        result["has_upstream"] = False

    cache[cwd] = result
    return result


# Classification -----------------------------------------------------------

def _classify_session(s_state, handoffs_by_id, peer_ids, git_cache) -> dict:
    s = s_state.get("session", {})
    session_id = s.get("id") or "(unknown)"
    raw_handoff_id = s.get("handoff_id")
    peer_id = s.get("peer_id")
    cwd = s.get("cwd")
    tmux_target = s.get("tmux_target")
    tmux_session = s.get("tmux_session")
    session_status = s.get("status")

    runtime_observation = _observe_tmux(tmux_session, tmux_target)

    # Hardening: validate slug-safety of handoff_id BEFORE passing it to
    # storage.handoff_path(). A corrupt or tampered session YAML could smuggle
    # `../` sequences or shell-hostile bytes into a filesystem path that
    # downstream os.path.join + read_state would then open. Same invariant as
    # the checkpoint rework. Classify as parse-error and refuse to consume the
    # tainted binding.
    if raw_handoff_id is not None and not is_slug_safe(raw_handoff_id):
        return {
            "session_id": session_id,
            "audit_verdict": "parse-error",
            "reasons": ["parse-error"],
            "handoff_id": None,
            "handoff_status": None,
            "review_state": None,
            "peer_id": peer_id,
            "cwd": cwd,
            "runtime_observation": runtime_observation,
        }

    handoff_id = raw_handoff_id
    reasons = []
    handoff_status = None
    review_state = None

    # 1) Binding to a room handoff
    if not handoff_id:
        verdict = "unbound"
        reasons.append("no_handoff_binding")
    elif handoff_id not in handoffs_by_id:
        # Could be: handoff in another room, or handoff file deleted.
        # handoff_id is slug-safe at this point (guarded above), so it is
        # safe to pass through storage.handoff_path().
        ho_path = storage.handoff_path(handoff_id)
        if os.path.isfile(ho_path):
            verdict = "unbound"
            reasons.append("handoff_not_in_room")
        else:
            # A dangling reference is a harder case — not truly unbound, but
            # can't be promoted either. Keep it in the unbound bucket with a
            # distinct reason so operators can spot it.
            verdict = "unbound"
            reasons.append("missing_handoff_ref")
    else:
        ho_state = handoffs_by_id[handoff_id]
        h = ho_state.get("handoff", {})
        handoff_status = h.get("status")
        review_state = _derive_review_state(ho_state)

        # Session liveness (Tier 1). A busy session must not be archived; the
        # V2 consumer reads `session_busy` verbatim.
        if session_status == "busy":
            reasons.append("session_busy")

        # Peer ownership (Tier 1). A session pointing at a peer not in the
        # registry is effectively owned by a foreign/unknown worker; the
        # V2 consumer reads `foreign_owner` verbatim.
        if peer_id and peer_id not in peer_ids:
            reasons.append("foreign_owner")

        # Handoff lifecycle (Tier 1). The V2 lock list collapses every
        # non-approved terminal state into `pending_review` /
        # `changes_requested_pending`; operators can still inspect the
        # precise `handoff_status` / `review_state` fields for detail.
        if handoff_status == "completed":
            if review_state == "approved":
                pass  # promoted candidate
            elif review_state == "changes_requested":
                reasons.append("changes_requested_pending")
            else:
                # `pending_review` or any unrecognized review_state — either
                # way archival must wait for a review outcome.
                reasons.append("pending_review")
        else:
            # open / claimed / blocked / unknown handoff status — handoff has
            # not reached a reviewable terminal state, so archival is blocked.
            # The V2 lock list folds this into `pending_review`.
            reasons.append("pending_review")

        # Git state (Tier 1). `_check_git` emits contract-aligned
        # unavailability reasons (`cwd_missing`, `cwd_not_absolute`,
        # `no_git_dir`), which we forward verbatim.
        git_state = _check_git(cwd, git_cache)
        if not git_state.get("available"):
            reasons.append(git_state.get("reason") or "no_git_dir")
        else:
            if git_state.get("dirty"):
                reasons.append("dirty_git")
            if git_state.get("detached_head"):
                reasons.append("detached_head")
            if git_state.get("inside_submodule"):
                reasons.append("inside_submodule")
            if (git_state.get("ahead") or 0) > 0:
                reasons.append("ahead_of_remote")
            if (git_state.get("behind") or 0) > 0:
                reasons.append("behind_remote")

        verdict = "promoted" if not reasons else "at-risk"

    return {
        "session_id": session_id,
        "audit_verdict": verdict,
        "reasons": reasons,
        "handoff_id": handoff_id,
        "handoff_status": handoff_status,
        "review_state": review_state,
        "peer_id": peer_id,
        "cwd": cwd,
        "runtime_observation": runtime_observation,
    }


# Summary ------------------------------------------------------------------

def _compute_coherence(results, handoff_parse_errors) -> str:
    """Room-level verdict derived from Tier 1 session verdicts.

    - `unknown`: any parse errors prevent a definitive call.
    - `some-at-risk`: any session is at-risk.
    - `coherent`: at least one bound session and all bound sessions are promoted.
    - `neutral`: no bound sessions (only unbound or none).
    """
    if handoff_parse_errors:
        return "unknown"
    if any(r["audit_verdict"] == "parse-error" for r in results):
        return "unknown"
    if any(r["audit_verdict"] == "at-risk" for r in results):
        return "some-at-risk"
    bound = [
        r for r in results
        if r["audit_verdict"] not in ("unbound", "parse-error")
    ]
    if bound and all(r["audit_verdict"] == "promoted" for r in bound):
        return "coherent"
    return "neutral"


def _summarize(results, handoff_parse_errors) -> dict:
    counts = {
        "promoted": 0,
        "at-risk": 0,
        "unbound": 0,
        "parse-error": 0,
    }
    for r in results:
        key = r["audit_verdict"]
        counts[key] = counts.get(key, 0) + 1
    return {
        "total_sessions": len(results),
        "promoted_count": counts["promoted"],
        "at_risk_count": counts["at-risk"],
        "unbound_count": counts["unbound"],
        "parse_error_count": counts["parse-error"],
        "handoff_parse_error_count": len(handoff_parse_errors),
        "room_verdict": _compute_coherence(results, handoff_parse_errors),
    }


# Report writing -----------------------------------------------------------

def _report_filename(base_dir: str) -> str:
    """Pick `<YYYYMMDDTHHMMSS.ffffff>[-NNN].yaml` that does not yet exist.

    Microsecond-precision timestamp with a monotonic suffix on collision.
    """
    now = datetime.now(timezone.utc)
    ts = now.strftime("%Y%m%dT%H%M%S.%f")
    candidate = os.path.join(base_dir, f"{ts}.yaml")
    if not os.path.exists(candidate):
        return candidate
    for n in range(1, 1000):
        candidate = os.path.join(base_dir, f"{ts}-{n:03d}.yaml")
        if not os.path.exists(candidate):
            return candidate
    raise RuntimeError(
        "Exceeded 1000 microsecond-collision suffixes; refusing to proceed"
    )


def _write_report(room_id: str, report: dict) -> str:
    gc_dir = _gc_audit_dir()
    room_audit_dir = os.path.join(gc_dir, room_id)
    # safe_write_text creates the target parent as needed with symlink refusal.
    # Pre-create so we can pick a non-colliding filename deterministically.
    os.makedirs(room_audit_dir, exist_ok=True)
    target_path = _report_filename(room_audit_dir)

    content = yaml.dump(
        report, default_flow_style=False, allow_unicode=True, sort_keys=False
    )
    storage.safe_write_text(gc_dir, target_path, content)
    return target_path


# Output -------------------------------------------------------------------

def _print_summary(room_id: str, summary: dict, report_path: str) -> None:
    print(f"Room '{room_id}' gc-audit V1 (read-only).")
    print(f"  Total sessions:         {summary['total_sessions']}")
    print(f"  Promoted:               {summary['promoted_count']}")
    print(f"  At-risk:                {summary['at_risk_count']}")
    print(f"  Unbound:                {summary['unbound_count']}")
    print(f"  Parse-error:            {summary['parse_error_count']}")
    print(f"  Handoff parse-errors:   {summary['handoff_parse_error_count']}")
    print(f"  Room verdict:           {summary['room_verdict']}")
    print(f"Report: {report_path}")
    print(
        "Note: Runtime observations (tmux liveness) are not authoritative. "
        "audit_verdict is computed from Tier 1 signals only."
    )


# Public entrypoint --------------------------------------------------------

def cmd_room_gc_audit(args) -> None:
    room_id = args.room_id
    require_room(room_id)

    # Scan authoritative state.
    sessions, session_parse_errors = _scan_room_sessions(room_id)
    room_handoffs, handoff_parse_errors = scan_room_handoffs(room_id)
    peer_ids = _load_peer_ids()

    handoffs_by_id = {}
    for ho_state in room_handoffs:
        h = ho_state.get("handoff", {})
        hid = h.get("id")
        if hid:
            handoffs_by_id[hid] = ho_state

    git_cache = {}
    results = [
        _classify_session(s_state, handoffs_by_id, peer_ids, git_cache)
        for s_state in sessions
    ]

    for err_session_id in session_parse_errors:
        results.append({
            "session_id": err_session_id,
            "audit_verdict": "parse-error",
            "reasons": ["parse-error"],
            "handoff_id": None,
            "handoff_status": None,
            "review_state": None,
            "peer_id": None,
            "cwd": None,
            "runtime_observation": {
                "tmux_session": None,
                "tmux_target": None,
                "tmux_alive": "unknown",
                "observation_method": "skipped_parse_error",
                "observed_at": storage.now_iso(),
            },
        })

    summary = _summarize(results, handoff_parse_errors)

    # V1 report shape: no safe_to_archive / archive_ready / can_archive fields
    # at any level. The presence of any of those keys would violate the
    # handoff invariant and be rejected by tests.
    report = {
        "gc_audit": {
            "room_id": room_id,
            "generated_at": storage.now_iso(),
            "version": 1,
            "notes": (
                "Runtime observations are not authoritative. "
                "audit_verdict is computed from Tier 1 signals only "
                "(room/handoff/review/session YAML + peer registry + local "
                "git state). V1 is read-only except for creating this report."
            ),
        },
        "room_summary": summary,
        "sessions": results,
        "handoff_parse_errors": handoff_parse_errors,
    }

    try:
        report_path = _write_report(room_id, report)
    except Exception as e:
        print(f"Error: failed to write gc-audit report: {e}", file=sys.stderr)
        sys.exit(1)

    _print_summary(room_id, summary, report_path)
