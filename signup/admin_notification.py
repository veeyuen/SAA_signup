from __future__ import annotations

import html
import smtplib
from email.message import EmailMessage
from typing import Any


class AdminNotificationError(RuntimeError):
    pass


def _clean(value: Any) -> str:
    return "" if value is None else str(value).strip()


def send_admin_amendment_email(
    *,
    smtp_host: str,
    smtp_port: int,
    smtp_user: str,
    smtp_password: str,
    smtp_from: str,
    to_email: str,
    athlete_name: str,
    order_id: str,
    registration_id: str,
    entry_id: str,
    changes: list[tuple[str, str, str]],
    reason: str,
) -> None:
    """Notify a registrant that SAA Admin amended registration details.

    Saving the amendment must not depend on SMTP availability. The caller is
    expected to audit notification success/failure separately.
    """
    host = _clean(smtp_host)
    user = _clean(smtp_user)
    password = str(smtp_password or "").strip()
    sender = _clean(smtp_from) or user
    recipient = _clean(to_email)

    if not recipient:
        raise AdminNotificationError("The registration has no email address.")
    if not host or not user or not password:
        raise AdminNotificationError(
            "SMTP_HOST, SMTP_USER and SMTP_PASS are required for amendment notifications."
        )

    subject = "SAA registration amended"
    change_lines = [
        f"- {label}: {before or '(blank)'} -> {after or '(blank)'}"
        for label, before, after in changes
    ]
    text_body = (
        "Dear Participant,\n\n"
        "Singapore Athletics has amended details in your registration.\n\n"
        f"Athlete: {athlete_name}\n"
        f"Order: {order_id}\n"
        f"Registration: {registration_id}\n"
        f"Entry: {entry_id}\n\n"
        "Changes:\n"
        + "\n".join(change_lines)
        + "\n\n"
        f"Reason: {reason}\n\n"
        "If you believe any detail is incorrect, please contact Singapore Athletics.\n\n"
        "SAA\n"
    )

    rows = "".join(
        "<tr>"
        f"<td style='padding:6px 10px;border:1px solid #ddd;'>{html.escape(label)}</td>"
        f"<td style='padding:6px 10px;border:1px solid #ddd;'>{html.escape(before or '(blank)')}</td>"
        f"<td style='padding:6px 10px;border:1px solid #ddd;'>{html.escape(after or '(blank)')}</td>"
        "</tr>"
        for label, before, after in changes
    )
    html_body = f"""<!doctype html>
<html><body style="font-family:Arial,Helvetica,sans-serif;color:#222;">
<p>Dear Participant,</p>
<p>Singapore Athletics has amended details in your registration.</p>
<p>
Athlete: {html.escape(athlete_name)}<br>
Order: {html.escape(order_id)}<br>
Registration: {html.escape(registration_id)}<br>
Entry: {html.escape(entry_id)}
</p>
<table style="border-collapse:collapse;">
<tr><th style="padding:6px 10px;border:1px solid #ddd;">Field</th><th style="padding:6px 10px;border:1px solid #ddd;">Before</th><th style="padding:6px 10px;border:1px solid #ddd;">After</th></tr>
{rows}
</table>
<p><strong>Reason:</strong> {html.escape(reason)}</p>
<p>If you believe any detail is incorrect, please contact Singapore Athletics.</p>
<p>SAA</p>
</body></html>"""

    message = EmailMessage()
    message["From"] = sender
    message["To"] = recipient
    message["Subject"] = subject
    message.set_content(text_body)
    message.add_alternative(html_body, subtype="html")

    try:
        with smtplib.SMTP(host, int(smtp_port or 587), timeout=20) as smtp:
            smtp.ehlo()
            smtp.starttls()
            smtp.ehlo()
            smtp.login(user, password)
            smtp.send_message(message)
    except Exception as exc:
        raise AdminNotificationError(f"{type(exc).__name__}: {exc}") from exc
