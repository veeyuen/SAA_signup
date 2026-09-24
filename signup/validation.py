import re

IC_LAST4_RE = re.compile(r"^\d{3}[A-Za-z]$")
EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")

def normalize_ic_last4(value: str) -> str:
    return (value or "").strip().upper()

def is_valid_ic_last4(value: str) -> bool:
    return bool(IC_LAST4_RE.match((value or "").strip()))

def normalize_email(value: str) -> str:
    return (value or "").strip()

def is_valid_email(value: str) -> bool:
    value = normalize_email(value)
    return bool(value) and len(value) <= 254 and bool(EMAIL_RE.match(value))

def match_option_case_insensitive(value: str, options: list[str]) -> str:
    v = (value or "").strip()
    if not v:
        return ""
    folded = v.casefold()
    for option in options or []:
        if str(option or "").strip().casefold() == folded:
            return option
    return ""
