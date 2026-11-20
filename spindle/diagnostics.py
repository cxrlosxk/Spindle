"""Privacy-conscious, read-only Apple Music Windows UI Automation inspection.

Designed to diagnose Microsoft Store Apple Music UI variants, multi-monitor
geometry, and unsupported accessibility patterns. Never sends data over network,
takes screenshots, or reads tokens. The user chooses whether to share the JSON.
"""
from __future__ import annotations

from collections import deque
from datetime import datetime, timezone
import re
import time

MAX_ELEMENTS = 1100
MAX_SECONDS = 14.0
# Keep only interface labels; song titles, artists, playlists, and accounts
# are private and never appear in the exported capture.
SAFE_NAME = re.compile(
    r"^(?:Search|Search Apple Music|Search Music|Search in Apple Music|"
    r"Playing Next|Play Next|Play Last|Play Later|Add to Queue|"
    r"No upcoming songs[.]?|More|More Options|"
    r"Songs|Content|Library|Browse|Home|Back|Cancel|"
    r"Apple Music|Clear Search|Show Queue|Queue|"
    r"Your Library|Search Results)$", re.IGNORECASE,
)
INTERESTING = re.compile(
    r"(search|query|nav|queue|song|playing|menu|more|popup|text|edit|"
    r"content|autocomplete|suggest|listview|sidebar|button|textbox)",
    re.IGNORECASE,
)
PATTERNS = {
    "invoke": 10000,
    "selection_item": 10010,
    "value": 10002,
    "toggle": 10015,
    "scroll_item": 10017,
    "legacy_accessible": 10018,
}


def public_name(name: str) -> str:
    """Private music/library names are omitted even if they contain a keyword."""
    if not isinstance(name, str):
        return ""
    name = name.strip()
    return name[:90] if SAFE_NAME.fullmatch(name) else "[redacted]" if name else ""


def public_id(value: str) -> str:
    """Automation IDs are developer identifiers, not private app content."""
    if not isinstance(value, str):
        return ""
    value = value[:130]
    if not re.fullmatch(r"[A-Za-z0-9_.:!-]*", value):
        return "[redacted-id]"
    return value


def safe_attr(element, attr: str, default=None):
    try:
        return getattr(element, attr)
    except Exception:
        return default


def rect_dict(rect):
    if rect is None:
        return None
    try:
        return {k: int(getattr(rect, k)) for k in ("left", "top", "right", "bottom")}
    except Exception:
        return None


def read_patterns(element):
    patterns = {}
    for name, pattern_id in PATTERNS.items():
        try:
            pointer = element.GetCurrentPattern(pattern_id)
            patterns[name] = bool(pointer)
        except Exception:
            patterns[name] = False
    return patterns


def inspect_window(adapter, *, max_elements=MAX_ELEMENTS, max_seconds=MAX_SECONDS):
    """Read a bounded snapshot of the actual Apple Music accessibility tree.

    This is intentionally passive. It does not click Search or otherwise alter
    the player's state, so it works before and after a failed Queue attempt.
    """
    started = time.monotonic()
    report = {
        "schema": 1,
        "captured_at_utc": datetime.now(timezone.utc).isoformat(),
        "read_only": True,
        "redaction": "Only approved Apple Music UI labels are retained; music and user names are redacted.",
        "window": {},
        "counts": {},
        "controls": [],
        "errors": [],
    }
    window = adapter._find_window()
    report["window"] = {
        "class": str(safe_attr(window, "CurrentClassName", ""))[:120],
        "bounds": rect_dict(safe_attr(window, "CurrentBoundingRectangle")),
    }
    try:
        report["monitor_count"] = int(adapter.user32.GetSystemMetrics(80))
    except Exception:
        report["monitor_count"] = None

    # Preserve ancestry/geometry so unnamed magnifying-glass buttons in a
    # compact sidebar can be located without guessing AutomationIds.
    todo = deque([(window, 0, -1)])
    inspected = 0
    matched = 0
    timed_out = False
    while todo and inspected < max_elements:
        if time.monotonic() - started > max_seconds:
            timed_out = True
            break
        element, depth, parent_index = todo.popleft()
        current_index = inspected
        inspected += 1
        kind = safe_attr(element, "CurrentControlType")
        aid = public_id(safe_attr(element, "CurrentAutomationId", ""))
        raw_name = safe_attr(element, "CurrentName", "")
        name = public_name(raw_name)
        cls = str(safe_attr(element, "CurrentClassName", ""))[:80]
        bounds = rect_dict(safe_attr(element, "CurrentBoundingRectangle"))
        visible = safe_attr(element, "CurrentIsOffscreen")
        # Record all named navigation/search controls plus shallow unnamed
        # elements. For deep controls, IDs help identify WinUI panel variants.
        relevant = (depth <= 2 or name not in ("", "[redacted]")
                    or bool(INTERESTING.search(aid + " " + cls)))
        if relevant and len(report["controls"]) < 400:
            row = {
                "index": current_index, "parent": parent_index, "depth": depth,
                "type": kind, "name": name, "automation_id": aid,
                "class": cls, "offscreen": visible if isinstance(visible, bool) else None,
                "bounds": bounds,
            }
            if bool(INTERESTING.search(aid + " " + cls)) or name not in ("", "[redacted]"):
                row["patterns"] = read_patterns(element)
            report["controls"].append(row)
            matched += 1
        if depth >= 10:
            continue
        try:
            children = adapter._all(element, scope=2)
        except Exception as exc:
            if len(report["errors"]) < 20:
                report["errors"].append({
                    "index": current_index, "depth": depth,
                    "error_type": type(exc).__name__,
                })
            continue
        for child in children:
            todo.append((child, depth + 1, current_index))
    report["counts"] = {
        "elements_visited": inspected, "controls_recorded": matched,
        "unvisited_estimate": len(todo), "timed_out": timed_out,
        "max_elements_reached": inspected >= max_elements,
        "seconds": round(time.monotonic() - started, 2),
    }
    return report
