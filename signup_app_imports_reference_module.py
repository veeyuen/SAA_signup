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

from google_sheets_roster import load_roster, parse_dob, last4_from_nric


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


def _match_option_case_insensitive(value: str, options: list[str]) -> str:
    """Return the option whose casefold matches value; else empty string."""
    v = (value or "").strip()
    if not v:
        return ""
    vcf = v.casefold()
    for o in options:
        if (o or "").strip().casefold() == vcf:
            return o
    return ""

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

    ws["A5"].value = "P/O to be sent"
    ws["B5"].value = header_info.get("po_to_be_sent", "")

    header_row = 7
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
    st.subheader("Roster search (Google Sheet)")
    roster_sheet_url = st.text_input(
        "Roster Google Sheet URL",
        value=st.secrets.get("ROSTER_SHEET_URL", "https://docs.google.com/spreadsheets/d/1PnTKatJGW3Eazy6YpDqnRHVZrVLRvK9rA_vBIVXKfUU/edit?usp=sharing"),
        key="roster_sheet_url",
    )
    roster_worksheet = st.text_input("Worksheet (optional)", value=st.secrets.get("ROSTER_WORKSHEET", ""), key="roster_worksheet")
    use_roster = st.toggle("Enable roster search", value=True, key="use_roster")
    if st.button("Refresh roster cache"):
        load_roster.clear()
        st.toast("Roster cache cleared.")
    st.caption("Sheet columns expected: LAST_NAME, OTHER_NAME, NRIC, DOB, NATIONALITY, UNIQUE_ID, TEAM_NAME, TEAM_CODE")
    po_to_be_sent = st.radio("P/O to be sent", options=["No", "Yes"], index=0, horizontal=True, key="po_to_be_sent")
    default_team_code = st.selectbox("Default Team Code (optional)", [""] + TEAM_CODES, index=0, key="default_team_code")
    default_team_name = get_team_name(default_team_code) if default_team_code else ""

    team_name_header = st.text_input("Team Name (for header)", value=default_team_name, key="team_name_header")
    billing_name = st.text_input("Billing contact name", value="", key="billing_name")
    billing_email = st.text_input("Billing email", value="", key="billing_email")
    charge_code = st.text_input("Charge code (optional)", value="", key="charge_code")

    if billing_email and not is_valid_email(billing_email):
        st.warning("Billing email looks invalid. Please double-check it.")



st.subheader("Athlete entry")

# Athlete fields (no form, so dependent dropdowns update immediately)
c1, c2, c3, c4 = st.columns(4)
with c1:
    st.text_input("Last Name", key="last_name")
with c2:
    st.text_input("First Name", key="first_name")
with c3:
    st.text_input("Other Name (optional)", key="other_name")
with c4:
    gender = st.selectbox("Gender", ["M", "F"], key="gender")

last_name = st.session_state.get("last_name", "")
first_name = st.session_state.get("first_name", "")
other_name = st.session_state.get("other_name", "")

# Roster match selector (Google Sheet) — selecting a row fills fields (no splitting)
search_text = (" ".join([p for p in [first_name, other_name, last_name] if (p or "").strip()])).strip()

if st.session_state.get("use_roster") and (st.session_state.get("roster_sheet_url") or "").strip() and len(search_text) >= 2:
    try:
        roster_rows = load_roster(
            st.session_state.get("roster_sheet_url", ""),
            worksheet=((st.session_state.get("roster_worksheet") or "").strip() or None),
        )
    except Exception as e:
        st.error(f"Roster load error: {type(e).__name__}: {repr(e)}")
        roster_rows = []

    q = search_text.casefold()
    matches = []
    for r in roster_rows:
        ln = str(r.get("LAST_NAME", "") or "")
        on = str(r.get("OTHER_NAME", "") or "")
        team = str(r.get("TEAM_NAME", "") or "")
        if q in ln.casefold() or q in on.casefold() or q in (f"{on} {ln}".strip()).casefold() or q in team.casefold():
            matches.append(r)

    if matches:
        labels = []
        for r in matches[:25]:
            ln = str(r.get("LAST_NAME", "") or "").strip()
            on = str(r.get("OTHER_NAME", "") or "").strip()
            team = str(r.get("TEAM_NAME", "") or "").strip()
            uid = str(r.get("UNIQUE_ID", "") or "").strip()
            label = f"{on} {ln}".strip()
            if team:
                label += f" — {team}"
            if uid:
                label += f" ({uid})"
            labels.append(label)

        sel_key = "athlete_roster_match"
        options = ["(keep typed)"] + list(range(len(labels)))
        chosen = st.selectbox(
            "Select From List of Matches :",
            options=options,
            key=sel_key,
            format_func=lambda x: "(keep typed)" if x == "(keep typed)" else labels[int(x)],
        )

        if chosen != "(keep typed)":
            idx = int(chosen)
            r = matches[idx]

            ln = str(r.get("LAST_NAME", "") or "").strip()
            on = str(r.get("OTHER_NAME", "") or "").strip()
            nric = str(r.get("NRIC", "") or "").strip()
            dob = parse_dob(r.get("DOB"))
            nat_raw = str(r.get("NATIONALITY", "") or "").strip()
            uid = str(r.get("UNIQUE_ID", "") or "").strip()
            tname = str(r.get("TEAM_NAME", "") or "").strip()
            tcode_raw = str(r.get("TEAM_CODE", "") or "").strip()

            nat_val = _match_option_case_insensitive(nat_raw, ([''] + (COUNTRIES or [])))
            tcode_val = _match_option_case_insensitive(tcode_raw, TEAM_CODES)

            st.session_state["first_name__pending"] = on
            st.session_state["last_name__pending"] = ln
            st.session_state["other_name__pending"] = ""

            st.session_state["ic_last4__pending"] = last4_from_nric(nric)
            st.session_state["birth_date__pending"] = dob
            # Nationality: if not in list, store as override so it still appears in the dropdown
            nat_pick = _match_option_case_insensitive(nat_raw, (COUNTRIES or []))
            if nat_pick:
                st.session_state["nationality__pending"] = nat_pick
                st.session_state["nationality_override__pending"] = ""
            else:
                st.session_state["nationality__pending"] = nat_raw
                st.session_state["nationality_override__pending"] = nat_raw

            st.session_state["unique_id_override__pending"] = uid
            if tname:
                st.session_state["team_name_override__pending"] = tname

            st.session_state["athlete_roster_match__pending"] = "(keep typed)"
            st.rerun()


# Combined name (display)
typed_full_name = " ".join([p for p in [first_name, other_name, last_name] if (p or "").strip()]).strip()
db_name_override = (st.session_state.get("db_name_override", "") or "").strip()

# Live validation: name presence (either First+Last typed, or selected via matches -> db_name_override)
name_ok = (bool((first_name or '').strip()) and bool((last_name or '').strip())) or bool(db_name_override)
if not name_ok:
    st.warning("Enter First Name and Last Name, or select a match from the list.")


c4, c5, c6 = st.columns(3)
with c4:
    birth_date = st.date_input("Birth Date", value=None, key="birth_date")
    # Live validation: birth date
    birth_ok = birth_date is not None
    if not birth_ok:
        st.warning("Birth Date is required.")

with c5:
    ic_last4 = st.text_input("IC Number (last 4)", key="ic_last4")
    # Live validation: IC last-4 (3 digits + 1 letter)
    ic_last4_norm = normalize_ic_last4(ic_last4)
    ic_ok = True
    if not ic_last4_norm:
        ic_ok = False
        st.caption("IC format: 3 digits + 1 letter (e.g., 123A)")
    elif len(ic_last4_norm) < 4:
        ic_ok = False
        st.warning("IC last 4 is incomplete (e.g., 123A).")
        ic_ok = is_valid_ic_last4(ic_last4_norm)
        if not ic_ok:
            st.error("IC last 4 must be 3 digits followed by 1 letter (e.g., 123A).")

with c6:
    nationality_options = [""] + (COUNTRIES or [])
    _nat_extra = (st.session_state.get("nationality_override", "") or "").strip()
    if _nat_extra and _nat_extra not in nationality_options:
        nationality_options = ["", _nat_extra] + [x for x in nationality_options if x != ""]
    nationality = st.selectbox("Nationality", nationality_options, index=0, key="nationality")

c7, c8 = st.columns(2)
contact_number = c7.text_input("Contact Number", key="contact_number")

# Live validation: contact number
contact_ok = bool((contact_number or '').strip())
if not contact_ok:
    st.warning("Contact Number is required.")

email = c8.text_input("Email", key="email")

# Live validation: email
email_norm = normalize_email(email)
email_ok = True
if email_norm:
    email_ok = is_valid_email(email_norm)
    if not email_ok:
        st.error("Please enter a valid email address (e.g., name@example.com).")


c9, c10 = st.columns(2)
# Per-entry team selection; default from sidebar if present
if default_team_code and default_team_code in TEAM_CODES:
    if st.session_state.get("team_code") not in TEAM_CODES:
        st.session_state["team_code"] = default_team_code
team_code_options = list(TEAM_CODES)
_tc_extra = (st.session_state.get("team_code_override", "") or "").strip()
if _tc_extra and _tc_extra not in team_code_options:
    team_code_options = [_tc_extra] + team_code_options
team_code = c9.selectbox("Team Code", team_code_options, key="team_code")
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
    st.session_state["event_name"] = ""

event_name = c12.selectbox("Event", event_names if event_names else ["(no events)"], key="event_name")

# Live validation: event selection availability
event_ok = bool(event_opts) and event_name not in (None, '', '(no events)')
if not event_ok:
    st.warning("Please select an event (none available for this Gender + Division).")


season_best = st.text_input("Season Best (optional)", key="season_best")
emergency_contact_name = st.text_input("Emergency Contact Name", key="emergency_contact_name")
emergency_contact_number = st.text_input("Emergency Contact Number", key="emergency_contact_number")
coach_full_name = st.text_input("Coach Full Name", key="coach_full_name")
parq = st.selectbox("PAR-Q completed?", ["Y", "N"], key="parq")

ic_last4_norm = normalize_ic_last4(ic_last4)
email_norm = normalize_email(email)
unique_id_override = (st.session_state.get("unique_id_override", "") or "").strip()
unique_id = unique_id_override or (compute_unique_id(first_name, ic_last4_norm, birth_date) if birth_date else "")
st.caption(f"Unique ID (auto): **{unique_id or '—'}**")

waiver_ok = st.checkbox("I acknowledge the waiver (as per the original form).", value=False, key="waiver_ok")

# Gate Add entry button (live checks)
ready_to_add = bool(waiver_ok) and bool(email_ok) and bool(ic_ok) and bool(birth_ok) and bool(contact_ok) and bool(name_ok) and bool(event_ok)


# Add entry button
if st.button("Add entry", type="primary", disabled=not ready_to_add):
    missing = []
    for k, v in [
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
    elif not (((first_name or '').strip()) and ((last_name or '').strip())) and not ((db_name_override or '').strip()):
        st.error("Please enter First Name and Last Name, or select a name from matches.")
    elif not is_valid_email(email_norm):
        st.error("Please enter a valid email address (e.g., name@example.com).")
    elif not is_valid_ic_last4(ic_last4_norm):
        st.error("IC last 4 must be 3 digits followed by 1 letter (e.g., 123A).")
    elif not event_opts or not event_names or event_name == "(no events)":
        st.error("No events available for that Gender + Division combination.")
        event_code = dict(event_opts).get(event_name, "")
        st.session_state.entries.append({
            "name": combined_name,
            "last_name": (last_name or "").strip(),
            "first_name": (first_name or "").strip(),
            "other_name": (other_name or "").strip(),
            "gender": gender,
            "birth_date": birth_date,
            "ic_last4": ic_last4_norm,
            "unique_id": unique_id,
            "nationality": nationality,
            "contact_number": (contact_number or "").strip(),
            "email": email_norm,
            "team_code": team_code,
            "team_name": team_name_row,
            "charge_code": charge_code,
            "po_to_be_sent": po_to_be_sent,
            "event": event_name,
            "event_code": event_code,
            "event_division": int(event_division),
            "season_best": (season_best or "").strip(),
            "emergency_contact_name": (emergency_contact_name or "").strip(),
            "emergency_contact_number": (emergency_contact_number or "").strip(),
            "coach_full_name": (coach_full_name or "").strip(),
            "parq": parq,
        })
        st.success("Entry added.")

        # Auto-clear form fields for the next entry (use __pending to avoid Streamlit widget-state mutation errors)
        for _k, _v in {
            "last_name": "",
            "first_name": "",
            "other_name": "",
            "birth_date": None,
            "ic_last4": "",
            "contact_number": "",
            "email": "",
            "season_best": "",
            "emergency_contact_name": "",
            "emergency_contact_number": "",
            "coach_full_name": "",
            "waiver_ok": False,
            # Clear any database-driven combined-name override
            "db_name_override": "",
            # Reset match selector (if present)
            "athlete_name_match": "(keep typed)",
        }.items():
            st.session_state[f"{_k}__pending"] = _v
        st.rerun()

st.subheader("Current entries")
entries_df = pd.DataFrame(st.session_state.entries)

# Ensure these columns exist even for entries added before the latest schema updates
for _col, _default in {
    'charge_code': '',
    'po_to_be_sent': '',
    'event': '',
}.items():
    if _col not in entries_df.columns:
        entries_df[_col] = _default

# Display key columns first (others will still be available)
preferred_cols = [
    'team_name', 'team_code', 'charge_code', 'po_to_be_sent',
    'name', 'first_name', 'other_name', 'last_name',
    'gender', 'birth_date', 'ic_last4', 'unique_id',
    'event_division', 'event', 'event_code',
    'season_best', 'parq',
    'contact_number', 'email',
    'emergency_contact_name', 'emergency_contact_number',
    'coach_full_name', 'nationality'
]
cols = [c for c in preferred_cols if c in entries_df.columns] + [c for c in entries_df.columns if c not in preferred_cols]
entries_df = entries_df[cols]

st.dataframe(entries_df, use_container_width=True)

st.markdown('### Summary')
total_entries = len(entries_df)
st.write(f"Total entries: **{total_entries}**")
if not entries_df.empty and 'team_name' in entries_df.columns:
    counts = entries_df['team_name'].fillna('').replace('', '(Unknown)').value_counts().reset_index()
    counts.columns = ['School/Club', 'Entries']
    st.dataframe(counts, use_container_width=True)


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
        "po_to_be_sent": po_to_be_sent,
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
