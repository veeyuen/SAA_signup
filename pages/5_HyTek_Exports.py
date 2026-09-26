from __future__ import annotations

import pandas as pd
import streamlit as st

from google_sheets_reader import read_sheet_as_df
from payment_store import create_google_client
from signup.data_quality import latest_review_state, scan_data_quality
from signup.hytek_export import build_transactional_hytek_exports
from signup.multi_payment_transaction_store import TransactionSheetStore
from signup.pilot_config import PilotConfigRepository, require_configured_user


st.set_page_config(page_title="SAA Hy-Tek Exports", layout="wide")
st.title("Hy-Tek Exports")
st.caption(
    "Generate the established semicolon-delimited I and E files from confirmed "
    "competition transaction records."
)

CONFIG_SHEET_URL = str(st.secrets.get("CONFIG_SHEET_URL", "") or "").strip()
if not CONFIG_SHEET_URL:
    st.error("CONFIG_SHEET_URL is missing from Streamlit secrets.")
    st.stop()

TRANSACTION_SHEET_URL = str(
    st.secrets.get("TRANSACTION_SHEET_URL", CONFIG_SHEET_URL) or CONFIG_SHEET_URL
).strip()
OUTPUT_SHEET_URL = str(
    st.secrets.get("OUTPUT_SHEET_URL", TRANSACTION_SHEET_URL) or TRANSACTION_SHEET_URL
).strip()
OUTPUT_WORKSHEET = str(st.secrets.get("OUTPUT_WORKSHEET", "OUTPUT") or "OUTPUT").strip()

pilot_config = PilotConfigRepository(CONFIG_SHEET_URL)
user_email, user, organization = require_configured_user(
    repository=pilot_config,
    app_title="SAA Hy-Tek Exports",
    provider="auth0",
)
if user.role != "SAA_ADMIN":
    st.error("SAA_ADMIN access is required for this page.")
    st.stop()


@st.cache_resource(show_spinner=False)
def _export_store(schema_version: str):
    gc = create_google_client(dict(st.secrets["gcp_service_account"]))
    store = TransactionSheetStore(gc, TRANSACTION_SHEET_URL)
    # Phase 6A adds structured name fields for future registrations. Existing
    # rows are preserved; missing columns are appended by ensure_schema().
    store.ensure_schema()
    return store


try:
    store = _export_store("phase6a-hytek-ie-v1")
    orders = store.list_rows("ORDERS")
    registrations = store.list_rows("REGISTRATIONS")
    entries = store.list_rows("EVENT_ENTRIES")
    audit_rows = store.list_rows("AUDIT_LOG")
except Exception as exc:
    st.error(f"Could not load transaction data: {type(exc).__name__}: {exc}")
    st.stop()

# OUTPUT is transitional enrichment only. Export eligibility is determined from
# the transaction sheets, never from this legacy projection.
legacy_output = pd.DataFrame()
if OUTPUT_SHEET_URL:
    try:
        legacy_output = read_sheet_as_df(OUTPUT_SHEET_URL, worksheet=OUTPUT_WORKSHEET)
    except Exception as exc:
        st.warning(
            "The legacy OUTPUT sheet could not be read for historical first/last-name "
            f"enrichment ({type(exc).__name__}: {exc}). New Phase 6A registrations are unaffected."
        )

competition_ids = sorted(
    {
        str(row.get("COMPETITION_ID", "") or "").strip()
        for row in entries
        if str(row.get("COMPETITION_ID", "") or "").strip()
    }
)
if not competition_ids:
    st.info("No event-entry transaction rows are available for export.")
    st.stop()

competition_names: dict[str, str] = {}
try:
    for competition in pilot_config.competitions(include_closed=True):
        competition_names[competition.competition_id] = competition.competition_name
except Exception:
    pass


def _competition_label(competition_id: str) -> str:
    name = competition_names.get(competition_id, "")
    return f"{competition_id} — {name}" if name else competition_id


selected_competition_id = st.selectbox(
    "Competition",
    options=competition_ids,
    format_func=_competition_label,
)

bundle = build_transactional_hytek_exports(
    entries,
    registrations,
    competition_id=selected_competition_id,
    legacy_output_rows=legacy_output,
)

selected_entries = [
    row for row in entries
    if str(row.get("COMPETITION_ID", "") or "").strip() == selected_competition_id
]

c1, c2, c3 = st.columns(3)
c1.metric("Confirmed export rows", bundle.exported_count)
c2.metric("Excluded / non-exportable rows", bundle.excluded_count)
c3.metric("I / E records", f"{bundle.exported_count} / {bundle.exported_count}")

st.caption(
    "Export rule: EVENT_ENTRIES and its parent REGISTRATION must both be CONFIRMED "
    "and not deleted/withdrawn. Pending-payment, withdrawn and deleted rows are excluded."
)

# Phase 5D is the pre-export guardrail. Critical/error issues must be fixed or
# explicitly reviewed as false positives before a Hy-Tek file can be downloaded.
review_state = latest_review_state(audit_rows)
quality_issues = [
    issue
    for issue in scan_data_quality(
        orders=orders,
        registrations=registrations,
        entries=entries,
    )
    if issue.competition_id == selected_competition_id
]
blocking_issues = [
    issue
    for issue in quality_issues
    if issue.severity in {"CRITICAL", "ERROR"}
    and review_state.get(issue.issue_key, {}).get("STATUS") != "FALSE_POSITIVE"
]

if blocking_issues:
    st.error(
        "Hy-Tek download is blocked because this competition has unresolved "
        "critical/error data-quality issues. Resolve them in Admin Operations, or "
        "mark a verified false positive there, before exporting."
    )
    st.dataframe(
        pd.DataFrame(
            [
                {
                    "SEVERITY": issue.severity,
                    "ISSUE_TYPE": issue.issue_type,
                    "ATHLETE_NAME": issue.athlete_name,
                    "DOB": issue.dob,
                    "DETAILS": issue.details,
                }
                for issue in blocking_issues
            ]
        ),
        width="stretch",
        hide_index=True,
    )

warnings = [d for d in bundle.diagnostics if d.level in {"WARNING", "ERROR"}]
if warnings:
    with st.expander(f"Export diagnostics ({len(warnings)})", expanded=False):
        st.dataframe(
            pd.DataFrame(
                [
                    {
                        "LEVEL": d.level,
                        "CODE": d.code,
                        "ENTRY_ID": d.entry_id,
                        "MESSAGE": d.message,
                    }
                    for d in warnings
                ]
            ),
            width="stretch",
            hide_index=True,
        )

if bundle.exported_count == 0:
    st.warning("There are no confirmed active entries to export for this competition.")
else:
    disable_download = bool(blocking_issues)
    d1, d2 = st.columns(2)
    d1.download_button(
        "Download semicolon-delimited I file",
        data=bundle.i_text,
        file_name="current_entries_I_semicolon_delimited.txt",
        mime="text/plain",
        disabled=disable_download,
        use_container_width=True,
    )
    d2.download_button(
        "Download semicolon-delimited E file",
        data=bundle.e_text,
        file_name="current_entries_E_semicolon_delimited.txt",
        mime="text/plain",
        disabled=disable_download,
        use_container_width=True,
    )

    with st.expander("Preview export", expanded=False):
        p1, p2 = st.columns(2)
        p1.markdown("**I file**")
        p1.code(bundle.i_text, language=None)
        p2.markdown("**E file**")
        p2.code(bundle.e_text, language=None)

if selected_entries:
    st.caption(
        f"Source rows in EVENT_ENTRIES for {selected_competition_id}: {len(selected_entries)}. "
        "The export preserves transaction-sheet order."
    )
