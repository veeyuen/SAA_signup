import html
import smtplib
from email.message import EmailMessage
import streamlit as st

def send_confirmation_email_smtp(
    to_email: str,
    subject: str,
    body: str,
    html_body: str | None = None,
) -> None:
    host = (st.secrets.get("SMTP_HOST", "") or "").strip()
    user = (st.secrets.get("SMTP_USER", "") or "").strip()
    password = (st.secrets.get("SMTP_PASS", "") or "").strip()
    sender = (st.secrets.get("SMTP_FROM", "") or user).strip()
    port = int(st.secrets.get("SMTP_PORT", 587) or 587)

    if not host or not user or not password:
        raise RuntimeError("SMTP secrets are missing (SMTP_HOST/SMTP_USER/SMTP_PASS).")

    msg = EmailMessage()
    msg["From"] = sender
    msg["To"] = to_email
    msg["Subject"] = subject
    msg.set_content(body)
    if html_body:
        msg.add_alternative(html_body, subtype="html")

    with smtplib.SMTP(host, port, timeout=20) as smtp:
        smtp.ehlo()
        smtp.starttls()
        smtp.ehlo()
        smtp.login(user, password)
        smtp.send_message(msg)

def build_confirmation_email_html(
    full_name: str,
    events: list[str],
    team_name: str,
    unique_id: str,
) -> str:
    safe_name = html.escape(full_name or "")
    safe_team = html.escape(team_name or "")
    safe_uid = html.escape(unique_id or "")
    safe_events = ", ".join(html.escape(str(x or "")) for x in (events or []))

    return (
        "<!doctype html><html><body>"
        "<p>Dear Participant,</p>"
        "<p>Your entry has been successfully received.</p>"
        f"<p>Full Name: {safe_name}<br>"
        f"Event(s): {safe_events}<br>"
        f"Team: {safe_team}<br>"
        f"Unique ID: {safe_uid}</p>"
        "<p>Thank you.</p><p>SAA</p>"
        "</body></html>"
    )
