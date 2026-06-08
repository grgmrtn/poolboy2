"""
email_helper.py — minimal SMTP send wrapper used by the digest + reminder jobs.

Env vars:
    SMTP_HOST       e.g. smtp.gmail.com
    SMTP_PORT       default 587
    SMTP_USER       full email address (auth)
    SMTP_PASS       password or app-specific password
    FROM_EMAIL      defaults to SMTP_USER if not set
    EMAIL_DRY_RUN   '1' prints to stdout instead of sending (test mode)

Use Gmail: enable 2FA → https://myaccount.google.com/apppasswords → generate
an "app password" → use that as SMTP_PASS. SMTP_HOST=smtp.gmail.com, PORT=587.
"""
import os
import smtplib
import ssl
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText


def send_email(to_addrs, subject, body, body_html=None, dry_run=None):
    """
    Send an email. to_addrs may be a string or list of strings. Returns the
    list of addresses successfully sent to; raises on hard SMTP failures.
    """
    if isinstance(to_addrs, str):
        to_addrs = [to_addrs]
    to_addrs = [a.strip() for a in to_addrs if a and a.strip()]
    if not to_addrs:
        return []

    if dry_run is None:
        dry_run = os.environ.get("EMAIL_DRY_RUN", "0") == "1"

    smtp_host  = os.environ.get("SMTP_HOST", "").strip()
    smtp_port  = int(os.environ.get("SMTP_PORT", "587"))
    smtp_user  = os.environ.get("SMTP_USER", "").strip()
    smtp_pass  = os.environ.get("SMTP_PASS", "")
    from_email = os.environ.get("FROM_EMAIL", "").strip() or smtp_user

    if not from_email:
        raise RuntimeError("FROM_EMAIL or SMTP_USER must be set")
    if not dry_run and not smtp_host:
        raise RuntimeError("SMTP_HOST must be set (or use EMAIL_DRY_RUN=1)")

    if body_html:
        msg = MIMEMultipart("alternative")
        msg.attach(MIMEText(body, "plain"))
        msg.attach(MIMEText(body_html, "html"))
    else:
        msg = MIMEText(body)
    msg["From"]    = from_email
    msg["To"]      = ", ".join(to_addrs)
    msg["Subject"] = subject

    if dry_run:
        print("─" * 60)
        print(f"[DRY-RUN]  to: {to_addrs}")
        print(f"[DRY-RUN]  from: {from_email}")
        print(f"[DRY-RUN]  subject: {subject}")
        print("[DRY-RUN]  body:")
        print(body)
        print("─" * 60)
        return list(to_addrs)

    # macOS Python.org builds don't trust the system keychain by default; the
    # bundled "Install Certificates.command" can fix this, but every machine
    # that runs the email scripts must remember to run it. Prefer certifi's
    # bundle (already a transitive dep of requests, which the smoke tests
    # use) so SSL verification just works out of the box.
    try:
        import certifi
        ctx = ssl.create_default_context(cafile=certifi.where())
    except ImportError:
        ctx = ssl.create_default_context()
    with smtplib.SMTP(smtp_host, smtp_port, timeout=30) as s:
        s.ehlo()
        s.starttls(context=ctx)
        s.ehlo()
        if smtp_user and smtp_pass:
            s.login(smtp_user, smtp_pass)
        s.send_message(msg, from_email, to_addrs)
    return list(to_addrs)
