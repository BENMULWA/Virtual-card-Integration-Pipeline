# =========================================================
# mail.py - outbound transactional email
# =========================================================
#
# Sends the email verification link. Uses a generic HTTP mail API (SendGrid,
# Mailgun, SES-proxy - whatever you point MAIL_API_URL at). When no mail provider
# is configured the link is logged instead of sent, so the flow stays testable
# in development without a mail account.

import logging
from typing import Optional

import httpx

from app.core.config import (
    APP_PUBLIC_URL,
    MAIL_API_KEY,
    MAIL_API_URL,
    MAIL_FROM,
    MAIL_FROM_NAME,
)

logger = logging.getLogger("mail")

VERIFY_PATH = "/api/v1/users/verify-email"


def configured() -> bool:
    return bool(MAIL_API_URL and MAIL_API_KEY and MAIL_FROM)


def verification_link(token: str) -> str:
    """The URL the user clicks. The token travels in the fragment-less query string."""
    base = APP_PUBLIC_URL or "http://localhost:8000"
    return f"{base}/verify-email?token={token}"


async def send_verification_email(to_email: str, full_name: str, token: str) -> bool:
    """
    Send the verification email. Returns True if it was handed to the provider,
    False if it was only logged (no provider configured).
    """
    link = verification_link(token)
    subject = "Verify your Mamlaka card application"
    text = (
        f"Hi {full_name},\n\n"
        f"Confirm your email address to continue with your Mamlaka virtual card application:\n\n"
        f"{link}\n\n"
        f"This link expires in 24 hours. If you did not create this account, ignore this email.\n\n"
        f"- The Mamlaka Team"
    )
    html = (
        f"<p>Hi {full_name},</p>"
        f"<p>Confirm your email address to continue with your Mamlaka virtual card application.</p>"
        f'<p><a href="{link}" style="display:inline-block;padding:12px 20px;background:#0F6F3C;'
        f'color:#fff;text-decoration:none;border-radius:6px">Verify my email</a></p>'
        f'<p style="font-size:13px;color:#666">Or paste this link into your browser:<br>{link}</p>'
        f"<p>This link expires in 24 hours. If you did not create this account, ignore this email.</p>"
        f"<p>- The Mamlaka Team</p>"
    )

    if not configured():
        logger.warning("MAIL NOT CONFIGURED - verification link for %s: %s", to_email, link)
        return False

    payload = {
        "from": {"email": MAIL_FROM, "name": MAIL_FROM_NAME},
        "to": [{"email": to_email, "name": full_name}],
        "subject": subject,
        "content": [{"type": "text/plain", "value": text}, {"type": "text/html", "value": html}],
    }
    try:
        async with httpx.AsyncClient(timeout=20) as client:
            response = await client.post(
                MAIL_API_URL,
                json=payload,
                headers={"Authorization": f"Bearer {MAIL_API_KEY}"},
            )
        if response.status_code >= 300:
            logger.error("mail provider rejected the message: HTTP %s", response.status_code)
            return False
        logger.info("verification email sent to %s", to_email)
        return True
    except httpx.HTTPError as exc:
        logger.error("mail send failed for %s: %s", to_email, type(exc).__name__)
        return False


async def send_work_order_receipt(to_email: str, full_name: str, order_no: str) -> None:
    """Confirmation that a support ticket was raised with Wasabi."""
    if not configured():
        logger.info("MAIL NOT CONFIGURED - work order %s for %s", order_no, to_email)
        return
    payload = {
        "from": {"email": MAIL_FROM, "name": MAIL_FROM_NAME},
        "to": [{"email": to_email, "name": full_name}],
        "subject": "We are reviewing your card application",
        "content": [{
            "type": "text/plain",
            "value": (
                f"Hi {full_name},\n\n"
                f"We have escalated your card application to our compliance team "
                f"(reference {order_no}). We will update you as soon as there is a decision.\n\n"
                f"- The Mamlaka Team"
            ),
        }],
    }
    try:
        async with httpx.AsyncClient(timeout=20) as client:
            await client.post(MAIL_API_URL, json=payload,
                              headers={"Authorization": f"Bearer {MAIL_API_KEY}"})
    except httpx.HTTPError as exc:
        logger.error("work order receipt failed for %s: %s", to_email, type(exc).__name__)
