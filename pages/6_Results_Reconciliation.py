from __future__ import annotations

import hashlib
import io
from pathlib import Path

import pandas as pd
import streamlit as st

from payment_store import create_google_client
from signup.multi_payment_transaction_store import TransactionSheetStore
from signup.pilot_config import PilotConfigRepository, require_configured_user
from signup.results_reconciliation import (
    ResultsSchemaError,
    reconcile_results_to_registrations,
    validate_canonical_results,
)


st.set_page_config(page_title="SAA Results Reconciliation", layout="wide")
st.title("Results Reconciliation")
st.caption(
    "Phase 6C.1 / 6C.2 — validate the canonical post-notebook results file and "
    "reconcile each result against confirmed competition registrations."
)
st.info(
    "Preview only: this phase does not write result matches back to Google Sheets or BigQuery. "
    "Persistence and admin resolution actions are added later in Phase 6C."
)

CONFIG_SHEET_URL = str(st.secrets.get("CONFIG_SHEET_URL", "") or "").strip()
if not CONFIG_SHEET_URL:
    st.error("CONFIG_SHEET_URL is missing from Streamlit secrets.")
    st.stop()

TRANSACTION_SHEET_URL = str(
    st.secrets.get("TRANSACTION_SHEET_URL", CONFIG_SHEET_URL) or CONFIG_SHEET_URL
).strip()

pilot_config = PilotConfigRepository(CONFIG_SHEET_URL)
user_email, user, organization = require_configured_user(
    repository=pilot_config,
    app_title="SAA Results Reconciliation",
    provider="auth0",
)
if user.role != "SAA_ADMIN":
    st.error("SAA_ADMIN access is required for this page.")
    st.stop()


@st.cache_resource(show_spinner=False)
def _transaction_store(schema_version: str):
    gc = create_google_client(dict(st.secrets["gcp_service_account"]))
    store = TransactionSheetStore(gc, TRANSACTION_SHEET_URL)
    store.ensure_schema()
    return store


@st.cache_data(show_spinner=False)
def _parse_results_upload(file_name: str, payload: bytes) -> pd.DataFrame:
    suffix = Path(file_name).suffix.lower()
    buffer = io.BytesIO(payload)
    if suffix == ".csv":
        # Preserve identifiers and source text exactly; matching functions perform
        # their own controlled normalization.
        return pd.read_csv(buffer, dtype=str, keep_default_na=False)
    if suffix == ".xlsx":
        return pd.read_excel(buffer, dtype=str).fillna("")
    raise ResultsSchemaError("Upload a CSV or XLSX file produced by the results notebook.")


try:
    store = _transaction_store("phase6c-results-reconciliation-v1")
    registrations = store.list_rows("REGISTRATIONS")
    entries = store.list_rows("EVENT_ENTRIES")
except Exception as exc:
    st.error(f"Could not load registration transactions: {type(exc).__name__}: {exc}")
    st.stop()

competition_ids = sorted(
    {
        str(row.get("COMPETITION_ID", "") or "").strip()
        for row in entries
        if str(row.get("COMPETITION_ID", "") or "").strip()
    }
)
if not competition_ids:
    st.info("No event-entry transaction rows are available for reconciliation.")
    st.stop()

competition_names: dict[str, str] = {}
try:
    for competition in pilot_config.competitions(include_closed=True):
        competition_names[competition.competition_id] = competition.competition_name
except Exception as exc:
    st.warning(f"Competition names could not be loaded: {type(exc).__name__}: {exc}")


def _competition_label(competition_id: str) -> str:
    name = competition_names.get(competition_id, "")
    return f"{competition_id} — {name}" if name else competition_id


selected_competition_id = st.selectbox(
    "Competition",
    options=competition_ids,
    format_func=_competition_label,
)
selected_competition_name = competition_names.get(selected_competition_id, "").strip()
if not selected_competition_name:
    st.error(
        "The selected competition has no COMPETITION_NAME available from configuration. "
        "Results cannot be reconciled safely because the result file identifies the competition by name."
    )
    st.stop()

selected_registrations = [
    row
    for row in registrations
    if str(row.get("COMPETITION_ID", "") or "").strip() == selected_competition_id
]
selected_entries = [
    row
    for row in entries
    if str(row.get("COMPETITION_ID", "") or "").strip() == selected_competition_id
]

c1, c2 = st.columns(2)
c1.metric("Registration rows", len(selected_registrations))
c2.metric("Event-entry rows", len(selected_entries))

uploaded = st.file_uploader(
    "Upload canonical results file",
    type=["csv", "xlsx"],
    help=(
        "Use the post-notebook BigQuery-shaped output, not the raw DBeaver/Meet Manager extract. "
        "The current canonical schema is the existing 40 columns plus boolean INDOOR."
    ),
)
if uploaded is None:
    st.stop()

upload_payload = uploaded.getvalue()
try:
    raw_results = _parse_results_upload(uploaded.name, upload_payload)
except Exception as exc:
    st.error(f"Could not read results file: {type(exc).__name__}: {exc}")
    st.stop()

st.caption(f"Uploaded rows: {len(raw_results):,} · columns: {len(raw_results.columns)}")

indoor_default = None
if "INDOOR" not in raw_results.columns:
    st.warning(
        "Legacy 40-column notebook output detected: INDOOR is missing. "
        "The system will not guess whether the competition is indoor or outdoor."
    )
    legacy_choice = st.selectbox(
        "Explicit INDOOR value for every row in this uploaded file",
        options=["Choose...", "Outdoor — INDOOR=False", "Indoor — INDOOR=True"],
    )
    if legacy_choice == "Choose...":
        st.stop()
    indoor_default = legacy_choice.startswith("Indoor")

try:
    canonical_results, schema_validation = validate_canonical_results(
        raw_results,
        indoor_default=indoor_default,
    )
except ResultsSchemaError as exc:
    st.error(f"Results schema validation failed: {exc}")
    st.stop()

if schema_validation.used_legacy_indoor_default:
    applied = "True" if schema_validation.indoor_value_if_defaulted else "False"
    st.success(
        f"Legacy schema upgraded in memory for this reconciliation only: INDOOR={applied} "
        f"was explicitly applied to {schema_validation.row_count:,} rows."
    )
else:
    st.success(
        f"Canonical results schema validated: {schema_validation.required_column_count} required columns, "
        f"{schema_validation.row_count:,} rows."
    )

upload_context_key = hashlib.sha256(
    upload_payload + f"|INDOOR_DEFAULT={indoor_default!r}".encode("utf-8")
).hexdigest()

if schema_validation.extra_columns:
    st.info(
        "Extra columns were preserved and do not block reconciliation: "
        + ", ".join(schema_validation.extra_columns)
    )

competition_values = sorted(
    {
        str(value or "").strip()
        for value in canonical_results["COMPETITION"].tolist()
        if str(value or "").strip()
    }
)
if competition_values:
    st.caption("Result-file competition value(s): " + " | ".join(competition_values))
st.caption(f"Selected registration competition: {selected_competition_name}")

if st.button("Run strict reconciliation", type="primary", use_container_width=True):
    try:
        bundle = reconcile_results_to_registrations(
            canonical_results,
            selected_registrations,
            selected_entries,
            competition_id=selected_competition_id,
            competition_name=selected_competition_name,
        )
    except Exception as exc:
        st.error(f"Reconciliation failed: {type(exc).__name__}: {exc}")
        st.stop()

    st.session_state["phase6c_reconciliation_rows"] = bundle.rows
    st.session_state["phase6c_reconciliation_competition_id"] = selected_competition_id
    st.session_state["phase6c_reconciliation_file_name"] = uploaded.name
    st.session_state["phase6c_reconciliation_upload_context"] = upload_context_key

report = st.session_state.get("phase6c_reconciliation_rows")
report_competition_id = st.session_state.get("phase6c_reconciliation_competition_id")
report_file_name = st.session_state.get("phase6c_reconciliation_file_name")
report_upload_context = st.session_state.get("phase6c_reconciliation_upload_context")
if (
    not isinstance(report, pd.DataFrame)
    or report_competition_id != selected_competition_id
    or report_upload_context != upload_context_key
):
    st.stop()

matched_count = int((report["MATCH_STATUS"] == "MATCHED").sum())
review_count = int((report["MATCH_STATUS"] == "REVIEW").sum())
unmatched_count = int((report["MATCH_STATUS"] == "UNMATCHED").sum())

m1, m2, m3, m4 = st.columns(4)
m1.metric("Result rows", len(report))
m2.metric("MATCHED", matched_count)
m3.metric("REVIEW", review_count)
m4.metric("UNMATCHED", unmatched_count)

st.caption(
    "Automatic MATCHED requires Unique ID + DOB + name to match one active confirmed registration, "
    "followed by one active confirmed matching event entry. Name spelling/order/punctuation variations "
    "are not fuzzy-matched and remain for SA Events Admin review."
)

summary = (
    report.groupby(["MATCH_STATUS", "MATCH_REASON"], dropna=False)
    .size()
    .reset_index(name="ROWS")
    .sort_values(["MATCH_STATUS", "ROWS", "MATCH_REASON"], ascending=[True, False, True])
)
st.subheader("Reconciliation summary")
st.dataframe(summary, width="stretch", hide_index=True)

review_columns = [
    "RESULT_ROW_NUMBER",
    "MATCH_STATUS",
    "MATCH_REASON",
    "MATCH_DETAILS",
    "UNIQUE_ID",
    "DOB",
    "NAME",
    "EVENT",
    "DIVISION",
    "RESULT",
    "REGISTRATION_ID",
    "ENTRY_ID",
    "REGISTRATION_ATHLETE_ID",
    "REGISTRATION_ATHLETE_NAME",
    "REGISTRATION_DOB",
    "REGISTERED_EVENT",
    "REGISTERED_DIVISION",
]
review_columns = [column for column in review_columns if column in report.columns]

st.subheader("Rows requiring attention")
attention = report[report["MATCH_STATUS"].isin(["REVIEW", "UNMATCHED"])][review_columns]
if attention.empty:
    st.success("Every uploaded result row reconciled automatically.")
else:
    st.dataframe(attention, width="stretch", hide_index=True)

with st.expander("Matched rows", expanded=False):
    matched = report[report["MATCH_STATUS"] == "MATCHED"][review_columns]
    st.dataframe(matched, width="stretch", hide_index=True)

base_name = Path(str(report_file_name or "results")).stem
report_csv = report.to_csv(index=False).encode("utf-8")
st.download_button(
    "Download reconciliation report CSV",
    data=report_csv,
    file_name=f"{base_name}_reconciliation.csv",
    mime="text/csv",
    use_container_width=True,
)
