"""Optional email. Everything works without it; with it, customers get receipts and password reset links.

Set SMTP_HOST (and usually SMTP_PORT, SMTP_USER, SMTP_PASSWORD, MAIL_FROM) to turn it on. Any provider
that offers SMTP works (Brevo, Zoho, Gmail app password, Mailgun, SendGrid...). Mail is sent on a
background thread, so a slow mail server never slows down checkout, and a failure never fails an order.
"""
import logging
import smtplib
import threading
from email.message import EmailMessage

log = logging.getLogger("infinity.mail")


def is_configured(config):
    return bool(config.get("SMTP_HOST")) or bool(config.get("MAIL_CAPTURE") is not None)


def _deliver(config, message):
    host, port = config["SMTP_HOST"], int(config.get("SMTP_PORT") or 587)
    try:
        if config.get("SMTP_SSL"):
            server = smtplib.SMTP_SSL(host, port, timeout=15)
        else:
            server = smtplib.SMTP(host, port, timeout=15)
            if config.get("SMTP_STARTTLS", True):
                server.starttls()
        with server:
            if config.get("SMTP_USER"):
                server.login(config["SMTP_USER"], config.get("SMTP_PASSWORD", ""))
            server.send_message(message)
        log.info("Sent email %r to %s", message["Subject"], message["To"])
    except Exception:  # noqa: BLE001 - mail must never break the shop
        log.exception("Could not send email %r to %s", message["Subject"], message["To"])


def send_mail(config, to, subject, body):
    """Queue one plain-text email. Returns True if it was queued."""
    if not to:
        return False
    message = EmailMessage()
    message["Subject"] = subject
    message["From"] = config.get("MAIL_FROM") or config.get("SMTP_USER") or "no-reply@localhost"
    message["To"] = to
    message.set_content(body)
    capture = config.get("MAIL_CAPTURE")
    if capture is not None:  # tests collect mail here instead of sending it
        capture.append({"to": to, "subject": subject, "body": body})
        return True
    if not config.get("SMTP_HOST"):
        return False
    threading.Thread(target=_deliver, args=(dict(config), message), daemon=True).start()
    return True
