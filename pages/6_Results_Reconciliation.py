from __future__ import annotations

import datetime as dt
import hashlib
import io
import json
import secrets
from pathlib import Path

import pandas as pd
import streamlit as st

from payment_store import create_google_client
from signup.multi_payment_transaction_store import TransactionSheetStore
from signup.pilot_config import PilotConfigRepository, require_configured_user
from signup.results_reconciliation import (
    ADMIN_EDITABLE_RESULT_FIELDS,
    admin_result_edit_changes,
    ResultsSchemaError,
    apply_admin_result_edits,
    manual_match_result,
    normalise_dob,
    reconcile_results_to_registrations,
    registration_resolution_candidates,
    set_admin_review_decision,
    unmatch_result,
    validate_canonical_results,
)


st.set_page_config(page_title="SAA Results Reconciliation", layout="wide")
st.title("Results Reconciliation")
st.caption(
    "Phase 6C.1 / 6C.2 — validate the canonical post-notebook results file and "
    "reconcile each result against confirmed competition registrations."
)
st.info(
    "Phase 6C2 admin resolution is enabled. Review/edit/match/unmatch/approve/remove actions are "
    "held in this reconciliation session only. Phase 6C3 adds confirmation + audit persistence; "
    "Phase 6C4 publishes approved results to the results database."
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


def _now_utc() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat()


def _new_audit_id() -> str:
    token = secrets.token_urlsafe(9).replace("-", "").replace("_", "").upper()
    return f"AUD-{token}"


def _audit_result_change(*, before: dict, after: dict, action: str, reason: str) -> None:
    # Reuse the application's existing immutable AUDIT_LOG worksheet.  Write the
    # audit record before mutating Streamlit reconciliation state: if Google
    # Sheets rejects the append, the admin change is not presented as complete.
    entity_id = str(before.get("RESULT_FINGERPRINT", "") or before.get("RESULT_ROW_NUMBER", "")).strip()
    store.append_audit_log({
        "AUDIT_ID": _new_audit_id(),
        "TIMESTAMP": _now_utc(),
        "USER_ID": user.user_id,
        "USER_EMAIL": user_email,
        "ACTION": action,
        "ENTITY_TYPE": "RESULT_RECONCILIATION",
        "ENTITY_ID": entity_id,
        "ORDER_ID": str(after.get("ORDER_ID", "") or before.get("ORDER_ID", "")).strip(),
        "BEFORE_JSON": json.dumps(before, ensure_ascii=False, sort_keys=True, default=str),
        "AFTER_JSON": json.dumps(after, ensure_ascii=False, sort_keys=True, default=str),
        "REASON": reason,
    })


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
    "REVIEW_STATUS",
    "ADMIN_ACTION",
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


st.subheader("SA Events Admin resolution workspace")
st.caption(
    "Select a result row to review. Confirmed field edits are written to the immutable "
    "AUDIT_LOG worksheet before the reconciliation-session change is applied."
)

admin_rows = report[
    (report["MATCH_STATUS"].isin(["REVIEW", "UNMATCHED", "MATCHED"]))
    & (report.get("REVIEW_STATUS", pd.Series("PENDING", index=report.index)) != "REMOVED")
]
if admin_rows.empty:
    st.info("There are no result rows available for admin resolution.")
else:
    row_indexes = list(admin_rows.index)

    def _admin_row_label(idx):
        row = report.loc[idx]
        return (
            f"Row {row.get('RESULT_ROW_NUMBER', idx)} · {row.get('NAME', '')} · "
            f"{row.get('EVENT', '')} · {row.get('RESULT', '')} · "
            f"{row.get('MATCH_STATUS', '')}/{row.get('REVIEW_STATUS', 'PENDING')}"
        )

    selected_idx = st.selectbox(
        "Result to review",
        options=row_indexes,
        format_func=_admin_row_label,
        key="phase6c_admin_row_index",
    )
    selected_row = report.loc[selected_idx].to_dict()

    left, right = st.columns(2)
    with left:
        st.markdown("**Incoming / current result**")
        st.dataframe(
            pd.DataFrame([{
                "Name": selected_row.get("NAME", ""),
                "DOB": selected_row.get("DOB", ""),
                "Unique ID": selected_row.get("UNIQUE_ID", ""),
                "Team": selected_row.get("TEAM", ""),
                "Competition": selected_row.get("COMPETITION", ""),
                "Event": selected_row.get("EVENT", ""),
                "Division": selected_row.get("DIVISION", ""),
                "Result": selected_row.get("RESULT", ""),
            }]),
            hide_index=True,
            width="stretch",
        )
    with right:
        st.markdown("**Current registration link**")
        st.dataframe(
            pd.DataFrame([{
                "Registration": selected_row.get("REGISTRATION_ID", ""),
                "Entry": selected_row.get("ENTRY_ID", ""),
                "Athlete": selected_row.get("REGISTRATION_ATHLETE_NAME", ""),
                "DOB": selected_row.get("REGISTRATION_DOB", ""),
                "Event": selected_row.get("REGISTERED_EVENT", ""),
                "Division": selected_row.get("REGISTERED_DIVISION", ""),
            }]),
            hide_index=True,
            width="stretch",
        )

    with st.expander("Edit result fields", expanded=True):
        with st.form(f"phase6c_edit_{selected_idx}"):
            e1, e2 = st.columns(2)
            edits = {}
            for pos, field in enumerate(ADMIN_EDITABLE_RESULT_FIELDS):
                target = e1 if pos % 2 == 0 else e2
                edits[field] = target.text_input(
                    field.replace("_", " ").title(),
                    value=str(selected_row.get(field, "") or ""),
                    key=f"phase6c_edit_{selected_idx}_{field}",
                )
            save_edits = st.form_submit_button("Stage edits", use_container_width=True)
        pending_key = f"phase6c_pending_edit_{selected_idx}"
        if save_edits:
            try:
                changes = admin_result_edit_changes(selected_row, edits)
            except ResultsSchemaError as exc:
                st.error(f"Cannot stage changes: {exc}")
                st.session_state.pop(pending_key, None)
            else:
                if not changes:
                    st.info("No changes to stage.")
                    st.session_state.pop(pending_key, None)
                else:
                    st.session_state[pending_key] = {
                        "edits": {field: edits[field] for field in ADMIN_EDITABLE_RESULT_FIELDS},
                        "changes": changes,
                    }

        pending_edit = st.session_state.get(pending_key)
        if pending_edit:
            st.markdown("#### Confirm changes")
            st.caption("The reconciliation row has not been changed yet.")
            st.dataframe(pd.DataFrame(pending_edit["changes"]), hide_index=True, width="stretch")
            cancel_col, confirm_col = st.columns(2)
            if cancel_col.button("Cancel", key=f"phase6c_cancel_edit_{selected_idx}", use_container_width=True):
                # Discard both the staged proposal and the edit-widget values.
                # On rerun each widget is rebuilt from the authoritative reconciliation row,
                # so Cancel visibly restores the current values rather than leaving the
                # abandoned proposal in Streamlit session state.
                st.session_state.pop(pending_key, None)
                for field in ADMIN_EDITABLE_RESULT_FIELDS:
                    st.session_state.pop(f"phase6c_edit_{selected_idx}_{field}", None)
                st.rerun()
            if confirm_col.button(
                "Confirm changes",
                type="primary",
                key=f"phase6c_confirm_edit_{selected_idx}",
                use_container_width=True,
            ):
                updated = apply_admin_result_edits(selected_row, pending_edit["edits"])
                updated["ADMIN_ACTION"] = "EDIT"
                try:
                    _audit_result_change(
                        before=selected_row,
                        after=updated,
                        action="RESULT_RECONCILIATION_EDIT",
                        reason="SA Events Admin confirmed staged result reconciliation edits.",
                    )
                except Exception as exc:
                    st.error(
                        "The change was not applied because the audit record could not be written: "
                        f"{type(exc).__name__}: {exc}"
                    )
                else:
                    report.loc[selected_idx, list(updated.keys())] = list(updated.values())
                    st.session_state["phase6c_reconciliation_rows"] = report
                    st.session_state.pop(pending_key, None)
                    for field in ADMIN_EDITABLE_RESULT_FIELDS:
                        st.session_state.pop(f"phase6c_edit_{selected_idx}_{field}", None)
                    st.success("Changes confirmed and written to the immutable AUDIT_LOG worksheet.")
                    st.rerun()

    candidates = registration_resolution_candidates(
        selected_registrations,
        selected_entries,
        competition_id=selected_competition_id,
    )
    st.markdown("**Manual registration match**")
    if candidates:
        candidate_indexes = list(range(len(candidates)))

        def _candidate_label(pos):
            candidate = candidates[pos]
            team = " ".join(x for x in (candidate.get("TEAM_CODE"), candidate.get("TEAM_NAME")) if x)
            return (
                f"{candidate.get('ATHLETE_NAME', '')} · DOB {candidate.get('DOB', '')} · "
                f"{candidate.get('EVENT', '')} · {candidate.get('DIVISION', '')}"
                + (f" · Team {team}" if team else "")
                + f" · {candidate.get('ENTRY_ID', '')}"
            )

        candidate_pos = st.selectbox(
            "Registration/event entry",
            candidate_indexes,
            format_func=_candidate_label,
            key=f"phase6c_candidate_{selected_idx}",
        )
        match_col, unmatch_col = st.columns(2)
        pending_match_key = f"phase6c_pending_match_{selected_idx}"

        if match_col.button(
            "Match result",
            type="primary",
            key=f"phase6c_match_{selected_idx}",
        ):
            candidate = dict(candidates[candidate_pos])
            st.session_state[pending_match_key] = candidate

        pending_match = st.session_state.get(pending_match_key)
        if pending_match:
            st.markdown("#### Confirm manual match")
            st.caption(
                "The reconciliation row has not been changed yet. "
                "Review the incoming result and proposed registration link."
            )

            incoming_dob = normalise_dob(selected_row.get("DOB", ""))
            candidate_dob = normalise_dob(pending_match.get("DOB", ""))

            comparison = pd.DataFrame([
                {
                    "Field": "Athlete name",
                    "Incoming result": selected_row.get("NAME", ""),
                    "Selected registration": pending_match.get("ATHLETE_NAME", ""),
                },
                {
                    "Field": "DOB",
                    "Incoming result": incoming_dob or selected_row.get("DOB", ""),
                    "Selected registration": candidate_dob or pending_match.get("DOB", ""),
                },
                {
                    "Field": "Unique / Athlete ID",
                    "Incoming result": selected_row.get("UNIQUE_ID", ""),
                    "Selected registration": pending_match.get("ATHLETE_ID", ""),
                },
                {
                    "Field": "Event",
                    "Incoming result": selected_row.get("EVENT", ""),
                    "Selected registration": pending_match.get("EVENT", ""),
                },
                {
                    "Field": "Division",
                    "Incoming result": selected_row.get("DIVISION", ""),
                    "Selected registration": pending_match.get("DIVISION", ""),
                },
                {
                    "Field": "Registration ID",
                    "Incoming result": selected_row.get("REGISTRATION_ID", ""),
                    "Selected registration": pending_match.get("REGISTRATION_ID", ""),
                },
                {
                    "Field": "Entry ID",
                    "Incoming result": selected_row.get("ENTRY_ID", ""),
                    "Selected registration": pending_match.get("ENTRY_ID", ""),
                },
            ])
            st.dataframe(comparison, hide_index=True, width="stretch")

            identity_mismatches = []
            incoming_name = str(selected_row.get("NAME", "") or "").strip().casefold()
            candidate_name = str(pending_match.get("ATHLETE_NAME", "") or "").strip().casefold()
            if incoming_name and candidate_name and incoming_name != candidate_name:
                identity_mismatches.append("name")

            if incoming_dob and candidate_dob and incoming_dob != candidate_dob:
                identity_mismatches.append("DOB")

            incoming_uid = str(selected_row.get("UNIQUE_ID", "") or "").strip().casefold()
            candidate_uid = str(pending_match.get("ATHLETE_ID", "") or "").strip().casefold()
            if incoming_uid and candidate_uid and incoming_uid != candidate_uid:
                identity_mismatches.append("Unique ID / Athlete ID")

            if identity_mismatches:
                st.warning(
                    "Identity discrepancy: "
                    + ", ".join(identity_mismatches)
                    + " differ between the incoming result and selected registration. "
                    "Confirm only if SA Events intentionally wants this manual linkage."
                )

            cancel_match_col, confirm_match_col = st.columns(2)

            if cancel_match_col.button(
                "Cancel match",
                key=f"phase6c_cancel_match_{selected_idx}",
                use_container_width=True,
            ):
                st.session_state.pop(pending_match_key, None)
                st.rerun()

            if confirm_match_col.button(
                "Confirm match",
                type="primary",
                key=f"phase6c_confirm_match_{selected_idx}",
                use_container_width=True,
            ):
                updated = manual_match_result(selected_row, pending_match)
                try:
                    _audit_result_change(
                        before=selected_row,
                        after=updated,
                        action="RESULT_RECONCILIATION_MATCH",
                        reason="SA Events Admin confirmed a manual result-to-registration match.",
                    )
                except Exception as exc:
                    st.error(
                        "The match was not applied because the audit record could not be written: "
                        f"{type(exc).__name__}: {exc}"
                    )
                else:
                    report.loc[selected_idx, list(updated.keys())] = list(updated.values())
                    st.session_state["phase6c_reconciliation_rows"] = report
                    st.session_state.pop(pending_match_key, None)
                    st.success(
                        "Manual match confirmed and written to the immutable AUDIT_LOG worksheet."
                    )
                    st.rerun()

        pending_unmatch_key = f"phase6c_pending_unmatch_{selected_idx}"

        if unmatch_col.button(
            "Unmatch result",
            key=f"phase6c_unmatch_{selected_idx}",
        ):
            if not str(selected_row.get("REGISTRATION_ID", "") or "").strip():
                st.info("This result is not currently linked to a registration.")
                st.session_state.pop(pending_unmatch_key, None)
            else:
                st.session_state[pending_unmatch_key] = True

        if st.session_state.get(pending_unmatch_key):
            st.markdown("#### Confirm unmatch")
            st.caption(
                "The reconciliation row has not been changed yet. Confirming will remove "
                "the registration/event linkage but retain the result."
            )

            st.dataframe(
                pd.DataFrame([{
                    "Registration": selected_row.get("REGISTRATION_ID", ""),
                    "Entry": selected_row.get("ENTRY_ID", ""),
                    "Athlete": selected_row.get("REGISTRATION_ATHLETE_NAME", ""),
                    "DOB": selected_row.get("REGISTRATION_DOB", ""),
                    "Event": selected_row.get("REGISTERED_EVENT", ""),
                    "Division": selected_row.get("REGISTERED_DIVISION", ""),
                }]),
                hide_index=True,
                width="stretch",
            )

            cancel_unmatch_col, confirm_unmatch_col = st.columns(2)

            if cancel_unmatch_col.button(
                "Cancel unmatch",
                key=f"phase6c_cancel_unmatch_{selected_idx}",
                use_container_width=True,
            ):
                st.session_state.pop(pending_unmatch_key, None)
                st.rerun()

            if confirm_unmatch_col.button(
                "Confirm unmatch",
                type="primary",
                key=f"phase6c_confirm_unmatch_{selected_idx}",
                use_container_width=True,
            ):
                updated = unmatch_result(selected_row)

                try:
                    _audit_result_change(
                        before=selected_row,
                        after=updated,
                        action="RESULT_RECONCILIATION_UNMATCH",
                        reason=(
                            "SA Events Admin confirmed removal of the "
                            "result-to-registration link."
                        ),
                    )
                except Exception as exc:
                    st.error(
                        "The unmatch was not applied because the audit record "
                        "could not be written: "
                        f"{type(exc).__name__}: {exc}"
                    )
                else:
                    report.loc[
                        selected_idx,
                        list(updated.keys()),
                    ] = list(updated.values())

                    st.session_state["phase6c_reconciliation_rows"] = report
                    st.session_state.pop(pending_unmatch_key, None)

                    st.success(
                        "Registration link removed and written to the "
                        "immutable AUDIT_LOG worksheet."
                    )
                    st.rerun()
    else:
        st.warning("No active confirmed registration/event entries are available for manual matching.")

    st.markdown("**Admin decision**")
    st.caption(
        "Approve means SA Events has confirmed this is the correct result. An UNMATCHED result may be "
        "approved as a legitimate competition result without a registration. Remove duplicate excludes "
        "the row from further processing while retaining it for the later audit trail."
    )
    approve_col, remove_col = st.columns(2)
    pending_approve_key = f"phase6c_pending_approve_{selected_idx}"

    def _stage_approval():
        st.session_state[pending_approve_key] = True

    approve_col.button(
        "Approve result",
        type="primary",
        key=f"phase6c_approve_{selected_idx}",
        on_click=_stage_approval,
    )

    if st.session_state.get(pending_approve_key):
        st.markdown("#### Confirm approval")
        st.caption(
            "The reconciliation row has not been changed yet. Confirming records that "
            "SA Events has reviewed and accepted this result. Database publishing remains "
            "disabled until Phase 6C4."
        )
        st.dataframe(
            pd.DataFrame([{
                "Name": selected_row.get("NAME", ""),
                "DOB": selected_row.get("DOB", ""),
                "Team": selected_row.get("TEAM", ""),
                "Competition": selected_row.get("COMPETITION", ""),
                "Event": selected_row.get("EVENT", ""),
                "Division": selected_row.get("DIVISION", ""),
                "Result": selected_row.get("RESULT", ""),
                "Match status": selected_row.get("MATCH_STATUS", ""),
                "Registration": selected_row.get("REGISTRATION_ID", ""),
                "Entry": selected_row.get("ENTRY_ID", ""),
            }]),
            hide_index=True,
            width="stretch",
        )

        cancel_approve_col, confirm_approve_col = st.columns(2)
        if cancel_approve_col.button(
            "Cancel approval",
            key=f"phase6c_cancel_approve_{selected_idx}",
            use_container_width=True,
        ):
            st.session_state.pop(pending_approve_key, None)
            st.rerun()

        if confirm_approve_col.button(
            "Confirm approval",
            type="primary",
            key=f"phase6c_confirm_approve_{selected_idx}",
            use_container_width=True,
        ):
            updated = set_admin_review_decision(selected_row, "APPROVED")
            try:
                _audit_result_change(
                    before=selected_row,
                    after=updated,
                    action="RESULT_RECONCILIATION_APPROVE",
                    reason="SA Events Admin confirmed approval of the reconciled result.",
                )
            except Exception as exc:
                st.error(
                    "The approval was not applied because the audit record could not be written: "
                    f"{type(exc).__name__}: {exc}"
                )
            else:
                report.loc[selected_idx, list(updated.keys())] = list(updated.values())
                st.session_state["phase6c_reconciliation_rows"] = report
                st.session_state.pop(pending_approve_key, None)
                st.success(
                    "Result approved and written to the immutable AUDIT_LOG worksheet. "
                    "Database publishing is not enabled until Phase 6C4."
                )
                st.rerun()

    pending_remove_key = f"phase6c_pending_remove_{selected_idx}"

    def _stage_duplicate_removal():
        st.session_state[pending_remove_key] = True

    remove_col.button(
        "Remove duplicate",
        key=f"phase6c_remove_{selected_idx}",
        on_click=_stage_duplicate_removal,
    )

    if st.session_state.get(pending_remove_key):
        st.markdown("#### Confirm duplicate removal")
        st.caption(
            "The reconciliation row has not been changed yet. Confirming marks this row "
            "REMOVED so it is excluded from further processing, while retaining the "
            "imported result and its audit history."
        )
        st.dataframe(
            pd.DataFrame([{
                "Name": selected_row.get("NAME", ""),
                "DOB": selected_row.get("DOB", ""),
                "Team": selected_row.get("TEAM", ""),
                "Competition": selected_row.get("COMPETITION", ""),
                "Event": selected_row.get("EVENT", ""),
                "Division": selected_row.get("DIVISION", ""),
                "Result": selected_row.get("RESULT", ""),
                "Match status": selected_row.get("MATCH_STATUS", ""),
                "Review status": selected_row.get("REVIEW_STATUS", ""),
                "Registration": selected_row.get("REGISTRATION_ID", ""),
                "Entry": selected_row.get("ENTRY_ID", ""),
            }]),
            hide_index=True,
            width="stretch",
        )
        st.warning(
            "Use this action only when this imported row is a duplicate result. "
            "This is a soft administrative removal; the underlying imported evidence is retained."
        )

        cancel_remove_col, confirm_remove_col = st.columns(2)

        if cancel_remove_col.button(
            "Cancel removal",
            key=f"phase6c_cancel_remove_{selected_idx}",
            use_container_width=True,
        ):
            st.session_state.pop(pending_remove_key, None)
            st.rerun()

        if confirm_remove_col.button(
            "Confirm removal",
            type="primary",
            key=f"phase6c_confirm_remove_{selected_idx}",
            use_container_width=True,
        ):
            updated = set_admin_review_decision(selected_row, "REMOVED")
            try:
                _audit_result_change(
                    before=selected_row,
                    after=updated,
                    action="RESULT_RECONCILIATION_REMOVE_DUPLICATE",
                    reason="SA Events Admin confirmed this imported result row is a duplicate.",
                )
            except Exception as exc:
                st.error(
                    "The duplicate removal was not applied because the audit record "
                    "could not be written: "
                    f"{type(exc).__name__}: {exc}"
                )
            else:
                report.loc[selected_idx, list(updated.keys())] = list(updated.values())
                st.session_state["phase6c_reconciliation_rows"] = report
                st.session_state.pop(pending_remove_key, None)
                st.success(
                    "Duplicate result marked REMOVED and written to the immutable AUDIT_LOG worksheet."
                )
                st.rerun()


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
