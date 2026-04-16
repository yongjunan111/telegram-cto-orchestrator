"""Wiki suggestion module for handoff cycle accumulation.

Extracts structured suggestions from completed handoff cycles (approve/rework).
Never modifies wiki files — suggestion only.
"""
import re

from . import storage
from .handoffs import scan_room_handoffs


DEFER_KEYWORDS_EN = ["deferred", "defer", "out of scope", "later", "not now"]
DEFER_KEYWORDS_KR = ["나중에", "이번엔 안 함", "스코프 밖"]
DEFER_KEYWORDS = DEFER_KEYWORDS_EN + DEFER_KEYWORDS_KR


def _normalize(text: str) -> str:
    """Normalize hint text for deduplication."""
    return " ".join(text.lower().strip().split())


def _normalize_pattern(text: str) -> str:
    """Normalize pattern hints, stripping digits for count-independent matching."""
    return _normalize(re.sub(r'\d+', '', text))


def _collect_prior_hints_by_page(prior_handoff_states: list) -> dict:
    """Read stored hint fingerprints from prior handoff states for dedupe.

    Only dedupes against hints that were actually generated in prior cycles.
    If a prior handoff has no stored fingerprints (legacy/never ran wiki-suggest),
    no dedupe is applied from that handoff — this is the safe default.
    """
    merged = {k: set() for k in ("lessons", "decisions", "deferred", "patterns", "current_state")}
    for state in prior_handoff_states:
        stored = state.get("wiki_suggest", {}).get("generated_hints", {})
        for page_key in merged:
            for hint_text in (stored.get(page_key) or []):
                merged[page_key].add(hint_text)  # already normalized when stored
    return merged


def _store_generated_hints(handoff_id: str, delta: dict) -> None:
    """Store generated hint fingerprints in handoff state for future dedupe.

    Writes wiki_suggest.generated_hints to the handoff YAML.
    This is metadata only — does not modify wiki files.
    Best-effort: storage failure does not affect suggestion output.
    """
    fingerprints = {}
    for page_key, hints in delta["pages"].items():
        if page_key == "patterns":
            fingerprints[page_key] = [_normalize_pattern(h["hint"]) for h in hints]
        else:
            fingerprints[page_key] = [_normalize(h["hint"]) for h in hints]

    try:
        path = storage.handoff_path(handoff_id)
        state = storage.read_state(path)
        state["wiki_suggest"] = {"generated_hints": fingerprints}
        storage.write_state(path, state)
    except Exception:
        pass  # best-effort — don't break anything


def _walk_rework_chain(handoff_id: str) -> list:
    """Walk rework_of chain backwards from handoff_id.
    Returns list of ancestor handoff IDs (oldest first), NOT including handoff_id.
    Fail-soft: stops at broken/missing/cyclic references, returns partial chain.
    """
    ancestors = []
    visited = {handoff_id}  # include self to prevent cycles

    try:
        state = storage.read_state(storage.handoff_path(handoff_id))
    except Exception:
        return []

    parent_id = state.get("handoff", {}).get("rework_of")

    while parent_id:
        if parent_id in visited:
            break  # cycle protection
        visited.add(parent_id)
        try:
            parent_state = storage.read_state(storage.handoff_path(parent_id))
        except Exception:
            break  # broken chain — keep what we have (parent_id not added)
        ancestors.append(parent_id)
        parent_id = parent_state.get("handoff", {}).get("rework_of")

    ancestors.reverse()  # oldest first
    return ancestors


def detect_continuity(room_id: str, handoff_id: str) -> dict:
    """Detect whether this handoff is part of a continuous cycle.

    Returns:
        dict with keys: is_continuous, cycle_count, prior_handoffs, continuity_reason
    """
    # Load current handoff state to check rework_of
    try:
        current_state = storage.read_state(storage.handoff_path(handoff_id))
    except Exception:
        current_state = {}

    rework_of = current_state.get("handoff", {}).get("rework_of")
    if rework_of:
        chain = _walk_rework_chain(handoff_id)
        return {
            "is_continuous": True,
            "cycle_count": len(chain) + 1,  # chain ancestors + current
            "prior_handoffs": chain,
            "continuity_reason": "rework_lineage",
        }

    # Check for same-room handoffs with reviews
    room_handoffs, _ = scan_room_handoffs(room_id)
    reviewed = [
        s for s in room_handoffs
        if s.get("handoff", {}).get("id") != handoff_id
        and s.get("review", {}).get("outcome") in ("approved", "changes_requested")
    ]

    if reviewed:
        prior_ids = [s.get("handoff", {}).get("id") for s in reviewed if s.get("handoff", {}).get("id")]
        return {
            "is_continuous": True,
            "cycle_count": len(reviewed) + 1,
            "prior_handoffs": prior_ids,
            "continuity_reason": "same_room_prior_review",
        }

    return {
        "is_continuous": False,
        "cycle_count": 1,
        "prior_handoffs": [],
        "continuity_reason": "none",
    }


def build_wiki_delta(
    handoff_state: dict,
    room_state: dict,
    continuity: dict,
    source_event: str,
    prior_handoff_states: list,
) -> dict:
    """Extract wiki update hints from handoff/room state deterministically.

    Args:
        handoff_state: Current handoff YAML state dict.
        room_state: Current room YAML state dict.
        continuity: Result from detect_continuity().
        source_event: "approve" or "rework".
        prior_handoff_states: List of prior handoff state dicts for deduplication.

    Returns:
        dict with has_suggestions, source_event, strength, continuity_reason, pages.
    """
    strength = "high" if source_event == "approve" else "medium"
    prior_hints = _collect_prior_hints_by_page(prior_handoff_states)

    pages = {
        "lessons": [],
        "decisions": [],
        "deferred": [],
        "patterns": [],
        "current_state": [],
    }

    # --- lessons ---
    review = handoff_state.get("review", {})
    review_note = (review.get("note") or "").strip()
    if review_note:
        norm = _normalize(review_note)
        if norm not in prior_hints["lessons"]:
            pages["lessons"].append({
                "hint": review_note,
                "source": "review.note",
                "matched_text": None,
            })

    rework = handoff_state.get("rework", {})
    must_address = rework.get("must_address") or []
    for item in must_address:
        item_str = str(item).strip() if item else ""
        if not item_str:
            continue
        norm = _normalize(item_str)
        if norm not in prior_hints["lessons"]:
            pages["lessons"].append({
                "hint": item_str,
                "source": "rework.must_address",
                "matched_text": None,
            })

    # For rework (medium strength): only lessons and patterns
    if strength == "high":
        # --- decisions ---
        discovery = room_state.get("discovery", {})
        decisions_made = discovery.get("decisions_made") or []
        for dec in decisions_made:
            dec_str = str(dec).strip() if dec else ""
            if dec_str:
                norm = _normalize(dec_str)
                if norm not in prior_hints["decisions"]:
                    pages["decisions"].append({
                        "hint": dec_str,
                        "source": "discovery.decisions_made",
                        "matched_text": None,
                    })

        chosen = (discovery.get("chosen_direction") or "").strip()
        if chosen:
            norm = _normalize(chosen)
            if norm not in prior_hints["decisions"]:
                pages["decisions"].append({
                    "hint": chosen,
                    "source": "discovery.chosen_direction",
                    "matched_text": None,
                })

        # --- deferred ---
        resolution = handoff_state.get("resolution", {})
        risks = resolution.get("risks") or []
        for risk in risks:
            risk_str = str(risk).strip() if risk else ""
            if not risk_str:
                continue
            risk_lower = risk_str.lower()
            matched_kw = None
            for kw in DEFER_KEYWORDS:
                if kw.lower() in risk_lower:
                    matched_kw = kw
                    break
            if matched_kw is not None:
                norm = _normalize(risk_str)
                if norm not in prior_hints["deferred"]:
                    pages["deferred"].append({
                        "hint": risk_str,
                        "source": "resolution.risks",
                        "matched_text": matched_kw,
                    })

        # --- current_state ---
        # Only on meaningful state changes. Use cycle_count from continuity to determine
        # whether this is actually a first handoff vs. a later cycle with no loaded priors.
        current_phase = room_state.get("lifecycle", {}).get("current_phase", "")
        cycle_count_val = continuity.get("cycle_count", 1)

        if len(prior_handoff_states) == 0 and cycle_count_val == 1 and current_phase:
            # True first handoff for this room — new active stream
            hint_text = f"New active stream in room phase: {current_phase}"
            if _normalize(hint_text) not in prior_hints["current_state"]:
                pages["current_state"].append({
                    "hint": hint_text,
                    "source": "lifecycle.current_phase",
                    "matched_text": None,
                })

        # Check blocker resolved: room has no blocker but a prior handoff was blocked
        room_blocker = room_state.get("room", {}).get("blocker")
        prior_had_blocker = any(
            ps.get("handoff", {}).get("status") == "blocked"
            for ps in prior_handoff_states
        )
        if prior_had_blocker and not room_blocker:
            hint_text = "Blocker resolved — room is no longer blocked."
            if _normalize(hint_text) not in prior_hints["current_state"]:
                pages["current_state"].append({
                    "hint": hint_text,
                    "source": "room.blocker",
                    "matched_text": None,
                })

    # --- patterns (both strength levels) ---
    cycle_count = continuity.get("cycle_count", 1)
    rework_of = handoff_state.get("handoff", {}).get("rework_of")
    if cycle_count >= 3 and rework_of:
        hint_text = f"Repeated rework pattern detected: {cycle_count} cycles on this room."
        norm_pat = _normalize_pattern(hint_text)
        if norm_pat not in prior_hints["patterns"]:
            pages["patterns"].append({
                "hint": hint_text,
                "source": "continuity.cycle_count",
                "matched_text": None,
            })

    # Determine has_suggestions
    has_suggestions = any(len(v) > 0 for v in pages.values())

    return {
        "has_suggestions": has_suggestions,
        "source_event": source_event,
        "strength": strength,
        "continuity_reason": continuity["continuity_reason"],
        "cycle_count": continuity.get("cycle_count", 1),
        "pages": pages,
    }


def render_wiki_suggestions(handoff_id: str, room_id: str, delta: dict) -> str:
    """Render the wiki delta as a formatted string."""
    if not delta["has_suggestions"]:
        return f"No wiki updates suggested for handoff '{handoff_id}'."

    lines = []
    lines.append("--- wiki suggestions ---")
    lines.append("")
    lines.append(f"Source:            handoff `{handoff_id}` ({delta['source_event']})")
    lines.append(f"Room:              `{room_id}`")
    lines.append(f"Continuity:        {delta['cycle_count']} cycles ({delta['continuity_reason']})")
    lines.append(f"Strength:          {delta['strength']}")
    lines.append("")
    lines.append("## Suggested Wiki Updates")

    page_titles = {
        "lessons": "lessons.md",
        "decisions": "decisions.md",
        "deferred": "deferred.md",
        "patterns": "patterns.md",
        "current_state": "current-state.md",
    }

    for page_key in ("lessons", "decisions", "deferred", "patterns", "current_state"):
        hints = delta["pages"].get(page_key, [])
        if not hints:
            continue
        lines.append("")
        lines.append(f"### {page_titles[page_key]}")
        for hint in hints:
            source_label = hint.get("source", "")
            matched = hint.get("matched_text")
            suffix = f" (source: {source_label})" if source_label else ""
            if matched:
                suffix += f" [matched: '{matched}']"
            lines.append(f"- [HINT] {hint['hint']}{suffix}")

    lines.append("")
    lines.append("---")
    lines.append("*Read-only suggestion. No wiki files modified.")
    lines.append("The CTO (orchestrator session) reviews these hints and decides what to accumulate.*")

    return "\n".join(lines)


def cmd_handoff_wiki_suggest(args):
    """CLI: suggest wiki updates from a completed handoff cycle."""
    from .handoffs import _load_handoff_with_room
    handoff_id = args.handoff_id

    handoff_state, room_state = _load_handoff_with_room(handoff_id)

    # Validate: handoff must be completed with review outcome
    status = handoff_state.get("handoff", {}).get("status", "")
    if status != "completed":
        import sys
        print(
            f"Error: Handoff '{handoff_id}' is in '{status}' state. "
            f"wiki-suggest requires a completed handoff with a review outcome.",
            file=sys.stderr,
        )
        sys.exit(1)

    outcome = handoff_state.get("review", {}).get("outcome", "")
    if outcome not in ("approved", "changes_requested"):
        import sys
        print(
            f"Error: Handoff '{handoff_id}' has no review outcome (or outcome is '{outcome}'). "
            f"wiki-suggest requires 'approved' or 'changes_requested'.",
            file=sys.stderr,
        )
        sys.exit(1)

    room_id = handoff_state.get("handoff", {}).get("room_id", "")
    continuity = detect_continuity(room_id, handoff_id)

    if not continuity["is_continuous"]:
        print(f"Handoff '{handoff_id}' is a standalone task — no wiki suggestions for single-cycle handoffs.")
        return

    source_event = "approve" if outcome == "approved" else "rework"

    prior_states = []
    for pid in continuity.get("prior_handoffs", []):
        try:
            prior_states.append(storage.read_state(storage.handoff_path(pid)))
        except Exception:
            pass

    delta = build_wiki_delta(handoff_state, room_state, continuity, source_event, prior_states)

    # Manual command is read-only — no fingerprint storage.
    # Fingerprints are written only by the auto hook (_try_wiki_suggest_auto)
    # to preserve the original cycle-time snapshot.

    output = render_wiki_suggestions(handoff_id, room_id, delta)
    print(output)


def _try_wiki_suggest_auto(handoff_id, handoff_state, room_state, source_event):
    """Best-effort wiki-suggest after approve/rework. Never affects parent operation."""
    try:
        from .config import load_config
        config = load_config()
        if not config.get("wiki", {}).get("auto_suggest", True):
            return
        room_id = handoff_state.get("handoff", {}).get("room_id", "")
        if not room_id:
            return
        continuity = detect_continuity(room_id, handoff_id)
        if not continuity["is_continuous"]:
            return  # silent skip for standalone tasks
        prior_states = []
        for pid in continuity.get("prior_handoffs", []):
            try:
                prior_states.append(storage.read_state(storage.handoff_path(pid)))
            except Exception:
                pass
        delta = build_wiki_delta(handoff_state, room_state, continuity, source_event, prior_states)
        _store_generated_hints(handoff_id, delta)
        if not delta["has_suggestions"]:
            return
        output = render_wiki_suggestions(handoff_id, room_id, delta)
        print()  # blank line separator
        print(output)
    except Exception:
        pass  # wiki suggest failure never affects approve/rework
