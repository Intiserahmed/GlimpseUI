"""
Multi-strategy element resolution for iOS and Android.

Instead of relying on a single selector, tries multiple strategies
in order from most-stable to least-stable. This fixes maintenance
issues where tests break because an element label changed slightly.

Resolution order (cheapest/most stable first):
  1. accessibilityId / resource-id  — survives layout changes
  2. Exact label match              — survives position changes
  3. Fuzzy label match              — survives minor text rewrites
  4. Type + index                   — last structural fallback
  5. Cached coords                  — pixel fallback (least stable)

Usage:
    from agent.mobile_element import resolve_element

    el = resolve_element(step, platform="ios")
    if el:
        cx, cy = el["cx"], el["cy"]
    else:
        # escalate to AI healer
"""

import requests
from dataclasses import dataclass
from typing import Optional

BRIDGE_URL = "http://localhost:22087"


@dataclass
class ResolvedElement:
    cx: int
    cy: int
    label: str
    identifier: str
    strategy: str   # which strategy found it — useful for debugging


# ── Tree fetchers ─────────────────────────────────────────────────────────────

def _ios_tree(bridge_url: str = BRIDGE_URL) -> list:
    try:
        r = requests.post(f"{bridge_url}/viewHierarchy", timeout=5)
        return r.json().get("elements", [])
    except Exception:
        return []


def _android_tree(device_serial: str = None) -> list:
    """Fetch Android UI elements via uiautomator2."""
    try:
        import uiautomator2 as u2
        d    = u2.connect(device_serial)
        dump = d.dump_hierarchy()
        # Parse XML → flat element list
        import xml.etree.ElementTree as ET
        root     = ET.fromstring(dump)
        elements = []
        for node in root.iter("node"):
            bounds = node.get("bounds", "[0,0][0,0]")
            try:
                parts  = bounds.replace("][", ",").strip("[]").split(",")
                x1, y1, x2, y2 = int(parts[0]), int(parts[1]), int(parts[2]), int(parts[3])
                elements.append({
                    "label":      node.get("content-desc", ""),
                    "identifier": node.get("resource-id", ""),
                    "text":       node.get("text", ""),
                    "type":       node.get("class", "").split(".")[-1],
                    "enabled":    node.get("enabled", "true") == "true",
                    "x": x1, "y": y1,
                    "w": x2 - x1, "h": y2 - y1,
                })
            except (ValueError, IndexError):
                pass
        return elements
    except Exception:
        return []


def _get_tree(platform: str, bridge_url: str = BRIDGE_URL,
              device_serial: str = None) -> list:
    if platform == "android":
        return _android_tree(device_serial)
    return _ios_tree(bridge_url)


def _center(el: dict) -> tuple[int, int]:
    return int(el["x"] + el["w"] / 2), int(el["y"] + el["h"] / 2)


# ── Resolution strategies ─────────────────────────────────────────────────────

def _by_accessibility_id(elements: list, acc_id: str) -> Optional[dict]:
    """Strategy 1: accessibilityIdentifier (iOS) / resource-id (Android)."""
    for el in elements:
        if el.get("identifier", "") == acc_id:
            return el
    return None


def _by_exact_label(elements: list, label: str) -> Optional[dict]:
    """Strategy 2: exact label / content-desc / text match."""
    label_lower = label.lower()
    for el in elements:
        if el.get("label", "").lower() == label_lower:
            return el
        if el.get("text", "").lower() == label_lower:
            return el
    return None


def _by_fuzzy_label(elements: list, label: str) -> Optional[dict]:
    """
    Strategy 3: fuzzy word-overlap match.
    Handles 'Sign In' → 'Log In', 'Submit Form' → 'Submit'.
    Returns the best match above a 60% word-overlap threshold.
    """
    label_words = set(label.lower().split())
    best, best_score = None, 0.0

    for el in elements:
        for field in ("label", "text"):
            val   = el.get(field, "")
            if not val:
                continue
            words   = set(val.lower().split())
            overlap = len(label_words & words) / max(len(label_words), 1)
            if overlap > 0.6 and overlap > best_score:
                best, best_score = el, overlap

    return best


def _by_type_index(elements: list, el_type: str, index: int = 0) -> Optional[dict]:
    """Strategy 4: nth element of given type."""
    matches = [e for e in elements
               if el_type.lower() in e.get("type", "").lower()
               and e.get("enabled", True)]
    return matches[index] if index < len(matches) else None


# ── Main resolver ─────────────────────────────────────────────────────────────

def resolve_element(
    step: dict,
    platform:      str = "ios",
    bridge_url:    str = BRIDGE_URL,
    device_serial: str = None,
) -> Optional[ResolvedElement]:
    """
    Find an element using multiple strategies, cheapest first.
    Returns a ResolvedElement with pixel coordinates, or None.

    Also updates the step dict with the winning strategy's values
    so the cache stays fresh for future runs.
    """
    elements = _get_tree(platform, bridge_url, device_serial)

    # Strategy 1: accessibilityId
    if acc_id := step.get("accessibilityId"):
        el = _by_accessibility_id(elements, acc_id)
        if el:
            cx, cy = _center(el)
            return ResolvedElement(cx=cx, cy=cy,
                                   label=el.get("label", ""),
                                   identifier=acc_id,
                                   strategy="accessibilityId")

    # Strategy 2: exact label
    if label := step.get("label"):
        el = _by_exact_label(elements, label)
        if el:
            cx, cy = _center(el)
            # Update cache if accessibilityId now available
            if el.get("identifier") and not step.get("accessibilityId"):
                step["accessibilityId"] = el["identifier"]
            return ResolvedElement(cx=cx, cy=cy,
                                   label=label,
                                   identifier=el.get("identifier", ""),
                                   strategy="exact_label")

    # Strategy 3: fuzzy label
    if label := step.get("label"):
        el = _by_fuzzy_label(elements, label)
        if el:
            cx, cy = _center(el)
            # Heal the label in cache to the new text
            step["label"] = el.get("label") or el.get("text", label)
            if el.get("identifier"):
                step["accessibilityId"] = el["identifier"]
            return ResolvedElement(cx=cx, cy=cy,
                                   label=step["label"],
                                   identifier=el.get("identifier", ""),
                                   strategy="fuzzy_label")

    # Strategy 4: type + index
    if el_type := step.get("type"):
        el = _by_type_index(elements, el_type, step.get("index", 0))
        if el:
            cx, cy = _center(el)
            return ResolvedElement(cx=cx, cy=cy,
                                   label=el.get("label", ""),
                                   identifier=el.get("identifier", ""),
                                   strategy="type_index")

    # Strategy 5: cached coords fallback
    if coords := step.get("coords"):
        return ResolvedElement(cx=coords[0], cy=coords[1],
                               label=step.get("label", ""),
                               identifier="",
                               strategy="cached_coords")

    return None  # all strategies failed → caller escalates to healer


# ── Nearest-element helper (used by compiler) ─────────────────────────────────

def _find_id_near(elements: list, cx: int, cy: int, max_dist: int = 80) -> Optional[str]:
    """
    Return the accessibilityIdentifier / resource-id of the element whose
    centre is closest to (cx, cy), within max_dist pixels.
    Returns None if no element is close enough or has an identifier.
    """
    best_el, best_d = None, float("inf")
    for el in elements:
        ecx, ecy = _center(el)
        d = ((ecx - cx) ** 2 + (ecy - cy) ** 2) ** 0.5
        if d < best_d:
            best_d, best_el = d, el
    if best_el is not None and best_d <= max_dist:
        return best_el.get("identifier") or None
    return None


# ── Convenience: get all tappable elements ────────────────────────────────────

def get_tappable(platform: str = "ios", bridge_url: str = BRIDGE_URL,
                 device_serial: str = None) -> list[dict]:
    """Return all interactive elements on the current screen."""
    TAPPABLE_TYPES = {
        "ios":     {"Button", "Cell", "Link", "Switch", "Tab",
                    "MenuItem", "TextField", "SecureTextField"},
        "android": {"Button", "ImageButton", "CheckBox", "RadioButton",
                    "Switch", "Spinner", "EditText"},
    }
    allowed = TAPPABLE_TYPES.get(platform, set())
    elements = _get_tree(platform, bridge_url, device_serial)
    return [
        e for e in elements
        if e.get("type", "") in allowed and e.get("enabled", True)
    ]
