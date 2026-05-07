"""Email sender for operator setup links.

Two providers are supported. Pick one with EMAIL_PROVIDER:

  smtp    (default) — generic SMTP via stdlib. Config:
    SMTP_HOST       e.g. smtp.gmail.com
    SMTP_PORT       default 587
    SMTP_USER       account for auth
    SMTP_PASSWORD   app password / API key
    SMTP_FROM       e.g. "AntiGreed <noreply@antigreed.local>"
    SMTP_STARTTLS   "1" (default) or "0" to disable STARTTLS

  resend  — Resend HTTP API. Use this when the VPS blocks outbound 587/465.
    RESEND_API_KEY  Resend API key (re_...)
    SMTP_FROM       reused as the From: address (must be a verified sender)

Shared config:
  PUBLIC_BASE_URL   e.g. http://localhost:8000 — base for setup links

If neither provider is configured we refuse to silently swallow the email;
instead we print the full setup URL to stdout so local dev still works.
"""
from __future__ import annotations

import json
import logging
import os
import smtplib
import urllib.error
import urllib.request
from email.message import EmailMessage

log = logging.getLogger(__name__)

_DEFAULT_FROM = "AntiGreed <noreply@antigreed.local>"
_DEFAULT_BASE_URL = "http://localhost:8000"
_RESEND_URL = "https://api.resend.com/emails"


def public_base_url() -> str:
    return os.environ.get("PUBLIC_BASE_URL", _DEFAULT_BASE_URL).rstrip("/")


def _provider() -> str:
    return (os.environ.get("EMAIL_PROVIDER") or "smtp").strip().lower()


def smtp_configured() -> bool:
    return bool(os.environ.get("SMTP_HOST"))


def mailer_configured() -> bool:
    """True if any send path is wired up — used by the API to decide whether
    to surface the setup URL inline (dev/no-mailer) or trust delivery."""
    if _provider() == "resend":
        return bool(os.environ.get("RESEND_API_KEY"))
    return smtp_configured()


def send_subscription_expired_email(*, to: str, ad_id: str) -> None:
    """Notify an operator that their subscription has lapsed. Same
    delivery path as setup links — Resend if configured, SMTP fallback,
    or stdout in dev. Raises on hard delivery failure.
    """
    subject = f"Your AntiGreed subscription has expired ({ad_id})"
    text_body = (
        f"Hi,\n\n"
        f"The subscription for your AntiGreed AD-ID {ad_id} has expired and "
        f"your access has been suspended.\n\n"
        f"To renew, please contact the admin who assigned this ID and ask them "
        f"to extend your subscription. Once renewed, you'll be able to log "
        f"back in immediately with the same AD-ID and password.\n\n"
        f"Thanks,\nAntiGreed\n"
    )
    html_body = f"""<!doctype html>
<html><body style="font-family:system-ui,sans-serif;line-height:1.5;color:#0b1220">
  <h2 style="color:#dc2626">AntiGreed · subscription expired</h2>
  <p>The subscription for your AD-ID
     <code style="background:#eee;padding:2px 6px;border-radius:4px">{ad_id}</code>
     has expired. Your access has been suspended.</p>
  <p>To renew, please contact the admin who assigned this ID and ask them
     to extend your subscription. Once renewed, you'll be able to log
     back in immediately with the same AD-ID and password.</p>
  <p style="color:#666;font-size:13px">— AntiGreed</p>
</body></html>"""
    _send(to=to, subject=subject, text=text_body, html=html_body)
    log.info("Sent subscription-expired email to %s for AD-ID %s", to, ad_id)


def _send(*, to: str, subject: str, text: str, html: str) -> None:
    """Internal: route a built message through the active provider."""
    provider = _provider()
    if provider == "resend":
        _send_via_resend(to=to, subject=subject, text=text, html=html)
        return

    if not smtp_configured():
        log.warning(
            "No mailer configured — would send '%s' to %s; printing instead.",
            subject, to,
        )
        print(f"\n--- email to {to} ---\nSubject: {subject}\n{text}\n---\n", flush=True)
        return

    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = os.environ.get("SMTP_FROM", _DEFAULT_FROM)
    msg["To"] = to
    msg.set_content(text)
    msg.add_alternative(html, subtype="html")

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

    provider = _provider()
    if provider == "resend":
        _send_via_resend(to=to, subject=subject, text=text_body, html=html_body)
        log.info("Sent setup email to %s for AD-ID %s via Resend", to, ad_id)
        return

    if not smtp_configured():
        log.warning(
            "No mailer configured — printing setup link to stdout instead of sending. "
            "Set EMAIL_PROVIDER=resend with RESEND_API_KEY, or SMTP_HOST/USER/PASSWORD."
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
    log.info("Sent setup email to %s for AD-ID %s via SMTP", to, ad_id)


def _send_via_resend(*, to: str, subject: str, text: str, html: str) -> None:
    api_key = os.environ.get("RESEND_API_KEY")
    if not api_key:
        raise RuntimeError("RESEND_API_KEY not set — cannot use EMAIL_PROVIDER=resend")
    payload = {
        "from": os.environ.get("SMTP_FROM", _DEFAULT_FROM),
        "to": [to],
        "subject": subject,
        "text": text,
        "html": html,
    }
    req = urllib.request.Request(
        _RESEND_URL,
        data=json.dumps(payload).encode("utf-8"),
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            body = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        # Resend returns JSON error bodies; bubble them up so the admin can
        # see "domain not verified" / "invalid api key" / etc.
        detail = e.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"resend HTTP {e.code}: {detail}") from None
    if "id" not in body:
        raise RuntimeError(f"resend unexpected response: {body}")
