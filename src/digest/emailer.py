"""Send email through the Resend REST API (no SDK; one urllib call with retries)."""

from __future__ import annotations

import json
import logging
import time
import urllib.error
import urllib.request

log = logging.getLogger(__name__)

RESEND_ENDPOINT = "https://api.resend.com/emails"
_RETRYABLE_STATUS = {429, 500, 502, 503, 504}
_BACKOFF_SECONDS = (2, 8)


class EmailError(Exception):
    pass


def build_payload(email_from: str, email_to: str, subject: str,
                  html_body: str, text_body: str) -> dict:
    return {
        "from": email_from,
        "to": [addr.strip() for addr in email_to.split(",") if addr.strip()],
        "subject": subject,
        "html": html_body,
        "text": text_body,
    }


def send_email(api_key: str, email_from: str, email_to: str, subject: str,
               html_body: str, text_body: str, timeout: int = 30) -> str:
    """Send one email; returns the Resend message id."""
    if not api_key:
        raise EmailError("RESEND_API_KEY is not set — add it to .env")
    payload = build_payload(email_from, email_to, subject, html_body, text_body)
    if not payload["to"]:
        raise EmailError("EMAIL_TO is not set — add it to .env")
    body = json.dumps(payload).encode()

    last_error: Exception | None = None
    for attempt in range(len(_BACKOFF_SECONDS) + 1):
        req = urllib.request.Request(
            RESEND_ENDPOINT,
            data=body,
            method="POST",
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
                # Resend sits behind Cloudflare, which rejects urllib's default
                # Python-urllib user agent with a 403 (error code 1010).
                "User-Agent": "aie-yt-daily-digest/0.1",
            },
        )
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                data = json.loads(resp.read().decode() or "{}")
                message_id = data.get("id", "")
                log.info("email sent via Resend (id=%s)", message_id)
                return message_id
        except urllib.error.HTTPError as exc:
            detail = ""
            try:
                detail = exc.read().decode()[:500]
            except Exception:
                pass
            if exc.code in _RETRYABLE_STATUS and attempt < len(_BACKOFF_SECONDS):
                last_error = exc
                wait = _BACKOFF_SECONDS[attempt]
                log.warning("Resend HTTP %d, retrying in %ds: %s", exc.code, wait, detail)
                time.sleep(wait)
                continue
            raise EmailError(f"Resend rejected the email (HTTP {exc.code}): {detail}") from exc
        except urllib.error.URLError as exc:
            if attempt < len(_BACKOFF_SECONDS):
                last_error = exc
                wait = _BACKOFF_SECONDS[attempt]
                log.warning("Resend unreachable, retrying in %ds: %s", wait, exc)
                time.sleep(wait)
                continue
            raise EmailError(f"could not reach Resend: {exc}") from exc
    raise EmailError(f"could not reach Resend: {last_error}")
