import streamlit as st

def apply_pending_text_updates():
    pending = [k for k in list(st.session_state.keys()) if k.endswith("__pending")]
    for pending_key in pending:
        base_key = pending_key[:-9]
        st.session_state[base_key] = st.session_state.get(pending_key, "")
        try:
            del st.session_state[pending_key]
        except Exception:
            pass

def init_sheet_session(load_roster_fn):
    roster_sheet_url = st.secrets.get("ROSTER_SHEET_URL", "")
    roster_worksheet = st.secrets.get("ROSTER_WORKSHEET", "")
    output_sheet_url = st.secrets.get("OUTPUT_SHEET_URL", "")
    output_worksheet = st.secrets.get("OUTPUT_WORKSHEET", "")

    st.session_state.setdefault("roster_sheet_url", roster_sheet_url)
    st.session_state.setdefault("roster_worksheet", roster_worksheet)
    st.session_state["use_roster"] = True

    st.session_state.setdefault("output_sheet_url", output_sheet_url)
    st.session_state.setdefault("output_worksheet", output_worksheet)
    st.session_state["sync_enabled"] = True

    if (
        st.session_state.get("use_roster")
        and (st.session_state.get("roster_sheet_url") or "").strip()
        and "roster_cache_rows" not in st.session_state
    ):
        try:
            st.session_state["roster_cache_rows"] = load_roster_fn(
                st.session_state.get("roster_sheet_url", ""),
                worksheet=((st.session_state.get("roster_worksheet") or "").strip() or None),
            )
        except Exception as exc:
            st.session_state["roster_cache_rows"] = []
            st.session_state["roster_cache_error"] = f"{type(exc).__name__}: {repr(exc)}"

    if st.session_state.get("roster_cache_error"):
        st.warning(
            "Roster could not be loaded. Name matching may be unavailable. "
            f"({st.session_state['roster_cache_error']})"
        )
