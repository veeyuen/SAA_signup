from __future__ import annotations

import html
import os
import smtplib
from email.message import EmailMessage


def send_paid_confirmation_email(
    *,
    to_email: str,
    full_name: str,
    team_name: str,
    events: list[str],
    registration_id: str,
    amount: str,
    currency: str,
):
    host = str(os.environ.get("SMTP_HOST", "") or "").strip()
    user = str(os.environ.get("SMTP_USER", "") or "").strip()
    password = str(os.environ.get("SMTP_PASS", "") or "").strip()
    sender = str(
        os.environ.get("SMTP_FROM", "") or user
    ).strip()
    port = int(os.environ.get("SMTP_PORT", "587"))

    if not host or not user or not password:
        raise RuntimeError(
            "SMTP_HOST, SMTP_USER and SMTP_PASS are required."
        )

    event_text = ", ".join(str(event) for event in (events or []))
    subject = "Registration confirmed"

    reference_label = (
        "Order reference"
        if str(registration_id or "").upper().startswith("ORD-")
        else "Registration reference"
    )

    text_body = (
        "Dear Participant,\n\n"
        "Your payment has been received and your registration "
        "is confirmed.\n\n"
        f"{reference_label}: {registration_id}\n"
        f"Full Name: {full_name}\n"
        f"Event(s): {event_text}\n"
        f"Team: {team_name}\n"
        f"Amount paid: {currency} {amount}\n\n"
        "Thank you.\n\n"
        "SAA\n"
    )

    html_body = f"""<!doctype html>
    <html>
      <body style="font-family:Arial,Helvetica,sans-serif;color:#222;">
        <p>Dear Participant,</p>
        <p>Your payment has been received and your registration is confirmed.</p>
        <p>
          {html.escape(reference_label)}: {html.escape(registration_id)}<br>
          Full Name: {html.escape(full_name)}<br>
          Event(s): {html.escape(event_text)}<br>
          Team: {html.escape(team_name)}<br>
          Amount paid: {html.escape(currency)} {html.escape(amount)}
        </p>
        <p>Thank you.</p>
        <p>SAA</p>
      </body>
    </html>"""

    message = EmailMessage()
    message["From"] = sender
    message["To"] = to_email
    message["Subject"] = subject
    message.set_content(text_body)
    message.add_alternative(html_body, subtype="html")

    with smtplib.SMTP(host, port, timeout=20) as smtp:
        smtp.ehlo()
        smtp.starttls()
        smtp.ehlo()
        smtp.login(user, password)
        smtp.send_message(message)
