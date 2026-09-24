import datetime as dt
import re
from .validation import normalize_ic_last4

def normalize_header(value: str) -> str:
    value = (value or "").strip().casefold()
    return re.sub(r"[^a-z0-9]+", "_", value).strip("_")

def gender_to_code(value: str) -> str:
    value = (value or "").strip().casefold()
    if value in ("m", "male"):
        return "M"
    if value in ("f", "female"):
        return "F"
    return ""

def code_to_gender_display(value: str) -> str:
    value = (value or "").strip().upper()
    if value == "M":
        return "Male"
    if value == "F":
        return "Female"
    return ""

def compute_unique_id(first_name: str, ic_last4: str, dob) -> str:
    if not first_name or not ic_last4 or not dob:
        return ""
    ic = normalize_ic_last4(ic_last4)
    return f"{first_name.strip()[:1]}{ic[:4]}{dob.year % 100:02d}".upper()

def safe_date_max(value):
    maximum = dt.date.today()
    try:
        if isinstance(value, dt.date) and value > maximum:
            return value
    except Exception:
        pass
    return maximum
