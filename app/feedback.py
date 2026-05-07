"""feedback.py — POST /feedback endpoint that emails user feedback via Resend.

Public, unauthenticated endpoint. Spam protection layered as cheaply as
possible:
  - Strict discriminated-union schema (`extra="forbid"`) — random/scraped
    POSTs that don't match one of the four variants get a 422.
  - Length caps on every string field.
  - Per-IP sliding-window rate limit (in-process; defaults: 5 / hour).
    Trades durability across restarts for zero infrastructure. If abuse
    becomes real, swap to a Redis-backed limiter (we already run Redis
    for the LangGraph checkpointer).

Resend integration uses httpx directly rather than the `resend` SDK so we
don't add a runtime dependency for a single POST. Sending domain isn't
verified yet — by default we send from `onboarding@resend.dev`, which is
only deliverable to the email tied to your Resend account. Once a domain
is verified, set FEEDBACK_FROM_EMAIL to a sender on that domain.

The frontend contract is a discriminated union keyed on `type`. Source of
truth on the frontend lives at `src/api/types.ts`. Variants:
  - "bug"          : intent + problem (+ optional query)
  - "data_source"  : category (enum) + source + usefulness
  - "loading_text" : phrase
  - "general"      : message

All variants accept an optional `email` for reply-to. Required strings
must be non-empty after trimming (the frontend trims; the server's
min_length=1 catches anything that slips through).
"""

from __future__ import annotations

import logging
import os
import time
import uuid
from collections import defaultdict, deque
from typing import Annotated, Any, Literal, Union

import httpx
from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, ConfigDict, EmailStr, Field

logger = logging.getLogger(__name__)

router = APIRouter()


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

RESEND_API_KEY_ENV = "RESEND_API_KEY"
FEEDBACK_TO_EMAIL_ENV = "FEEDBACK_TO_EMAIL"
FEEDBACK_FROM_EMAIL_ENV = "FEEDBACK_FROM_EMAIL"

# Until a sending domain is verified in Resend, this address is the only
# legal sender — and it can only deliver to the email registered on the
# Resend account. Override via FEEDBACK_FROM_EMAIL once the domain is up.
DEFAULT_FROM_EMAIL = "Denver Bot Feedback <onboarding@resend.dev>"

RESEND_API_URL = "https://api.resend.com/emails"
RESEND_TIMEOUT_SECONDS = 10.0

# Rate limit knobs. Tuned conservatively — feedback is low-frequency by
# nature, so a real user won't bump 5/hour and a bot will.
RATE_LIMIT_MAX_REQUESTS = 5
RATE_LIMIT_WINDOW_SECONDS = 60 * 60  # 1 hour


# ---------------------------------------------------------------------------
# Schema — discriminated union per frontend contract
# ---------------------------------------------------------------------------


# Locked enum mirroring the frontend dropdown. Adding a category requires a
# coordinated frontend + backend change.
DataSourceCategory = Literal[
    "Transportation",
    "Demographics",
    "Safety",
    "Housing",
    "Environment",
    "Other",
]


class _FeedbackBase(BaseModel):
    """Common base. extra='forbid' makes the schema the contract — any
    unknown field at the top level is a 422, which keeps drift between
    frontend and backend visible immediately."""

    model_config = ConfigDict(extra="forbid")

    email: EmailStr | None = None


class BugFeedback(_FeedbackBase):
    type: Literal["bug"]
    intent: str = Field(..., min_length=1, max_length=2000)
    problem: str = Field(..., min_length=1, max_length=4000)
    # Frontend auto-fills from last user message; user-editable; omitted
    # entirely from the JSON when blank.
    query: str | None = Field(default=None, max_length=4000)


class DataSourceFeedback(_FeedbackBase):
    type: Literal["data_source"]
    category: DataSourceCategory
    source: str = Field(..., min_length=1, max_length=2000)
    usefulness: str = Field(..., min_length=1, max_length=4000)


class LoadingTextFeedback(_FeedbackBase):
    type: Literal["loading_text"]
    phrase: str = Field(..., min_length=1, max_length=200)


class GeneralFeedback(_FeedbackBase):
    type: Literal["general"]
    message: str = Field(..., min_length=1, max_length=4000)


FeedbackBody = Annotated[
    Union[BugFeedback, DataSourceFeedback, LoadingTextFeedback, GeneralFeedback],
    Field(discriminator="type"),
]


# ---------------------------------------------------------------------------
# Rate limiter
# ---------------------------------------------------------------------------


_request_log: dict[str, deque[float]] = defaultdict(deque)


def _rate_limit_check(client_ip: str) -> bool:
    """Sliding-window allow-check. Mutates the per-IP deque in place,
    expiring entries outside the window before counting. Returns True if
    the request is within budget, False if it should be rejected."""
    now = time.monotonic()
    cutoff = now - RATE_LIMIT_WINDOW_SECONDS
    log = _request_log[client_ip]
    while log and log[0] < cutoff:
        log.popleft()
    if len(log) >= RATE_LIMIT_MAX_REQUESTS:
        return False
    log.append(now)
    return True


def _reset_rate_limiter() -> None:
    """Clear the per-IP rate-limit log. Test-only — keeps tests
    independent of each other when they share the module-level dict."""
    _request_log.clear()


# ---------------------------------------------------------------------------
# Email composition — variant-aware
# ---------------------------------------------------------------------------


def _html_escape(value: Any) -> str:
    """Minimal HTML escape for email bodies. Avoids bringing in markupsafe
    for one helper. Order matters: & first, then <, >, quote, apostrophe."""
    text = str(value)
    return (
        text.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
        .replace("'", "&#x27;")
    )


def _snippet(value: str, n: int) -> str:
    """One-line summary suitable for an email subject."""
    return value.strip().replace("\n", " ")[:n]


def _compose_subject(body: BugFeedback | DataSourceFeedback | LoadingTextFeedback | GeneralFeedback) -> str:
    if isinstance(body, BugFeedback):
        return f"[Denver Bot Feedback] Bug: {_snippet(body.intent, 80)}"
    if isinstance(body, DataSourceFeedback):
        return f"[Denver Bot Feedback] Data source ({body.category}): {_snippet(body.source, 80)}"
    if isinstance(body, LoadingTextFeedback):
        return f"[Denver Bot Feedback] Loading phrase: {_snippet(body.phrase, 80)}"
    # GeneralFeedback
    return f"[Denver Bot Feedback] General: {_snippet(body.message, 80)}"


def _row(label: str, value: Any) -> str:
    if value is None or value == "":
        return ""
    return (
        f"<tr><td style='padding:4px 12px 4px 0;vertical-align:top;'>"
        f"<strong>{_html_escape(label)}</strong></td>"
        f"<td style='padding:4px 0;'>{_html_escape(value)}</td></tr>"
    )


def _compose_html(body: BugFeedback | DataSourceFeedback | LoadingTextFeedback | GeneralFeedback, feedback_id: str) -> str:
    rows: list[str] = [_row("Feedback ID", feedback_id), _row("Type", body.type)]

    if isinstance(body, BugFeedback):
        rows.append(_row("Intent", body.intent))
        rows.append(_row("Problem", body.problem))
        rows.append(_row("Query", body.query))
    elif isinstance(body, DataSourceFeedback):
        rows.append(_row("Category", body.category))
        rows.append(_row("Source", body.source))
        rows.append(_row("Usefulness", body.usefulness))
    elif isinstance(body, LoadingTextFeedback):
        rows.append(_row("Phrase", body.phrase))
    else:  # GeneralFeedback
        rows.append(_row("Message", body.message))

    rows.append(_row("Email", body.email))

    return (
        "<table style='font-family:system-ui,sans-serif;border-collapse:collapse;'>"
        + "".join(rows)
        + "</table>"
    )


def _compose_text(body: BugFeedback | DataSourceFeedback | LoadingTextFeedback | GeneralFeedback, feedback_id: str) -> str:
    parts = [f"Feedback ID: {feedback_id}", f"Type: {body.type}"]

    if isinstance(body, BugFeedback):
        parts.append(f"Intent: {body.intent}")
        parts.append(f"Problem: {body.problem}")
        if body.query:
            parts.append(f"Query: {body.query}")
    elif isinstance(body, DataSourceFeedback):
        parts.append(f"Category: {body.category}")
        parts.append(f"Source: {body.source}")
        parts.append(f"Usefulness: {body.usefulness}")
    elif isinstance(body, LoadingTextFeedback):
        parts.append(f"Phrase: {body.phrase}")
    else:  # GeneralFeedback
        parts.append(f"Message: {body.message}")

    if body.email:
        parts.append(f"Email: {body.email}")

    return "\n".join(parts)


# ---------------------------------------------------------------------------
# Resend wrapper
# ---------------------------------------------------------------------------


class FeedbackEmailError(Exception):
    """Raised on any Resend delivery failure. Wraps the underlying httpx
    error or HTTP status so the endpoint can return a generic 502 without
    leaking provider details to the client."""


def _resend_payload(
    body: BugFeedback | DataSourceFeedback | LoadingTextFeedback | GeneralFeedback,
    feedback_id: str,
    from_email: str,
    to_email: str,
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "from": from_email,
        "to": [to_email],
        "subject": _compose_subject(body),
        "html": _compose_html(body, feedback_id),
        "text": _compose_text(body, feedback_id),
    }
    # Set reply_to so hitting "Reply" in your inbox goes back to the
    # submitter (when they opted to share their email).
    if body.email:
        payload["reply_to"] = [body.email]
    return payload


async def _send_via_resend(
    body: BugFeedback | DataSourceFeedback | LoadingTextFeedback | GeneralFeedback,
    feedback_id: str,
) -> None:
    api_key = os.getenv(RESEND_API_KEY_ENV)
    to_email = os.getenv(FEEDBACK_TO_EMAIL_ENV)
    from_email = os.getenv(FEEDBACK_FROM_EMAIL_ENV) or DEFAULT_FROM_EMAIL

    if not api_key or not to_email:
        # Caller is expected to have already 503'd in this case — this is
        # a defense-in-depth check, not the primary gate.
        raise FeedbackEmailError("Resend not configured")

    payload = _resend_payload(body, feedback_id=feedback_id, from_email=from_email, to_email=to_email)

    try:
        async with httpx.AsyncClient(timeout=RESEND_TIMEOUT_SECONDS) as client:
            response = await client.post(
                RESEND_API_URL,
                headers={
                    "Authorization": f"Bearer {api_key}",
                    "Content-Type": "application/json",
                },
                json=payload,
            )
    except httpx.HTTPError as exc:
        raise FeedbackEmailError(f"transport error: {exc}") from exc

    if response.status_code >= 400:
        # Capture body snippet for server-side log; do NOT bubble it back
        # to the client (Resend errors can echo the API key in some
        # malformed-request paths).
        raise FeedbackEmailError(
            f"resend returned {response.status_code}: {response.text[:300]}"
        )


# ---------------------------------------------------------------------------
# Endpoint
# ---------------------------------------------------------------------------


@router.post("/feedback")
async def feedback_endpoint(
    body: FeedbackBody,
    request: Request,
) -> dict[str, Any]:
    """Send the submitted feedback to the configured inbox via Resend.

    Returns {"success": True, "id": "<uuid>"} on success. Pydantic
    validation handles 422 for malformed bodies (wrong type, missing
    required field, invalid category, oversized strings). 429 on
    rate-limit, 503 when Resend env config is missing, 502 on delivery
    failure.

    The id is a server-generated UUID — included in both the email
    subject/body (for audit) and the response (so the frontend can
    surface it as a receipt to the user).
    """
    client_ip = request.client.host if request.client else "unknown"
    if not _rate_limit_check(client_ip):
        logger.warning("feedback rate limit hit from %s", client_ip)
        raise HTTPException(status_code=429, detail="too many requests")

    if not os.getenv(RESEND_API_KEY_ENV) or not os.getenv(FEEDBACK_TO_EMAIL_ENV):
        logger.error(
            "feedback endpoint hit but Resend config missing (RESEND_API_KEY / FEEDBACK_TO_EMAIL)"
        )
        raise HTTPException(status_code=503, detail="feedback service not configured")

    feedback_id = str(uuid.uuid4())

    try:
        await _send_via_resend(body, feedback_id=feedback_id)
    except FeedbackEmailError:
        logger.exception("feedback delivery failed (id=%s)", feedback_id)
        raise HTTPException(status_code=502, detail="feedback delivery failed")

    logger.info("feedback delivered: id=%s type=%s ip=%s", feedback_id, body.type, client_ip)

    return {"success": True, "id": feedback_id}
