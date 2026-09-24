import re
from .config import DIVISIONS, DIVISIONS_60M, SPRINT_60M_ONLY_MODE

def active_divisions():
    return DIVISIONS_60M if SPRINT_60M_ONLY_MODE else DIVISIONS

def default_division_key():
    keys = list(active_divisions().keys())
    return keys[0] if keys else ""

def division_display_label(key):
    if SPRINT_60M_ONLY_MODE:
        return str(DIVISIONS_60M.get(key, key))
    return f"{key} - {DIVISIONS[key]}"

def coerce_division_key_for_options(value, options):
    if value in options:
        return value
    raw = str(value or "").strip()
    for option in options:
        if str(option).strip().lower() == raw.lower():
            return option
    old_to_60m = {
        "8": "U7",
        "2": "U9",
        "5": "U13",
        "6": "U16",
        "10": "U18",
        "1": "Masters",
    }
    mapped = old_to_60m.get(raw)
    if mapped in options:
        return mapped
    return options[0] if options else value

def division_value_for_storage(value):
    if SPRINT_60M_ONLY_MODE:
        return str(value or "").strip()
    return int(value)

def event_sort_key(event_name: str):
    name = str(event_name or "").strip()
    upper = name.upper()
    relay = re.search(r"\b(\d+)\s*[Xx]\s*(\d+(?:\.\d+)?)\s*M\b", upper)
    if relay:
        return (0, float(relay.group(1)) * float(relay.group(2)), upper)
    dist = re.search(r"\b(\d+(?:\.\d+)?)\s*(KM|M)\b", upper)
    if dist:
        value = float(dist.group(1))
        metres = value * 1000 if dist.group(2) == "KM" else value
        return (0, metres, upper)
    return (1, float("inf"), upper)

def allowed_events(gender: str, division_no):
    # Retained for compatibility. Phase 1 event choices now come from
    # COMPETITION_EVENTS via signup.pilot_config.
    if SPRINT_60M_ONLY_MODE:
        active_keys = {str(k).strip().lower() for k in DIVISIONS_60M.keys()}
        if str(division_no or "").strip().lower() in active_keys:
            return [("60M", "60")]
    return []
