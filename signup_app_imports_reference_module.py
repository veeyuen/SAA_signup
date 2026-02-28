# Streamlit Athlete Signup App (Importing Hardcoded Lists)
# Generated on: 2026-02-28T02:16:53
#
# This app does NOT load the original Excel workbook for reference data.
# It imports dropdown reference lists from reference_lists.py.

import streamlit as st
import pandas as pd
import openpyxl
from io import BytesIO
from datetime import date

from reference_lists import (
    ENTRY_HEADERS,
    TEAM_CODES,
    get_team_name,
    COUNTRIES,
    get_events,
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

def compute_unique_id(first_name: str, ic_last4: str, dob: date) -> str:
    if not first_name or not ic_last4 or not dob:
        return ""
    return f"{first_name.strip()[:1]}{ic_last4.strip()[:4]}{dob.year % 100:02d}".upper()

def allowed_events(gender: str, division_no: int):
    """Return list of (event_name, event_code) allowed for dropdown."""
    if int(division_no) in (1, 2, 3, 4, 8):
        return get_events(gender, int(division_no))
    if int(division_no) in (5, 6, 7):
        return [("High Jump", "HJ"), ("Pole Vault", "PV")]
    return []

def export_entries_to_excel(header_info: dict, entries: pd.DataFrame) -> bytes:
    """Export a standalone Excel file without requiring the original template."""
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

st.set_page_config(page_title="Athletic Meet Signup", layout="wide")
st.title("Athletic Meet Signup")

if "entries" not in st.session_state:
    st.session_state.entries = []

with st.sidebar:
    st.header("Team / Billing")
    default_team_code = st.selectbox("Default Team Code (optional)", [""] + TEAM_CODES, index=0)
    default_team_name = get_team_name(default_team_code) if default_team_code else ""

    team_name = st.text_input("Team Name (for header)", value=default_team_name)
    billing_name = st.text_input("Billing contact name", value="")
    billing_email = st.text_input("Billing email", value="")
    charge_code = st.text_input("Charge code (optional)", value="")

st.subheader("Add athlete entry")

with st.form("entry_form", clear_on_submit=False):
    c1, c2, c3 = st.columns(3)
    last_name = c1.text_input("Last Name", "")
    first_name = c2.text_input("First Name", "")
    gender = c3.selectbox("Gender", ["M", "F"])

    c4, c5, c6 = st.columns(3)
    birth_date = c4.date_input("Birth Date", value=None)
    ic_last4 = c5.text_input("IC Number (last 4)", "")
    nationality = c6.selectbox("Nationality", COUNTRIES, index=0 if COUNTRIES else None)

    c7, c8 = st.columns(2)
    contact_number = c7.text_input("Contact Number", "")
    email = c8.text_input("Email", "")

    c9, c10 = st.columns(2)
    team_codes = TEAM_CODES
    if default_team_code and default_team_code in team_codes:
        default_index = team_codes.index(default_team_code)
    else:
        default_index = 0

    team_code = c9.selectbox("Team Code", team_codes, index=default_index)
    team_name_row = get_team_name(team_code)
    c10.text_input("Team Name (auto)", team_name_row, disabled=True)

    c11, c12 = st.columns(2)
    event_division = c11.selectbox(
        "Event Division (1–8)",
        options=list(DIVISIONS.keys()),
        format_func=lambda k: f"{k} - {DIVISIONS[k]}",
    )

    event_opts = allowed_events(gender, event_division)
    event_name = c12.selectbox("Event", [n for n, _ in event_opts] if event_opts else [])

    season_best = st.text_input("Season Best (optional)", "")
    emergency_contact_name = st.text_input("Emergency Contact Name", "")
    emergency_contact_number = st.text_input("Emergency Contact Number", "")
    coach_full_name = st.text_input("Coach Full Name", "")
    parq = st.selectbox("PAR-Q completed?", ["Y", "N"])

    unique_id = compute_unique_id(first_name, ic_last4, birth_date) if birth_date else ""
    st.caption(f"Unique ID (auto): **{unique_id or '—'}**")

    waiver_ok = st.checkbox("I acknowledge the waiver (as per the original form).", value=False)
    submitted = st.form_submit_button("Add entry")

if submitted:
    missing = []
    for k, v in [
        ("Last Name", last_name),
        ("First Name", first_name),
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
    elif len(ic_last4.strip()) < 4:
        st.error("IC last 4 should be at least 4 characters.")
    elif not event_opts:
        st.error("No events available for that Gender + Division combination.")
    else:
        event_code = dict(event_opts).get(event_name, "")
        st.session_state.entries.append({
            "last_name": last_name.strip(),
            "first_name": first_name.strip(),
            "gender": gender,
            "birth_date": birth_date,
            "ic_last4": ic_last4.strip(),
            "unique_id": unique_id,
            "nationality": nationality,
            "contact_number": contact_number.strip(),
            "email": email.strip(),
            "team_code": team_code,
            "team_name": team_name_row,
            "event_code": event_code,
            "event_division": int(event_division),
            "season_best": season_best.strip(),
            "emergency_contact_name": emergency_contact_name.strip(),
            "emergency_contact_number": emergency_contact_number.strip(),
            "coach_full_name": coach_full_name.strip(),
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
        "team_name": team_name,
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
