"""Stable Streamlit Community Cloud entrypoint for the SAA registration app.

Keep this filename as the deployed Community Cloud entrypoint so the existing
app URL, secrets and deployment coordinates do not need to change.  The
user-facing page labels are defined explicitly with ``st.Page``.
"""

import streamlit as st

registration_page = st.Page(
    "Competition_Athlete_Registrations.py",
    title="Competition Athlete Registrations",
    default=True,
)
admin_page = st.Page(
    "pages/2_Admin_Operations.py",
    title="Admin Operations",
)
financial_page = st.Page(
    "pages/3_Financial_Reporting.py",
    title="Financial Reporting",
)
moe_page = st.Page(
    "pages/4_MOE_Billing.py",
    title="MOE Billing",
)
hytek_page = st.Page(
    "pages/5_HyTek_Exports.py",
    title="Hy-Tek Exports",
)

navigation = st.navigation(
    [registration_page, admin_page, financial_page, moe_page, hytek_page],
    position="sidebar",
)
navigation.run()
