"""Per-call usage collection; never stores text or credentials."""
from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from typing import Any

from domain.ai.value_objects.token_usage import TokenUsage


@dataclass
class UsageCapture:
    novel_id: str = ''
    attempt_id: str = ''
    token_usage: TokenUsage | None = None
    metadata: dict[str, Any] = field(default_factory=dict)
    input_tokens: int | None = None
    output_tokens: int | None = None
    responses: list[TokenUsage | None] = field(default_factory=list)


_CURRENT: ContextVar[UsageCapture | None] = ContextVar('plotpilot_usage_capture', default=None)


def current_usage() -> UsageCapture | None:
    return _CURRENT.get()


@contextmanager
def capture_usage(*, novel_id: str = '', attempt_id: str = ''):
    capture = UsageCapture(novel_id=novel_id, attempt_id=attempt_id)
    token = _CURRENT.set(capture)
    try:
        yield capture
    finally:
        _CURRENT.reset(token)


def parse_usage(usage, input_name='input_tokens', output_name='output_tokens') -> TokenUsage | None:
    def value(name):
        return usage.get(name) if isinstance(usage, dict) else getattr(usage, name, None)
    counts = (value(input_name), value(output_name))
    if any(isinstance(n, bool) or not isinstance(n, int) or n < 0 for n in counts):
        return None
    return TokenUsage(*counts)


def record_stream_usage(usage, input_name='input_tokens', output_name='output_tokens') -> None:
    capture = current_usage()
    if capture is None:
        return
    for source, target in ((input_name, 'input_tokens'), (output_name, 'output_tokens')):
        value = usage.get(source) if isinstance(usage, dict) else getattr(usage, source, None)
        if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
            setattr(capture, target, value)
    capture.token_usage = parse_usage({'input_tokens': capture.input_tokens, 'output_tokens': capture.output_tokens})


def record_response_usage(usage: TokenUsage | None) -> None:
    capture = current_usage()
    if capture is not None:
        capture.responses.append(usage)


def response_usage(capture: UsageCapture, fallback=None):
    if not capture.responses:
        return fallback
    if any(usage is None for usage in capture.responses):
        return None
    return sum(capture.responses, TokenUsage(0, 0))
