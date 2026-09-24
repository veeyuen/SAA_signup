# Streamlit Athlete Signup App (Dynamic dependent dropdowns)
# Generated on: 2026-03-01T11:27:08
#
# Key fix:
# - Removed st.form wrapper around dependent widgets so Event dropdown updates immediately
#   when Gender/Division changes.
#
# This app does NOT load the original Excel workbook.
# TEST
import re
import traceback as tb

import datetime as dt
import pandas as pd
import streamlit as st

from google_sheets_reader import read_sheet_as_df
from google_sheets_roster import load_roster, parse_dob, last4_from_nric
from google_sheets_writer import sync_entries_to_sheet
from reference_lists import (
    COUNTRIES,
    ENTRY_HEADERS,
    TEAM_CODES,
    get_events,
    get_team_name,
)

from signup.config import (
    APP_TITLE,
    DEFAULT_APP_TITLE,
    DIVISIONS,
    DIVISIONS_60M,
    SPRINT_60M_APP_TITLE,
    SPRINT_60M_ONLY_MODE,
)
from signup.email import (
    build_confirmation_email_html,
    send_confirmation_email_smtp,
)
from signup.events import (
    active_divisions as _active_divisions,
    allowed_events,
    coerce_division_key_for_options as _coerce_division_key_for_options,
    default_division_key as _default_division_key,
    division_display_label as _division_display_label,
    division_value_for_storage as _division_value_for_storage,
    event_sort_key as _event_sort_key,
)
from signup.export import (
    build_semicolon_export_from_output_sheet,
    export_entries_to_excel,
    sheet_df_to_entries as _sheet_df_to_entries,
)
from signup.formatting import (
    code_to_gender_display,
    compute_unique_id,
    gender_to_code,
    normalize_header as _normalize_header,
    safe_date_max,
)
from signup.session import apply_pending_text_updates, init_sheet_session
from signup.pilot_config import (
    PilotConfigError,
    PilotConfigRepository,
    require_configured_user,
)
from signup.validation import (
    is_valid_email,
    is_valid_ic_last4,
    match_option_case_insensitive as _match_option_case_insensitive,
    normalize_email,
    normalize_ic_last4,
)

import json
import secrets
from decimal import Decimal

from stripe_checkout import create_registration_checkout
from payment_store import (
    create_google_client,
    get_pending_worksheet,
    save_pending_registration,
)

APP_VARIANT = "public_entry_only"

LOGIN_REQUIRED_FOR_THIS_ROLLOUT = True

# ---------------- UI ----------------
st.set_page_config(page_title=APP_TITLE, layout="wide")

CONFIG_SHEET_URL = str(st.secrets.get("CONFIG_SHEET_URL", "") or "").strip()
if not CONFIG_SHEET_URL:
    st.error("CONFIG_SHEET_URL is missing from Streamlit secrets.")
    st.stop()

pilot_config = PilotConfigRepository(CONFIG_SHEET_URL)

if not LOGIN_REQUIRED_FOR_THIS_ROLLOUT:
    st.error("The Google Sheets pilot configuration requires login to be enabled.")
    st.stop()

current_user_email, current_user, current_organization = require_configured_user(
    repository=pilot_config,
    app_title=APP_TITLE,
    provider="auth0",
)
st.title(APP_TITLE)

def show_stripe_return_status():
    """Show a safe return screen after Stripe redirects back to Streamlit.

    The redirect itself is not treated as proof of payment. The Stripe webhook
    remains responsible for saving the confirmed registration and sending the
    acknowledgement email.
    """
    payment_result = str(st.query_params.get("payment_result", "") or "").strip().lower()
    if not payment_result:
        return

    pending_checkout = st.session_state.get("pending_checkout", {}) or {}
    registration_id = str(pending_checkout.get("registration_id", "") or "").strip()

    if payment_result == "cancelled":
        st.warning("Payment was cancelled. Your registration has not been confirmed.")
        if registration_id:
            st.caption(f"Registration reference: {registration_id}")

        if st.button("Return to registration form", type="primary"):
            st.session_state.pop("pending_checkout", None)
            st.query_params.clear()
            st.rerun()

        st.stop()

    if payment_result == "success":
        session_id = str(st.query_params.get("session_id", "") or "").strip()

        st.success("Your payment has been submitted to Stripe.")
        st.info(
            "Your registration will be saved and the acknowledgement email will be "
            "sent only after Stripe's webhook confirms that the payment succeeded."
        )

        if registration_id:
            st.write(f"Registration reference: `{registration_id}`")
        if session_id:
            st.caption(f"Stripe Checkout session: {session_id}")

        st.stop()


show_stripe_return_status()


apply_pending_text_updates()
init_sheet_session(load_roster_fn=load_roster)

# ---------------- Pilot master/configuration ----------------
try:
    available_competitions = pilot_config.competitions(include_closed=False)
except PilotConfigError as exc:
    st.error(f"Configuration error: {exc}")
    st.stop()

if not available_competitions:
    st.info("There are currently no competitions open for registration.")
    st.stop()

_competition_by_id = {
    comp.competition_id: comp for comp in available_competitions
}
_competition_ids = list(_competition_by_id.keys())

selected_competition_id = st.selectbox(
    "Competition",
    options=_competition_ids,
    format_func=lambda cid: _competition_by_id[cid].competition_name,
    key="selected_competition_id",
)
selected_competition = _competition_by_id[selected_competition_id]
registration_period = pilot_config.registration_period(selected_competition)

try:
    selected_entry_fee = pilot_config.fee_for(
        competition_id=selected_competition_id,
        organization_type=current_organization.organization_type,
        registration_period=registration_period,
    )
except PilotConfigError as exc:
    st.error(f"Configuration error: {exc}")
    st.stop()

configured_payment_status = (
    "NO_COST" if selected_entry_fee == Decimal("0") else "REQUIRED"
)

_summary_col1, _summary_col2, _summary_col3, _summary_col4 = st.columns(4)
_summary_col1.metric("Organisation", current_organization.organization_name)
_summary_col2.metric("Account type", current_organization.organization_type.title())
_summary_col3.metric("Registration period", registration_period.title())
_summary_col4.metric("Fee per event", f"S${selected_entry_fee:.2f}")

st.caption(
    f"Team code: {current_organization.team_code} · "
    f"Payment status: {'No cost' if configured_payment_status == 'NO_COST' else 'Required'}"
)

# Preload existing entries from OUTPUT Google Sheet on app start (only if empty)
st.session_state.setdefault("entries", [])
st.session_state.setdefault("full_name", "")
st.session_state.setdefault("name_passport", "")

if not st.session_state.entries:
    _preload_url = (st.session_state.get("output_sheet_url") or "").strip()
    _preload_ws = (st.session_state.get("output_worksheet") or "").strip() or None
    if _preload_url:
        try:
            _df_pre = read_sheet_as_df(_preload_url, worksheet=_preload_ws)
            st.session_state.entries = _sheet_df_to_entries(_df_pre)
        except Exception as e:
            st.session_state["preload_error"] = f"{type(e).__name__}: {repr(e)}"

if st.session_state.get("preload_error"):
    st.warning(f"Could not preload existing entries from output sheet. ({st.session_state['preload_error']})")



if "entries" not in st.session_state:
    st.session_state.entries = []

with st.sidebar:
    st.header("Team / Billing")

    default_team_code = current_organization.team_code
    default_team_name = current_organization.organization_name
    team_name_header = current_organization.organization_name

    st.text_input("Team Name", value=team_name_header, disabled=True)
    st.text_input("Team Code", value=default_team_code, disabled=True)

    st.session_state.setdefault("billing_email", current_user_email)
    billing_name = st.text_input("Billing contact name", value="", key="billing_name")
    billing_email = st.text_input("Billing email", key="billing_email")
    charge_code = st.text_input("Charge code (optional)", value="", key="charge_code")
    po_to_be_sent = st.radio("P/O to be sent", options=["No", "Yes"], index=0, horizontal=True, key="po_to_be_sent")
    if billing_email and not is_valid_email(billing_email):
        st.warning("Billing email looks invalid. Please double-check it.")

    st.divider()
    st.caption(f"Competition: {selected_competition.competition_name}")
    st.caption(f"Registration period: {registration_period.title()}")
    st.caption(f"Entry fee: S${selected_entry_fee:.2f}")

# Defensive: ensure billing fields are bound even if sidebar UI is modified
po_to_be_sent = st.session_state.get("po_to_be_sent", "No")
charge_code = st.session_state.get("charge_code", "")
st.subheader("Athlete Entry Form")

# Athlete fields (no form, so dependent dropdowns update immediately)
c1, c2, c3, c4 = st.columns(4)
with c1:
    st.text_input("Last Name", key="last_name")
with c2:
    st.text_input("First Name", key="first_name")
with c3:
    st.text_input("Other Name (optional)", key="other_name")
with c4:
    gender = st.selectbox("Gender", ["", "Male", "Female"], index=0, key="gender")

# Name as per NRIC/Passport is a separate required field.
# It is intentionally NOT auto-filled from First Name / Last Name / Full Name.
passport_name = st.text_input("Name as per NRIC/Passport", key="name_passport")
passport_ok = bool((passport_name or "").strip())
if not passport_ok:
    st.warning("Name as per NRIC/Passport is required.")

# Live validation: gender (mandatory)
gender_ok = gender in ('Male','Female')
if not gender_ok:
    st.warning("Gender is required (select Male or Female).")


last_name = st.session_state.get("last_name", "")
first_name = st.session_state.get("first_name", "")
other_name = st.session_state.get("other_name", "")

# If user edits any name fields after selecting from roster, clear roster-derived FULL_NAME and UNIQUE_ID
current_name_sig = "|".join([
    (st.session_state.get("first_name", "") or "").strip(),
    (st.session_state.get("other_name", "") or "").strip(),
    (st.session_state.get("last_name", "") or "").strip(),
])
prev_sig = (st.session_state.get("full_name_signature", "") or "").strip()
if prev_sig and current_name_sig != prev_sig:
    # User typed a new name; clear roster-derived fields so they don't persist
    st.session_state["full_name__pending"] = ""
    st.session_state["unique_id_override__pending"] = ""
    st.session_state["db_name_override__pending"] = ""
    st.session_state["full_name_signature__pending"] = ""
    st.rerun()


# Roster match selector (Google Sheet) — selecting a row fills fields (no splitting)
search_text = (" ".join([p for p in [first_name, other_name, last_name] if (p or "").strip()])).strip()

_roster_enabled = bool(st.session_state.get("use_roster"))
_roster_url = (st.session_state.get("roster_sheet_url") or "").strip()

# Small inline hints so you can see why the dropdown may not appear
if not _roster_enabled:
    st.caption("Roster search is OFF (check configuration).")
elif not _roster_url:
    st.caption("Roster sheet URL is empty (set ROSTER_SHEET_URL in secrets).")
elif len(search_text) < 2:
    st.caption("Type at least 2 characters in First/Other/Last name to search the roster.")

matches = []
roster_rows = []
if _roster_enabled and _roster_url and len(search_text) >= 2:
    try:
        roster_rows = st.session_state.get("roster_cache_rows")
        # If cache exists but is empty, try one reload per session (handles first-run load glitches)
        if isinstance(roster_rows, list) and (len(roster_rows) == 0) and (not st.session_state.get("roster_cache_reloaded_once")):
            st.session_state["roster_cache_reloaded_once"] = True
            roster_rows = load_roster(
                st.session_state.get("roster_sheet_url", ""),
                worksheet=((st.session_state.get("roster_worksheet") or "").strip() or None),
            )
            st.session_state["roster_cache_rows"] = roster_rows
        if not isinstance(roster_rows, list):
            roster_rows = load_roster(
                st.session_state.get("roster_sheet_url", ""),
                worksheet=((st.session_state.get("roster_worksheet") or "").strip() or None),
            )
            st.session_state["roster_cache_rows"] = roster_rows
    except Exception as e:
        st.error(f"Roster load error: {type(e).__name__}: {repr(e)}")
        roster_rows = []

    st.caption(f"Roster loaded: {len(roster_rows)} rows")
    q = search_text.casefold()
    for r in roster_rows:
        full_name = str(r.get("FULL_NAME", "") or "")
        fn = str(r.get("FIRST_NAME", "") or "")
        ln = str(r.get("LAST_NAME", "") or "")
        on = str(r.get("OTHER_NAME", "") or "")
        team = str(r.get("TEAM_NAME", "") or "")
        uid = str(r.get("UNIQUE_ID", "") or "")
        nric = str(r.get("NRIC", "") or "")
        # Match on tokens across ALL name fields (FIRST/LAST/OTHER/FULL), plus team/uid/nric(last4)
        full_name = str(r.get("FULL_NAME", "") or "")
        name_hay = " ".join([full_name, fn, on, ln]).casefold()
        tokens = [t.casefold() for t in search_text.split() if t.strip()]
        extra_hay = " ".join([team, uid, last4_from_nric(nric)]).casefold()
        # Scored OR-matching: show suggestions even if only part of the name is typed
        score = 0
        if tokens:
            score += sum(1 for t in tokens if t in name_hay)
        if q and q in name_hay:
            score += 2  # boost full-query name hits
        if q and q in extra_hay:
            score += 1
        if score > 0:
            matches.append((score, r))

    if matches:
        matches = [r for _, r in sorted(matches, key=lambda x: x[0], reverse=True)]
    st.caption(f"Matches found: {len(matches)}")

    if roster_rows and not matches:
        st.info(f"No roster matches for: '{search_text}'. You can refine the search (try first name, last name, team, UID, or NRIC last-4).")

    # Optional browse mode (helps confirm data is loading)
    browse_mode = st.toggle("Browse roster (show first 25)", value=False, key="browse_roster_mode")
    if browse_mode and roster_rows:
        matches = roster_rows[:25]

    if matches:
        labels = []
        for r in matches[:25]:
            fn = str(r.get("FIRST_NAME", "") or "").strip()
            fn_raw = str(r.get("FIRST_NAME", "") or "").strip()
            ln = str(r.get("LAST_NAME", "") or "").strip()
            on = str(r.get("OTHER_NAME", "") or "").strip()
            nric = str(r.get("NRIC", "") or "").strip()
            dob_val = parse_dob(r.get("DOB"))
            # Privacy: only show birth year in roster match/browse labels, not full DOB.
            if hasattr(dob_val, "strftime") and dob_val:
                dob_str = dob_val.strftime("%Y")
            else:
                _dob_raw = str(r.get("DOB", "") or "").strip()
                _year_match = re.search(r"(?:19|20)\d{2}", _dob_raw)
                dob_str = _year_match.group(0) if _year_match else ""
            gen = str(r.get("GENDER", "") or "").strip()
            nat = str(r.get("NATIONALITY", "") or "").strip()
            uid = str(r.get("UNIQUE_ID", "") or "").strip()
            tcode = str(r.get("TEAM_CODE", "") or "").strip()
            team = str(r.get("TEAM_NAME", "") or "").strip()

            # Privacy: roster dropdown labels show first name only and redact last name.
            # Underlying roster row still contains full details for autofill after selection.
            first_for_label = fn or str(r.get("FIRST_NAME", "") or "").strip()
            last_for_label = ln or str(r.get("LAST_NAME", "") or "").strip()
            label_parts = []
            if first_for_label:
                label_parts.append(first_for_label)
            if last_for_label:
                label_parts.append("*")
            label = " ".join(label_parts).strip() or "(unnamed roster entry)"
            parts = []
            n4 = last4_from_nric(nric)
            if n4:
                parts.append(f"NRIC(last4): {n4}")
            if dob_str:
                parts.append(f"{dob_str}")
            if gen:
                parts.append(f"{gen}")
            if nat:
                parts.append(f"{nat}")
            team_piece = " ".join([p for p in [tcode, team] if p]).strip()
            if team_piece:
                parts.append(f"{team_piece}")
            if parts:
                label = label + " | " + " | ".join(parts)
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

            full_name_sel = str(r.get("FULL_NAME", "") or "").strip()
            if not full_name_sel:
                fn_tmp = str(r.get("FIRST_NAME", "") or "").strip()
                on_tmp = str(r.get("OTHER_NAME", "") or "").strip()
                ln_tmp = str(r.get("LAST_NAME", "") or "").strip()
                full_name_sel = " ".join([p for p in [fn_tmp, on_tmp, ln_tmp] if p]).strip()

            fn = str(r.get("FIRST_NAME", "") or "").strip()
            fn_raw = str(r.get("FIRST_NAME", "") or "").strip()
            ln = str(r.get("LAST_NAME", "") or "").strip()
            on = str(r.get("OTHER_NAME", "") or "").strip()
            nric = str(r.get("NRIC", "") or "").strip()
            dob = parse_dob(r.get("DOB"))
            gender_raw = str(r.get("GENDER", "") or "").strip().upper()
            nat_raw = str(r.get("NATIONALITY", "") or "").strip()
            sgpr_raw = str(r.get("SINGAPORE_PR", "") or r.get("SG_PR", "") or r.get("PR_STATUS", "") or "").strip()
            uid = str(r.get("UNIQUE_ID", "") or "").strip()
            tname = str(r.get("TEAM_NAME", "") or "").strip()
            tcode_raw = str(r.get("TEAM_CODE", "") or "").strip()

            # Populate name fields
            st.session_state["first_name__pending"] = fn_raw or on
            st.session_state["last_name__pending"] = ln
            st.session_state["other_name__pending"] = on
            st.session_state["full_name__pending"] = full_name_sel
            roster_name_passport = str(
                r.get("NAME_PASSPORT", "")
                or r.get("NAME_AS_PER_NRIC_PASSPORT", "")
                or r.get("NAME AS PER NRIC/PASSPORT", "")
                or ""
            ).strip()
            if roster_name_passport:
                st.session_state["name_passport__pending"] = roster_name_passport
            st.session_state["full_name_signature__pending"] = "|".join([
                (st.session_state.get("first_name__pending", "") or "").strip(),
                (st.session_state.get("other_name__pending", "") or "").strip(),
                (st.session_state.get("last_name__pending", "") or "").strip(),
            ])

            # Populate other fields
            st.session_state["ic_last4__pending"] = last4_from_nric(nric)
            st.session_state["birth_date__pending"] = dob
            # Gender from roster (may be blank; still mandatory to submit)
            if gender_raw in ("M","F","MALE","FEMALE"):
                st.session_state["gender__pending"] = ("Male" if gender_raw.startswith("M") else "Female")
            else:
                st.session_state["gender__pending"] = ""

            # Nationality: if not in list, store as override so it still appears in the dropdown
            nat_pick = _match_option_case_insensitive(nat_raw, (COUNTRIES or []))
            if nat_pick:
                st.session_state["nationality__pending"] = nat_pick
                st.session_state["nationality_override__pending"] = ""
            else:
                st.session_state["nationality__pending"] = nat_raw
                st.session_state["nationality_override__pending"] = nat_raw

            _nat_cf = nat_raw.casefold()
            _sgpr_cf = sgpr_raw.casefold()
            roster_is_sg_pr = (
                _sgpr_cf in ("yes", "y", "true", "1", "pr", "singapore pr", "sg pr")
                or _nat_cf in ("singapore pr", "sg pr")
            )
            st.session_state["singapore_pr__pending"] = bool(roster_is_sg_pr)
            if roster_is_sg_pr and not nat_pick:
                # Keep nationality as an IOC/WA code while using the checkbox for PR status.
                st.session_state["nationality__pending"] = "SGP"
                st.session_state["nationality_override__pending"] = ""

            # Unique ID override from roster
            st.session_state["unique_id_override__pending"] = uid
            # Optional roster fields, if available
            roster_email = str(r.get("EMAIL", "") or r.get("Email", "") or "").strip()
            roster_contact = str(
                r.get("CONTACT_NUMBER", "")
                or r.get("CONTACT", "")
                or r.get("MOBILE", "")
                or r.get("PHONE", "")
                or ""
            ).strip()
            if roster_email:
                st.session_state["email__pending"] = roster_email
            if roster_contact:
                st.session_state["contact_number__pending"] = roster_contact


            # Team fields:
            # The form now uses Team Name as the selected widget and shows Team Code automatically.
            # Therefore we must set BOTH the code-related state and the team_name_selected widget key.
            tcode_pick = _match_option_case_insensitive(tcode_raw, TEAM_CODES)
            resolved_team_code = tcode_pick or tcode_raw
            resolved_team_name = tname or (get_team_name(resolved_team_code) if resolved_team_code else "")

            if tcode_pick:
                st.session_state["team_code__pending"] = tcode_pick
                st.session_state["team_code_override__pending"] = ""
            else:
                st.session_state["team_code__pending"] = tcode_raw
                st.session_state["team_code_override__pending"] = tcode_raw

            if resolved_team_name:
                st.session_state["team_name_selected__pending"] = resolved_team_name
                st.session_state["team_name_override__pending"] = resolved_team_name
            else:
                st.session_state["team_name_selected__pending"] = ""
                st.session_state["team_name_override__pending"] = ""

            st.session_state["athlete_roster_match__pending"] = "(keep typed)"
            st.rerun()


# Combined name (display)
typed_full_name = " ".join([p for p in [first_name, other_name, last_name] if (p or "").strip()]).strip()
db_name_override = (st.session_state.get("db_name_override", "") or "").strip()

# Full Name (auto) — editable
full_name_display = (st.session_state.get("full_name", "") or "").strip()
if (not full_name_display) and typed_full_name:
    # Pre-fill from typed First/Other/Last (user can edit)
    st.session_state["full_name"] = typed_full_name
    full_name_display = typed_full_name
st.text_input("Full Name (auto)", key="full_name")

# Live validation: name presence
selected_from_roster = bool((st.session_state.get("unique_id_override", "") or "").strip() or (db_name_override or "").strip())
first_last_ok = selected_from_roster or (bool((first_name or "").strip()) and bool((last_name or "").strip()))
name_ok = passport_ok and first_last_ok
if not first_last_ok:
    st.warning("First Name and Last Name are required unless you selected the athlete from the roster.")


c4, c5, c6 = st.columns(3)
with c4:
    # Birth Date input (allow rendering even if a preloaded value is later than today)
    _birth_cur = st.session_state.get("birth_date")
    _birth_max = dt.date.today()
    if isinstance(_birth_cur, dt.date) and _birth_cur > _birth_max:
        _birth_max = _birth_cur
    birth_date = st.date_input(
        "Birth Date",
        value=_birth_cur if isinstance(_birth_cur, dt.date) else None,
        min_value=dt.date(1900, 1, 1),
        max_value=_birth_max,
        key="birth_date",
        format="DD-MM-YYYY",
    )
    # Live validation: birth date
    birth_ok = (st.session_state.get("birth_date") is not None) and (st.session_state.get("birth_date") <= dt.date.today())
    if st.session_state.get("birth_date") and st.session_state.get("birth_date") > dt.date.today():
        st.warning("Birth Date cannot be in the future.")
    elif not birth_ok:
        st.warning("Birth Date is required.")

with c6:
    nationality_options = [""] + (COUNTRIES or [])
    _nat_extra = (st.session_state.get("nationality_override", "") or "").strip()
    if _nat_extra and _nat_extra not in nationality_options:
        nationality_options = ["", _nat_extra] + [x for x in nationality_options if x != ""]
    nationality = st.selectbox("Nationality", nationality_options, index=0, key="nationality")
    is_singapore = (str(nationality or '').strip().upper() in ('SGP','SIN','SG','SINGAPORE'))
    # Singapore PR status (separate from nationality code)
    singapore_pr = st.checkbox('Singapore PR?', key='singapore_pr')



# Unique ID (display) — placed under Birth Date / IC row
unique_id_override = (st.session_state.get("unique_id_override", "") or "").strip()
_ic_for_uid = normalize_ic_last4(st.session_state.get("ic_last4", "") or "")
unique_id = unique_id_override or (compute_unique_id(first_name, _ic_for_uid, birth_date) if birth_date else "")
uid_from_roster = unique_id_override
if uid_from_roster:
    st.text_input("Unique ID (from roster)", value=uid_from_roster, disabled=True)
else:
    st.text_input("Unique ID (auto)", value=(unique_id or ""), disabled=True)


with c5:
    ic_last4 = st.text_input("IC Number (last 4)", key="ic_last4")
    # Live validation: IC last-4 (3 digits + 1 letter)
    ic_last4_norm = normalize_ic_last4(ic_last4)  # ALWAYS define
    # IC last-4 is required if Singapore PR is ticked, or if Singapore athlete has no UNIQUE_ID
    unique_id_present_for_ic = bool((st.session_state.get("unique_id_override", "") or "").strip() or (st.session_state.get("unique_id", "") or "").strip())
    ic_required = bool(singapore_pr) or (bool(is_singapore) and (not unique_id_present_for_ic))
    ic_ok = True
    if ic_required and not ic_last4_norm:
        ic_ok = False
        st.warning("IC format: 3 digits + 1 letter (e.g., 123A) — required when Singapore PR is ticked, or when a Singapore athlete has no UNIQUE_ID.")
    elif (not ic_last4_norm):
        # Not required and not provided
        ic_ok = True
    elif len(ic_last4_norm) < 4:
        ic_ok = False
        st.warning("IC last 4 is incomplete (e.g., 123A).")
    else:
        ic_ok = is_valid_ic_last4(ic_last4_norm)
        if not ic_ok:
            st.error("IC last 4 must be 3 digits followed by 1 letter (e.g., 123A).")

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

email_present = bool(email_norm)
if not email_present:
    st.warning("Email is required.")
    if not email_ok:
        st.error("Please enter a valid email address (e.g., name@example.com).")


c9, c10 = st.columns(2)

team_name_row = current_organization.organization_name
team_code = current_organization.team_code
st.session_state["team_code"] = team_code
c9.text_input("Team Name", value=team_name_row, disabled=True)
c10.text_input("Team Code", value=team_code, disabled=True)

# Divisions and events are driven by the competition configuration worksheets.
c11, c12 = st.columns(2)
try:
    _division_rows = pilot_config.division_rows(
        competition_id=selected_competition_id,
        gender=gender,
        birth_date=birth_date if birth_ok else None,
        competition_start_at=selected_competition.competition_start_at,
    )
except PilotConfigError as exc:
    st.error(f"Configuration error: {exc}")
    st.stop()

_active_division_keys = [row["code"] for row in _division_rows]
_division_labels = {
    row["code"]: row["label"] for row in _division_rows
}

if _active_division_keys:
    if st.session_state.get("event_division") not in _active_division_keys:
        st.session_state["event_division"] = _active_division_keys[0]

event_division = c11.selectbox(
    "Event Division",
    options=_active_division_keys,
    format_func=lambda code: (
        f"{code} - {_division_labels.get(code, code)}"
        if _division_labels.get(code, code) != code
        else code
    ),
    key="event_division",
    disabled=(not _active_division_keys),
)

try:
    event_opts_raw = pilot_config.event_options(
        competition_id=selected_competition_id,
        gender=gender,
        division_code=event_division if _active_division_keys else "",
    )
except PilotConfigError as exc:
    st.error(f"Configuration error: {exc}")
    st.stop()

event_opts = sorted(event_opts_raw, key=lambda _x: _event_sort_key(_x[0]))
event_names = [name for name, _code in event_opts]

prev_selected = st.session_state.get("events_selected", [])
if not isinstance(prev_selected, list):
    prev_selected = []
prev_selected_valid = [e for e in prev_selected if e in event_names]
if prev_selected_valid != prev_selected:
    st.session_state["events_selected"] = prev_selected_valid

selected_events = c12.multiselect(
    "Select event(s)",
    options=event_names,
    default=prev_selected_valid,
    key="events_selected",
    disabled=(not event_names),
)

# Live validation: DOB first determines age-eligible divisions; gender + division
# then determine the configured event list.
event_ok = bool(event_opts) and len(selected_events) > 0

if birth_ok and not _active_division_keys:
    athlete_age = pilot_config.age_on_date(
        birth_date,
        selected_competition.competition_start_at,
    )
    competition_date = (
        selected_competition.competition_start_at.date()
        if selected_competition.competition_start_at is not None
        else None
    )
    age_text = f"age {athlete_age}" if athlete_age is not None else "this age"
    date_text = (
        f" on {competition_date.strftime('%d-%m-%Y')}"
        if competition_date is not None
        else ""
    )
    st.warning(
        "No eligible divisions with configured events are available for "
        f"{age_text}{date_text}."
    )
elif not event_ok:
    st.warning("Please select at least one event for the selected division.")


season_best = st.text_input("Season Best", key="season_best")
season_best_ok = bool((season_best or "").strip())
if not season_best_ok:
    st.warning("Season Best is required.")
emergency_contact_name = st.text_input("Emergency Contact Name", key="emergency_contact_name")
emergency_contact_number = st.text_input("Emergency Contact Number", key="emergency_contact_number")
coach_full_name = st.text_input("Coach Full Name", key="coach_full_name")
parq = st.selectbox("PAR-Q completed?", ["Y", "N"], key="parq")

ic_last4_norm = normalize_ic_last4(ic_last4)
email_norm = normalize_email(email)

waiver_ok = st.checkbox("I acknowledge the waiver (as per the original form).", value=False, key="waiver_ok")

# Gate Add entry button (live checks)
ready_to_add = bool(waiver_ok) and bool(email_present) and bool(email_ok) and bool(ic_ok) and bool(birth_ok) and bool(contact_ok) and bool(name_ok) and bool(gender_ok) and bool(event_ok) and bool(season_best_ok)



# Existing pending Checkout session, if one has already been created.
_pending_checkout = st.session_state.get("pending_checkout", {}) or {}
if _pending_checkout:
    st.success("Your registration is pending payment.")
    st.write(f"Amount payable: **SGD {_pending_checkout.get('amount', '')}**")

    _pending_registration_id = str(
        _pending_checkout.get("registration_id", "") or ""
    ).strip()
    if _pending_registration_id:
        st.caption(f"Registration reference: {_pending_registration_id}")

    st.link_button(
        "Pay by Card or PayNow",
        str(_pending_checkout.get("payment_url", "") or ""),
        type="primary",
    )

    st.warning(
        "Your registration has not yet been saved to the confirmed-entry sheet. "
        "It will be saved only after Stripe verifies successful payment."
    )

    if st.button("Cancel this payment request and edit the form"):
        st.session_state.pop("pending_checkout", None)
        st.rerun()

    st.stop()


# Phase 1 leaves school invoicing and no-cost final submission for the
# transactional/cart milestone. Configuration, organisation mapping and pricing
# can still be fully tested for those account types.
_checkout_supported = (
    current_organization.organization_type in {"AFFILIATE", "ASSOCIATE"}
    and selected_entry_fee > Decimal("0")
)

if current_organization.organization_type == "SCHOOL":
    st.info(
        "School account detected. Post-event MOE invoicing will be enabled in "
        "the next transactional/cart phase; no Stripe payment is started here."
    )
elif selected_entry_fee == Decimal("0"):
    st.info(
        "This competition is configured as No cost. Final no-cost submission "
        "will be enabled in the next transactional/cart phase."
    )

# Create a Stripe Checkout session and pending registration.
if st.button(
    "Proceed to payment",
    type="primary",
    disabled=(not ready_to_add) or (not _checkout_supported),
):
    missing = []
    _uid_present = bool((unique_id or "").strip())
    _is_sgp_local = (
        str(nationality or "").strip().upper()
        in ("SGP", "SIN", "SG", "SINGAPORE")
    )
    _pr_local = bool(st.session_state.get("singapore_pr", False))
    ic_required = bool(_pr_local) or (
        bool(_is_sgp_local) and (not _uid_present)
    )

    missing_checks = [
        (
            "Name as per NRIC/Passport",
            (st.session_state.get("name_passport", "") or "").strip(),
        ),
        ("Birth Date", birth_date),
        ("Email", email),
        ("Contact Number", contact_number),
        ("Season Best", season_best),
    ]

    # IC is required only if Singapore PR is ticked, or a Singapore athlete
    # does not already have a UNIQUE_ID.
    if ic_required:
        missing_checks.insert(1, ("IC last 4", ic_last4))

    for field_name, field_value in missing_checks:
        if not field_value:
            missing.append(field_name)

    if not waiver_ok:
        st.error("Please tick the waiver acknowledgement.")
    elif missing:
        st.error("Missing: " + ", ".join(missing))
    elif not gender_ok:
        st.error("Please select Gender (Male or Female).")
    elif not (
        (st.session_state.get("name_passport", "") or "").strip()
    ):
        st.error("Name as per NRIC/Passport is required.")
    elif (
        not (
            (st.session_state.get("unique_id_override", "") or "").strip()
            or (db_name_override or "").strip()
        )
        and not (
            bool((first_name or "").strip())
            and bool((last_name or "").strip())
        )
    ):
        st.error(
            "First Name and Last Name are required unless you selected "
            "the athlete from the roster."
        )
    elif not is_valid_email(email_norm):
        st.error(
            "Please enter a valid email address "
            "(e.g., name@example.com)."
        )
    elif (
        str(nationality or "").strip().upper()
        in ("SGP", "SIN", "SG", "SINGAPORE")
        and (not _uid_present)
        and (not is_valid_ic_last4(ic_last4_norm))
    ):
        st.error(
            "IC last 4 must be 3 digits followed by 1 letter "
            "(e.g., 123A)."
        )
    elif (
        _uid_present
        and ic_last4_norm
        and (not is_valid_ic_last4(ic_last4_norm))
    ):
        st.error(
            "IC last 4 must be 3 digits followed by 1 letter "
            "(e.g., 123A)."
        )
    elif not event_opts or not event_names or not selected_events:
        st.error(
            "Please select at least one event for that "
            "Gender + Division combination."
        )
    elif not (season_best or "").strip():
        st.error("Season Best is required.")
    else:
        registration_id = (
            "SAA-"
            + secrets.token_urlsafe(9)
            .replace("-", "")
            .replace("_", "")
            .upper()
        )

        full_name_for_payment = (
            (st.session_state.get("full_name", "") or "").strip()
            or (db_name_override or typed_full_name)
        )
        unique_id_for_payment = (
            (st.session_state.get("unique_id_override", "") or "").strip()
            or unique_id
        )

        # Build rows but do NOT add them to st.session_state.entries and do NOT
        # sync them to the confirmed output sheet yet.
        entry_rows = []
        added_events = []

        for selected_event in selected_events:
            event_code = dict(event_opts).get(selected_event, "")

            entry_rows.append(
                {
                    "registration_id": registration_id,
                    "payment_provider": "stripe",
                    "payment_status": configured_payment_status,
                    "competition_id": selected_competition_id,
                    "competition_name": selected_competition.competition_name,
                    "registration_period": registration_period,
                    "organization_id": current_organization.organization_id,
                    "organization_type": current_organization.organization_type,
                    "entry_fee": f"{selected_entry_fee:.2f}",
                    "name": (db_name_override or typed_full_name),
                    "full_name": full_name_for_payment,
                    "name_passport": (
                        st.session_state.get("name_passport", "") or ""
                    ).strip(),
                    "last_name": (last_name or "").strip(),
                    "first_name": (first_name or "").strip(),
                    "other_name": (other_name or "").strip(),
                    "gender": gender,
                    "birth_date": (
                        birth_date.isoformat() if birth_date else ""
                    ),
                    "ic_last4": ic_last4_norm,
                    "unique_id": unique_id_for_payment,
                    "nationality": nationality,
                    "singapore_pr": singapore_pr,
                    "contact_number": (contact_number or "").strip(),
                    "email": email_norm,
                    "team_code": team_code,
                    "team_name": team_name_row,
                    "charge_code": charge_code,
                    "po_to_be_sent": po_to_be_sent,
                    "event_division": event_division,
                    "season_best": (season_best or "").strip(),
                    "emergency_contact_name": (
                        emergency_contact_name or ""
                    ).strip(),
                    "emergency_contact_number": (
                        emergency_contact_number or ""
                    ).strip(),
                    "coach_full_name": (
                        coach_full_name or ""
                    ).strip(),
                    "parq": parq,
                    "event": selected_event,
                    "event_code": event_code,
                }
            )
            added_events.append(selected_event)

        # Pricing now comes from COMPETITION_FEES rather than a global
        # STRIPE_PRICE_PER_EVENT secret.
        price_per_event = selected_entry_fee
        total_amount = price_per_event * Decimal(len(entry_rows))
        amount_str = f"{total_amount:.2f}"

        currency = str(
            st.secrets.get("STRIPE_CURRENCY", "sgd") or "sgd"
        ).strip().lower()

        if currency != "sgd":
            st.error(
                "Stripe PayNow requires STRIPE_CURRENCY to be set to 'sgd'."
            )
            st.stop()

        stripe_secret_key = str(
            st.secrets.get("STRIPE_SECRET_KEY", "") or ""
        ).strip()
        public_app_url = str(
            st.secrets.get(
                "PUBLIC_APP_URL",
                "https://saapublicaccess.streamlit.app",
            )
            or ""
        ).strip()

        pending_sheet_url = str(
            st.secrets.get("PENDING_PAYMENT_SHEET_URL", "") or ""
        ).strip()
        pending_worksheet_name = str(
            st.secrets.get(
                "PENDING_PAYMENT_WORKSHEET",
                "PendingPayments",
            )
            or "PendingPayments"
        ).strip()

        if not stripe_secret_key:
            st.error("Stripe is not configured: STRIPE_SECRET_KEY is missing.")
            st.stop()

        if not pending_sheet_url:
            st.error(
                "Pending-payment storage is not configured: "
                "PENDING_PAYMENT_SHEET_URL is missing."
            )
            st.stop()

        try:
            checkout = create_registration_checkout(
                secret_key=stripe_secret_key,
                registration_id=registration_id,
                amount=amount_str,
                currency=currency,
                customer_email=email_norm,
                description=(
                    f"{selected_competition.competition_name}: "
                    f"{', '.join(added_events)}"
                ),
                public_app_url=public_app_url,
            )

            google_client = create_google_client(
                dict(st.secrets["gcp_service_account"])
            )

            pending_worksheet = get_pending_worksheet(
                google_client,
                pending_sheet_url,
                pending_worksheet_name,
            )

            save_pending_registration(
                worksheet=pending_worksheet,
                registration_id=registration_id,
                login_email=current_user_email,
                athlete_email=email_norm,
                full_name=full_name_for_payment,
                team_name=team_name_row,
                events=added_events,
                entry_rows=entry_rows,
                amount=amount_str,
                currency=currency,
                stripe_session_id=checkout["session_id"],
            )

        except Exception as exc:
            st.error(
                "Unable to start payment: "
                f"{type(exc).__name__}: {exc}"
            )
            st.stop()

        # Persist the payment link across Streamlit reruns so that a user does
        # not accidentally create multiple Checkout Sessions.
        st.session_state["pending_checkout"] = {
            "registration_id": registration_id,
            "session_id": checkout["session_id"],
            "payment_url": checkout["payment_url"],
            "amount": amount_str,
            "currency": currency,
        }

        st.rerun()


# -------- Public entry-only mode --------
# Existing/current entries, download buttons, and edit/delete controls are intentionally hidden.
# Registrations are written to the confirmed output sheet only by the Stripe webhook
# after successful payment.
st.caption(
    "Entry-only mode: existing entries and edit controls are hidden. "
    "Payment confirmation is handled by Stripe."
)
