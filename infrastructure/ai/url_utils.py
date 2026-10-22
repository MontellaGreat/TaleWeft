from __future__ import annotations

from typing import Optional
from urllib.parse import urlparse, urlunparse


def _strip_known_suffix(url: str, suffixes: tuple[str, ...]) -> str:
    normalized = (url or '').strip().rstrip('/')
    lower = normalized.lower()
    for suffix in suffixes:
        if lower.endswith(suffix):
            return normalized[: -len(suffix)].rstrip('/')
    return normalized


def normalize_openai_base_url(url: Optional[str]) -> Optional[str]:
    if not url or not str(url).strip():
        return None
    return _strip_known_suffix(
        str(url),
        (
            '/chat/completions',
            '/v1/chat/completions',
            '/completions',
        ),
    )


def normalize_anthropic_base_url(url: Optional[str]) -> Optional[str]:
    if not url or not str(url).strip():
        return None
    normalized = str(url).strip().rstrip('/')
    parsed = urlparse(normalized)
    path = (parsed.path or '').rstrip('/')
    # Endpoints append /v1/messages or /v1/models. Strip that API suffix
    # (including /v1 by itself) while retaining any gateway prefix such as
    # /api or /anthropic.
    if path.endswith('/v1/messages'):
        path = path[:-len('/v1/messages')]
    elif path.endswith('/messages'):
        path = path[:-len('/messages')]
    elif path.endswith('/v1'):
        path = path[:-len('/v1')]
    return urlunparse((parsed.scheme, parsed.netloc, path, '', parsed.query, parsed.fragment)).rstrip('/')


def normalize_gemini_base_url(url: Optional[str]) -> Optional[str]:
    if not url or not str(url).strip():
        return None
    return _strip_known_suffix(
        str(url),
        (
            '/models',
            '/v1beta/models',
            '/v1/models',
        ),
    )


def normalize_gemini_model_id(model: str) -> str:
    """Accept both Gemini's catalog name and the provider-facing model ID."""
    return (model or '').strip().removeprefix('models/')


def anthropic_models_url(base_url: Optional[str]) -> str:
    """Build the model catalog endpoint without duplicating the API version."""
    base = normalize_anthropic_base_url(base_url) or 'https://api.anthropic.com'
    return f'{base}/v1/models'


def anthropic_messages_url(base_url: Optional[str]) -> str:
    """Build the raw messages endpoint while preserving a gateway prefix."""
    base = normalize_anthropic_base_url(base_url) or 'https://api.anthropic.com'
    return f'{base}/v1/messages'
