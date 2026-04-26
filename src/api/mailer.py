"""Email sender for operator setup links.

Generic SMTP via stdlib so we're not tied to a specific provider (Gmail,
SendGrid, SES, Postmark all work the same). Config lives in env:

  SMTP_HOST         e.g. smtp.gmail.com   (empty → dev-mode: log to stdout)
  SMTP_PORT         default 587
  SMTP_USER         account for auth
  SMTP_PASSWORD     app password / API key
  SMTP_FROM         e.g. "AntiGreed <noreply@antigreed.local>"
  SMTP_STARTTLS     "1" (default) or "0" to disable STARTTLS
  PUBLIC_BASE_URL   e.g. http://localhost:8000 — base for setup links

If SMTP_HOST is unset we refuse to silently swallow the email; instead we
print the full setup URL to stdout so local dev still works end-to-end.
"""
from __future__ import annotations

import logging
import os
import smtplib
from email.message import EmailMessage

log = logging.getLogger(__name__)

_DEFAULT_FROM = "AntiGreed <noreply@antigreed.local>"
_DEFAULT_BASE_URL = "http://localhost:8000"


def public_base_url() -> str:
    return os.environ.get("PUBLIC_BASE_URL", _DEFAULT_BASE_URL).rstrip("/")


def smtp_configured() -> bool:
    return bool(os.environ.get("SMTP_HOST"))


def send_setup_email(*, to: str, ad_id: str, setup_url: str, expires_hours: int) -> None:
    """Deliver the setup link to a new operator.

    Raises on SMTP failure so the caller can surface it to the admin —
    silent failure here would leave an AD-ID stuck in limbo.
    """
    subject = f"Set up your AntiGreed access ({ad_id})"
    text_body = (
        f"An admin has assigned you AD-ID {ad_id} on the AntiGreed trading bot.\n\n"
        f"Open this link to choose your password (valid for {expires_hours}h):\n"
        f"  {setup_url}\n\n"
        f"If you weren't expecting this, ignore the email — the link will expire on its own.\n"
    )
    html_body = f"""<!doctype html>
<html><body style="font-family:system-ui,sans-serif;line-height:1.5;color:#0b1220">
  <h2 style="color:#0E7C42">AntiGreed · operator setup</h2>
  <p>An admin has assigned you AD-ID <code style="background:#eee;padding:2px 6px;border-radius:4px">{ad_id}</code> on the trading bot.</p>
  <p><a href="{setup_url}"
        style="display:inline-block;background:#0E7C42;color:#fff;padding:10px 18px;
               border-radius:6px;text-decoration:none">Set your password</a></p>
  <p style="color:#666;font-size:13px">Link expires in {expires_hours} hours. If you weren't expecting this email, ignore it.</p>
</body></html>"""

    if not smtp_configured():
        log.warning(
            "SMTP not configured — printing setup link to stdout instead of sending. "
            "Set SMTP_HOST/SMTP_USER/SMTP_PASSWORD to enable email delivery."
        )
        print(
            "\n" + "=" * 72
            + f"\n[DEV] Setup link for {to} (AD-ID {ad_id}):\n  {setup_url}\n"
            + "=" * 72 + "\n",
            flush=True,
        )
        return

    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = os.environ.get("SMTP_FROM", _DEFAULT_FROM)
    msg["To"] = to
    msg.set_content(text_body)
    msg.add_alternative(html_body, subtype="html")

    host = os.environ["SMTP_HOST"]
    port = int(os.environ.get("SMTP_PORT", "587"))
    user = os.environ.get("SMTP_USER")
    password = os.environ.get("SMTP_PASSWORD")
    use_tls = os.environ.get("SMTP_STARTTLS", "1") != "0"

    with smtplib.SMTP(host, port, timeout=15) as s:
        s.ehlo()
        if use_tls:
            s.starttls()
            s.ehlo()
        if user and password:
            s.login(user, password)
        s.send_message(msg)
    log.info("Sent setup email to %s for AD-ID %s", to, ad_id)
