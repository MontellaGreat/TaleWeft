"""LLM retry policy shared by provider and structured-output pipelines.

The limits here intentionally stay small: callers may retry transient transport
or empty-response failures, but no path should loop indefinitely or spend user
tokens without a bounded budget.
"""

import math
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
import re

# 含首次调用在内，同一结构化/解析流程最多调用 LLM 的次数
LLM_MAX_TOTAL_ATTEMPTS = 3

_RETRYABLE_MARKERS = (
    "api returned empty content",
    "returned empty content",
    "empty non-stream content",
    "empty content",
    "empty response",
    "overloaded_error",
    "rate limit",
    "timeout",
    "temporar",
    "connection reset",
    "connection error",
    "service unavailable",
)

_RETRYABLE_STATUSES = (" 408", " 409", " 425", " 429", " 500", " 502", " 503", " 504", " 524", " 529")


def _error_chain(exc):
    seen = set()
    while isinstance(exc, BaseException) and id(exc) not in seen:
        seen.add(id(exc))
        yield exc
        exc = exc.__cause__ or exc.__context__


def _error_body(exc):
    body = getattr(exc, 'body', None)
    if isinstance(body, dict) and isinstance(body.get('error'), dict):
        return body['error']
    return body if isinstance(body, dict) else {}


def llm_error_status(exc: Exception | str) -> int | None:
    for error in _error_chain(exc):
        status = getattr(error, 'status_code', None)
        if status is None:
            status = getattr(getattr(error, 'response', None), 'status_code', None)
        if type(status) is int:
            return status
    match = re.search(r'(?:error\s+code|http(?:\s+status)?|status(?:\s+code)?)\s*[:=]?\s*(\d{3})\b', str(exc), re.I)
    return int(match[1]) if match else None


def llm_retry_delay_seconds(exc: Exception | str, attempt: int = 0) -> float:
    """Honor server cooldowns, including gateway JSON; caller bounds auto-wait.

    Mirrors OpenAI SDK Retry-After seconds/date/ms support. Larger delays are
    returned intact so callers can stop rather than retry before allowed.
    """
    delays = []
    for error in _error_chain(exc):
        headers = getattr(getattr(error, 'response', None), 'headers', {})
        values = [(_error_body(error).get('retry_after'), 1), (headers.get('retry-after-ms'), .001),
                  (headers.get('retry-after'), 1)]
        for value, scale in values:
            if value is None or isinstance(value, bool):
                continue
            try:
                delay = float(value) * scale
            except (TypeError, ValueError, OverflowError):
                try:
                    stamp = parsedate_to_datetime(str(value))
                    stamp = stamp if stamp.tzinfo else stamp.replace(tzinfo=timezone.utc)
                    delay = (stamp - datetime.now(timezone.utc)).total_seconds()
                except (TypeError, ValueError, OverflowError):
                    continue
            if math.isfinite(delay) and delay > 0:
                delays.append(delay)
    return max(delays) if delays else 120.0 if llm_error_status(exc) == 524 else min(1.5 * (2 ** min(max(attempt, 0), 10)), 8.0)


def is_retryable_llm_error(exc: Exception | str) -> bool:
    """Return True for transient LLM/provider failures worth retrying.

    This deliberately includes empty upstream content. Several OpenAI-compatible
    gateways occasionally return a syntactically successful response with no
    text; treating that as retryable keeps module-specific callers from each
    inventing their own empty-response handling.
    """
    for error in _error_chain(exc):
        headers = getattr(getattr(error, 'response', None), 'headers', {})
        if headers.get('x-should-retry') == 'false' or _error_body(error).get('retryable') is False:
            return False
    status = llm_error_status(exc)
    if status is not None:
        return status in (408, 409, 425, 429) or 500 <= status < 600
    message = str(exc).lower()
    return any(marker in message for marker in _RETRYABLE_MARKERS) or any(
        status in message for status in _RETRYABLE_STATUSES
    )
