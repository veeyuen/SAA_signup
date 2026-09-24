from dataclasses import dataclass
import streamlit as st

@dataclass(frozen=True)
class LoginConfig:
    app_title: str
    info_message: str = "Please log in with email to continue."
    button_label: str = "Log in with email"
    provider: str = "auth0"

def _secret_list(section: str, key: str) -> list[str]:
    try:
        value = st.secrets.get(section, {}).get(key, [])
    except Exception:
        value = []
    if isinstance(value, str):
        value = [x.strip() for x in value.split(",") if x.strip()]
    return [str(x).strip().lower() for x in (value or []) if str(x).strip()]

def require_login(required_group: str = "entry", config: LoginConfig | None = None) -> str:
    config = config or LoginConfig(app_title="SAA Signup")

    if not getattr(st, "user", None) or not st.user.is_logged_in:
        st.title(config.app_title)
        st.info(config.info_message)
        st.button(config.button_label, on_click=lambda: st.login(config.provider))
        st.stop()

    email = (getattr(st.user, "email", "") or "").strip().lower()
    if not email:
        st.error("Login succeeded but no email address was returned. Please contact the administrator.")
        st.button("Log out", on_click=st.logout)
        st.stop()

    # For Phase 1, ordinary entry authorization is done from USERS/ORGANIZATIONS.
    if required_group == "admin":
        admin_emails = set(_secret_list("access", "admin_emails"))
        if email not in admin_emails:
            st.error(f"Admin access is required. Access denied for {email}.")
            st.button("Log out", on_click=st.logout)
            st.stop()

    with st.sidebar:
        st.caption(f"Logged in as: {email}")
        st.button("Log out", on_click=st.logout)

    return email
