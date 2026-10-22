"""Persisted trace policy.

Trace records have two different content contracts:

* ``*_full`` / ``content_text`` are payload fields and require an explicit
  project opt-in (``capture_fulltext=True``).
* ``*_preview`` fields are bounded diagnostic previews.  They remain available
  by default so a timeline can still explain what happened, while secrets and
  fields marked as sensitive are removed before persistence.

Keeping the two contracts separate is important.  Dropping previews together
with full payloads makes the trace list technically present but useless for
diagnosis, while treating a caller supplied preview as trusted could leak a
whole chapter when a caller forgets to truncate it.
"""
from __future__ import annotations

import os
from typing import Any

from pydantic import BaseModel, Field


class TracePrivacySettings(BaseModel):
    enabled: bool = True
    capture_fulltext: bool = False
    retention_days: int = Field(default=14, ge=1, le=365)
    max_spans: int = Field(default=10000, ge=100, le=100000)


def default_trace_settings() -> TracePrivacySettings:
    disabled = {'0', 'false', 'off', 'no'}
    return TracePrivacySettings(
        enabled=os.getenv('AI_TRACE_ENABLED', 'true').strip().lower() not in disabled,
        capture_fulltext=os.getenv('AI_TRACE_FULLTEXT', 'false').strip().lower() in {'1', 'true', 'on', 'yes'},
    )


_SECRET_KEYS = {'api_key', 'apikey', 'authorization', 'proxy_authorization', 'x_api_key', 'key', 'token',
                'access_token', 'refresh_token', 'client_secret', 'password', 'secret'}
_CONTENT_KEYS = {'content', 'text', 'prompt', 'response', 'variables', 'user', 'system',
                 'messages', 'chapter_content', 'raw_response', 'raw_output', 'body'}

_FULLTEXT_FIELDS = {
    'variables_full',
    'prompt_full',
    'response_full',
    'content_text',
}

_PREVIEW_FIELDS = {
    'variables_preview',
    'prompt_preview',
    'response_preview',
    'content_preview',
}


def _redact_preview_content(value: Any) -> Any:
    """Remove sensitive preview values before a depth limit can stringify them."""
    if isinstance(value, dict):
        return {
            key: ('[已脱敏]' if str(key).lower().replace('-', '_') in
                  {'chapter_content', 'user_private_notes'} else _redact_preview_content(item))
            for key, item in value.items()
        }
    if isinstance(value, (list, tuple)):
        return [_redact_preview_content(item) for item in value]
    return value


def _safe_preview(value: Any) -> Any:
    """Return a bounded preview while preserving diagnostic context.

    ``preview_value`` is shared with trace producers and already understands
    nested values, list limits, and sensitive field names such as
    ``chapter_content``.  Run the persistence-level secret redaction as a
    second boundary so callers that construct previews manually cannot bypass
    the broader ``key``/``authorization`` rules used here.
    """
    # Redact recursively before collapsing deep structures into a string.  If
    # this happened after ``preview_value`` only, a secret nested below its
    # depth limit could be embedded in that string and escape key-based
    # redaction.
    value = _redact_preview_content(redact_metadata(value, True))
    try:
        from application.ai.trace_context import preview_value

        value = preview_value(value)
    except Exception:
        # Never persist an unbounded caller-supplied preview if the formatter
        # is unavailable during early startup or migration.
        return '[preview unavailable]'
    return redact_metadata(value, True)


def redact_metadata(value: Any, fulltext: bool = False) -> Any:
    if isinstance(value, dict):
        result = {}
        for key, item in value.items():
            normalized = str(key).lower().replace('-', '_')
            if normalized in _SECRET_KEYS:
                result[key] = '[redacted]'
            elif not fulltext and normalized in _CONTENT_KEYS:
                result[key] = '[content omitted]'
            else:
                result[key] = redact_metadata(item, fulltext)
        return result
    if isinstance(value, (list, tuple, set)):
        return [redact_metadata(item, fulltext) for item in value]
    return value


def apply_trace_privacy(record: dict, settings: TracePrivacySettings) -> dict:
    result = dict(record)
    # Full payloads are opt-in.  Preserve preview fields in either mode, but
    # enforce the bounded/sanitized preview contract at the persistence edge.
    for field in _FULLTEXT_FIELDS:
        if not settings.capture_fulltext:
            result[field] = None
        elif field in result:
            result[field] = redact_metadata(result[field], True)
    for field in _PREVIEW_FIELDS:
        if field in result:
            result[field] = _safe_preview(result[field])
    result['metadata'] = redact_metadata(result.get('metadata') or {}, settings.capture_fulltext)
    # Provider exceptions can contain credentials, URLs, prompts, or upstream bodies.
    if result.get('error'):
        result['error'] = 'AI call failed (upstream details omitted)'
    return result
