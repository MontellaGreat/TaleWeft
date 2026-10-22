"""Explicit model capability limits; unknown model IDs never inherit guessed caps."""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class ModelTokenLimits:
    max_output_tokens: int | None = None
    context_window: int | None = None
    source: str = 'unknown'


# Stable documented model snapshots. New/proxy aliases require an explicit
# Settings override instead of substring-matching a possibly unrelated model.
_KNOWN_LIMITS = {
    ('openai', 'gpt-4o'): (16384, 128000),
    ('openai', 'gpt-4o-2024-05-13'): (4096, 128000),
    ('openai', 'gpt-4o-2024-08-06'): (16384, 128000),
    ('openai', 'gpt-4o-2024-11-20'): (16384, 128000),
    ('openai', 'gpt-4o-mini'): (16384, 128000),
    ('openai', 'gpt-4o-mini-2024-07-18'): (16384, 128000),
    ('anthropic', 'claude-3-opus-20240229'): (4096, 200000),
    ('anthropic', 'claude-3-5-sonnet-20241022'): (8192, 200000),
    ('gemini', 'gemini-2.0-flash'): (8192, 1048576),
}


def resolve_token_limits(protocol: str, model: str, settings=None) -> ModelTokenLimits:
    known = _KNOWN_LIMITS.get((protocol, model.strip()))
    output, window = known if known else (None, None)
    source = 'documented_model_snapshot' if known else 'unknown'
    configured_output = getattr(settings, 'max_output_tokens_limit', None)
    configured_window = getattr(settings, 'context_window_limit', None)
    if configured_output is not None:
        output = min(output, configured_output) if output else configured_output
        source = 'configured' if not known else 'configured_and_documented'
    if configured_window is not None:
        window = min(window, configured_window) if window else configured_window
        source = 'configured' if not known else 'configured_and_documented'
    return ModelTokenLimits(output, window, source)
