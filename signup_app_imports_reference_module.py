# Streamlit Athlete Signup App (Dynamic dependent dropdowns)
# Generated on: 2026-03-01T11:27:08
#
# Key fix:
# - Removed st.form wrapper around dependent widgets so Event dropdown updates immediately
#   when Gender/Division changes.
#
# This app does NOT load the original Excel workbook.

import re
from datetime import date
from io import BytesIO

import openpyxl
import pandas as pd
import streamlit as st

from name_suggestions import suggested_text_input, unique_preserve
from bigquery_names import bq_name_matches

from reference_lists import (
    ENTRY_HEADERS,
    TEAM_CODES,
    get_team_name,
    COUNTRIES,
    get_events,  # gender+division -> [(event_name,event_code), ...] filtered (blacked-out excluded)
)

DIVISIONS = {
    1: "U15 (13–14)",
    2: "U18 (15–17)",
    3: "U20 (18–19)",
    4: "Open (16+)",
    5: "Novice (Vertical Jumps Only)",
    6: "Intermediate (Vertical Jumps Only)",
    7: "Advance (Vertical Jumps Only)",
    8: "A Div (17–19)",
}

IC_LAST4_RE = re.compile(r"^\d{3}[A-Za-z]$")
EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")

def normalize_ic_last4(s: str) -> str:
    return (s or "").strip().upper()

def is_valid_ic_last4(s: str) -> bool:
    return bool(IC_LAST4_RE.match((s or "").strip()))

def normalize_email(s: str) -> str:
    return (s or "").strip()

def is_valid_email(s: str) -> bool:
    s = normalize_email(s)
    if not s or len(s) > 254:
        return False
    return bool(EMAIL_RE.match(s))

def compute_unique_id(first_name: str, ic_last4: str, dob: date) -> str:
    if not first_name or not ic_last4 or not dob:
        return ""
    ic = normalize_ic_last4(ic_last4)
    return f"{first_name.strip()[:1]}{ic[:4]}{dob.year % 100:02d}".upper()

def allowed_events(gender: str, division_no: int):
    """Return list of (event_name, event_code) for dropdown."""
    d = int(division_no)
    if d in (1, 2, 3, 4, 8):
        return get_events(gender, d)
    if d in (5, 6, 7):
        return [("High Jump", "HJ"), ("Pole Vault", "PV")]
    return []

def export_entries_to_excel(header_info: dict, entries: pd.DataFrame) -> bytes:
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "Entry Form"

    ws["A1"].value = "Team Name"
    ws["B1"].value = header_info.get("team_name", "")

    ws["A2"].value = "Billing Contact Name"
    ws["B2"].value = header_info.get("billing_name", "")

    ws["A3"].value = "Billing Email"
    ws["B3"].value = header_info.get("billing_email", "")

    ws["A4"].value = "Charge Code"
    ws["B4"].value = header_info.get("charge_code", "")

    header_row = 6
    for c, h in enumerate(ENTRY_HEADERS, start=1):
        ws.cell(header_row, c).value = h

    start_row = header_row + 1
    for idx, row in entries.reset_index(drop=True).iterrows():
        r = start_row + idx
        ws.cell(r, 1).value = idx + 1  # No
        ws.cell(r, 2).value = row.get("last_name", "")
        ws.cell(r, 3).value = row.get("first_name", "")
        ws.cell(r, 4).value = row.get("gender", "")
        ws.cell(r, 5).value = row.get("birth_date", None)
        ws.cell(r, 6).value = row.get("ic_last4", "")
        ws.cell(r, 7).value = row.get("unique_id", "")
        ws.cell(r, 8).value = row.get("nationality", "")
        ws.cell(r, 9).value = row.get("contact_number", "")
        ws.cell(r,10).value = row.get("email", "")
        ws.cell(r,11).value = row.get("team_code", "")
        ws.cell(r,12).value = row.get("team_name", "")
        ws.cell(r,13).value = row.get("event_code", "")
        ws.cell(r,14).value = row.get("event_division", "")
        ws.cell(r,15).value = row.get("season_best", "")
        ws.cell(r,16).value = row.get("emergency_contact_name", "")
        ws.cell(r,17).value = row.get("emergency_contact_number", "")
        ws.cell(r,18).value = row.get("coach_full_name", "")
        ws.cell(r,19).value = row.get("parq", "")

    bio = BytesIO()
    wb.save(bio)
    return bio.getvalue()


def _bq_suggest_select(label: str, key: str, *, limit: int = 25, min_chars: int = 2):
    """If BigQuery suggestions are enabled, show a selectbox of matches for current input."""
    if not (st.session_state.get("name_source") == "BigQuery"):
        return
    bq_project = (st.session_state.get("bq_project", "") or "").strip()
    bq_dataset = (st.session_state.get("bq_dataset", "") or "").strip()
    bq_table = (st.session_state.get("bq_table", "") or "").strip()
    bq_column = (st.session_state.get("bq_column", "NAME") or "NAME").strip()
    if not (bq_project and bq_dataset and bq_table and bq_column):
        return

    text = (st.session_state.get(key, "") or "").strip()
    if len(text) < int(min_chars):
        return

    try:
        matches = bq_name_matches(
            text,
            project=bq_project,
            dataset=bq_dataset,
            table=bq_table,
            column=bq_column,
            limit=int(limit),
        )
    except Exception as e:
        st.warning(f"BigQuery name suggestions error: {e}")
        return

    if not matches:
        return

    sel_key = f"{key}__bq_suggestion"
    options = ["(keep typed)"] + matches
    chosen = st.selectbox("Select From List of Matches :", options=options, key=sel_key)
    if chosen and chosen != "(keep typed)":
        # Avoid modifying the widget key after it is instantiated.
        st.session_state[f"{key}__pending"] = chosen
        # Reset suggestion selectbox on next rerun (must be before widget instantiation).
        st.session_state[f"{sel_key}__pending"] = "(keep typed)"
        st.rerun()

# ---------------- UI ----------------
st.set_page_config(page_title="Allcomers Meet Signup", layout="wide")
st.title("Allcomers Meet Signup")

def _apply_pending_text_updates():
    """Apply any pending text updates BEFORE widgets are instantiated."""
    pending = [k for k in list(st.session_state.keys()) if k.endswith("__pending")]
    for pk in pending:
        base = pk[:-9]  # strip '__pending'
        st.session_state[base] = st.session_state.get(pk, "")
        try:
            del st.session_state[pk]
        except Exception:
            pass

_apply_pending_text_updates()

if "entries" not in st.session_state:
    st.session_state.entries = []

with st.sidebar:
    st.header("Team / Billing")
    default_team_code = st.selectbox("Default Team Code (optional)", [""] + TEAM_CODES, index=0, key="default_team_code")
    default_team_name = get_team_name(default_team_code) if default_team_code else ""

    team_name_header = st.text_input("Team Name (for header)", value=default_team_name, key="team_name_header")
    # Billing contact name (with optional suggestions)
    if "known_full_names" in locals() and known_full_names:
        suggested_text_input("Billing contact name", key="billing_name", candidates=known_full_names)
        billing_name = st.session_state.get("billing_name", "")
        _bq_suggest_select("Billing contact name", "billing_name")
    else:
        billing_name = st.text_input("Billing contact name", value="", key="billing_name")
        _bq_suggest_select("Billing contact name", "billing_name")
    billing_email = st.text_input("Billing email", value="", key="billing_email")
    charge_code = st.text_input("Charge code (optional)", value="", key="charge_code")

    if billing_email and not is_valid_email(billing_email):
        st.warning("Billing email looks invalid. Please double-check it.")

    # Optional: load known names for suggestions
    st.divider()
    st.subheader("Name suggestions (optional)")
    name_source = st.radio("Source: CSV or BigQuery", options=["Off", "CSV", "BigQuery"], horizontal=True, key="name_source")
    bq_project = st.text_input("BigQuery Project (for suggestions)", value=(st.secrets.get("bq_project", "") if hasattr(st, "secrets") else ""), key="bq_project")
    bq_dataset = st.text_input("BigQuery Dataset", value=(st.secrets.get("bq_dataset", "") if hasattr(st, "secrets") else ""), key="bq_dataset")
    bq_table = st.text_input("BigQuery Table", value=(st.secrets.get("bq_table", "") if hasattr(st, "secrets") else ""), key="bq_table")
    bq_column = st.text_input("BigQuery Column", value=(st.secrets.get("bq_column", "NAME") if hasattr(st, "secrets") else "NAME"), key="bq_column")
    st.caption("Tip: Store bq_project/bq_dataset/bq_table/bq_column and gcp_service_account in secrets.toml for deployment.")

    uploaded_names = None
    if name_source == "CSV":
        uploaded_names = st.file_uploader(
        "Upload name list CSV (expects a column named NAME, or uses the first column)",
        type=["csv"],
        key="uploaded_names_csv",
    )

    known_full_names = []  # from CSV (if selected)
    bq_enabled = (name_source == "BigQuery" and bq_project and bq_dataset and bq_table and bq_column)

    if uploaded_names is not None:
        try:
            df_names = pd.read_csv(uploaded_names)
            col = "NAME" if "NAME" in df_names.columns else df_names.columns[0]
            known_full_names = unique_preserve(df_names[col].astype(str).tolist())
        except Exception:
            st.warning("Could not read CSV for name suggestions.")
            known_full_names = []  # from CSV (if selected)
    bq_enabled = (name_source == "BigQuery" and bq_project and bq_dataset and bq_table and bq_column)


    # Derive simple first/last name pools (best-effort; useful for your split fields)
    known_first_names = unique_preserve([n.split()[0] for n in known_full_names if str(n).strip()])
    known_last_names = unique_preserve([n.split()[-1] for n in known_full_names if str(n).strip()])

st.subheader("Athlete entry")

# Athlete fields (no form, so dependent dropdowns update immediately)
c1, c2 = st.columns(2)
with c1:
    # Full name (First + Last) with optional suggestions
    if ("known_full_names" in locals() and known_full_names and st.session_state.get("name_source") == "CSV"):
        suggested_text_input("Name (First + Last)", key="name", candidates=known_full_names)
    else:
        st.text_input("Name (First + Last)", key="name")
    _bq_suggest_select("Name", "name")
with c2:
    gender = st.selectbox("Gender", ["M", "F"], key="gender")
full_name = (st.session_state.get("name", "") or "").strip()
name_parts = [p for p in full_name.split() if p]
# Split rule: last token is Last Name; everything before is First Name (can include middle names)
derived_first_name = " ".join(name_parts[:-1]).strip() if len(name_parts) >= 2 else ""
derived_last_name = name_parts[-1].strip() if len(name_parts) >= 2 else ""
if full_name and len(name_parts) >= 2:
    st.caption(f"Parsed: **First Name** = {derived_first_name} | **Last Name** = {derived_last_name}")
elif full_name:
    st.caption("Parsed: please enter at least 2 words (First + Last).")

c4, c5, c6 = st.columns(3)
birth_date = c4.date_input("Birth Date", value=None, key="birth_date")
ic_last4 = c5.text_input("IC Number (last 4)", key="ic_last4")
nationality = c6.selectbox("Nationality", COUNTRIES, index=0 if COUNTRIES else 0, key="nationality")

c7, c8 = st.columns(2)
contact_number = c7.text_input("Contact Number", key="contact_number")
email = c8.text_input("Email", key="email")

c9, c10 = st.columns(2)
# Per-entry team selection; default from sidebar if present
if default_team_code and default_team_code in TEAM_CODES:
    if st.session_state.get("team_code") not in TEAM_CODES:
        st.session_state["team_code"] = default_team_code
team_code = c9.selectbox("Team Code", TEAM_CODES, key="team_code")
team_name_row = get_team_name(team_code)
c10.text_input("Team Name (auto)", team_name_row, disabled=True)

c11, c12 = st.columns(2)
event_division = c11.selectbox(
    "Event Division (1–8)",
    options=list(DIVISIONS.keys()),
    format_func=lambda k: f"{k} - {DIVISIONS[k]}",
    key="event_division",
)

event_opts = allowed_events(gender, int(event_division))
event_names = [n for n, _ in event_opts]

# Keep event selection consistent when options change
if event_names:
    if st.session_state.get("event_name") not in event_names:
        st.session_state["event_name"] = event_names[0]
else:
    st.session_state["event_name"] = ""

event_name = c12.selectbox("Event", event_names if event_names else ["(no events)"], key="event_name")

season_best = st.text_input("Season Best (optional)", key="season_best")
if "known_full_names" in locals() and known_full_names:
    suggested_text_input("Emergency Contact Name", key="emergency_contact_name", candidates=known_full_names)
    _bq_suggest_select("Emergency Contact Name", "emergency_contact_name")
else:
    st.text_input("Emergency Contact Name", key="emergency_contact_name")
    _bq_suggest_select("Emergency Contact Name", "emergency_contact_name")
emergency_contact_name = st.session_state.get("emergency_contact_name", "")
emergency_contact_number = st.text_input("Emergency Contact Number", key="emergency_contact_number")
if "known_full_names" in locals() and known_full_names:
    suggested_text_input("Coach Full Name", key="coach_full_name", candidates=known_full_names)
    _bq_suggest_select("Coach Full Name", "coach_full_name")
else:
    st.text_input("Coach Full Name", key="coach_full_name")
    _bq_suggest_select("Coach Full Name", "coach_full_name")
coach_full_name = st.session_state.get("coach_full_name", "")
parq = st.selectbox("PAR-Q completed?", ["Y", "N"], key="parq")

ic_last4_norm = normalize_ic_last4(ic_last4)
email_norm = normalize_email(email)
unique_id = compute_unique_id(derived_first_name, ic_last4_norm, birth_date) if birth_date else ""
st.caption(f"Unique ID (auto): **{unique_id or '—'}**")

waiver_ok = st.checkbox("I acknowledge the waiver (as per the original form).", value=False, key="waiver_ok")

# Add entry button
if st.button("Add entry", type="primary"):
    missing = []
    for k, v in [
        ("Name", full_name),
        ("Birth Date", birth_date),
        ("IC last 4", ic_last4),
        ("Email", email),
        ("Contact Number", contact_number),
    ]:
        if not v:
            missing.append(k)

    if not waiver_ok:
        st.error("Please tick the waiver acknowledgement.")
    elif missing:
        st.error("Missing: " + ", ".join(missing))
    elif full_name and len(name_parts) < 2:
        st.error("Name must contain at least 2 words (First + Last).")
    elif not is_valid_email(email_norm):
        st.error("Please enter a valid email address (e.g., name@example.com).")
    elif not is_valid_ic_last4(ic_last4_norm):
        st.error("IC last 4 must be 3 digits followed by 1 letter (e.g., 123A).")
    elif not event_opts or not event_names or event_name == "(no events)":
        st.error("No events available for that Gender + Division combination.")
    else:
        event_code = dict(event_opts).get(event_name, "")
        st.session_state.entries.append({
            "name": full_name,
            "last_name": derived_last_name,
            "first_name": derived_first_name,
            "gender": gender,
            "birth_date": birth_date,
            "ic_last4": ic_last4_norm,
            "unique_id": unique_id,
            "nationality": nationality,
            "contact_number": (contact_number or "").strip(),
            "email": email_norm,
            "team_code": team_code,
            "team_name": team_name_row,
            "event_code": event_code,
            "event_division": int(event_division),
            "season_best": (season_best or "").strip(),
            "emergency_contact_name": (emergency_contact_name or "").strip(),
            "emergency_contact_number": (emergency_contact_number or "").strip(),
            "coach_full_name": (coach_full_name or "").strip(),
            "parq": parq,
        })
        st.success("Entry added.")

st.subheader("Current entries")
entries_df = pd.DataFrame(st.session_state.entries)
st.dataframe(entries_df, use_container_width=True)

cA, cB, cC = st.columns([1, 1, 2])
with cA:
    if st.button("Clear all entries"):
        st.session_state.entries = []
        st.rerun()

if not entries_df.empty:
    header_info = {
        "team_name": team_name_header,
        "billing_name": billing_name,
        "billing_email": billing_email,
        "charge_code": charge_code,
    }

    xlsx_bytes = export_entries_to_excel(header_info, entries_df)
    csv_bytes = entries_df.to_csv(index=False).encode("utf-8")

    with cB:
        st.download_button(
            "Download Excel",
            data=xlsx_bytes,
            file_name="Allcomers_Meet_Entries.xlsx",
            mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        )
    with cC:
        st.download_button(
            "Download CSV",
            data=csv_bytes,
            file_name="Allcomers_Meet_Entries.csv",
            mime="text/csv",
        )
