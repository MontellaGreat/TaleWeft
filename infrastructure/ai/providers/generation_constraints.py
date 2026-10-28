"""Pure offline constraints shared by saved profiles and request builders.

Only adapter guarantees and reviewed model capabilities belong here. Auto
selects Chat for explicit stops, but a possible future protocol fallback is
not evidence that every auto profile must satisfy Chat-only constraints.
"""
from __future__ import annotations

from copy import deepcopy
from typing import Any

from .capabilities import validate_reasoning_capability
from .token_limits import resolve_token_limits


_PROTECTED_EXTENSION_FIELDS = {
    'openai': {'messages', 'input', 'instructions', 'system', 'stream', 'response_format', 'text',
               'tools', 'tool_choice', 'functions', 'function_call', 'n'},
    'anthropic': {'messages', 'system', 'stream', 'input', 'instructions', 'response_format', 'tools', 'tool_choice'},
    'gemini': {'contents', 'systemInstruction', 'stream', 'input', 'messages', 'response_format', 'tools', 'toolConfig'},
}


def openai_endpoint(endpoint_kind: str, *, legacy_chat: bool = False, stop_sequences=None) -> str:
    if endpoint_kind == 'responses':
        if stop_sequences:
            raise ValueError('stop_sequences is unavailable for OpenAI Responses; select Chat or auto')
        return 'responses'
    return 'chat' if endpoint_kind == 'chat' or legacy_chat or stop_sequences else 'responses'


def extract_openai_reasoning_controls(extra_body: dict[str, Any], explicit_effort: Any):
    """Promote native effort declarations without interpreting vendor values."""
    cleaned = deepcopy(extra_body or {})
    declarations = []
    def declare(value):
        value = value.strip().lower() if isinstance(value, str) else value
        if value is not None:
            declarations.append(value)
    declare(explicit_effort)
    declare(cleaned.pop('reasoning_effort', None))
    nested = cleaned.get('reasoning')
    if isinstance(nested, dict) and 'effort' in nested:
        declare(nested.pop('effort'))
        if not nested:
            cleaned.pop('reasoning')
    if declarations and any(value != declarations[0] for value in declarations[1:]):
        raise ValueError('explicit reasoning_effort conflicts with extra_body reasoning control')
    return declarations[0] if declarations else None, cleaned


def anthropic_thinking(config, extra_body: dict[str, Any]) -> dict[str, Any]:
    budget = config.thinking_budget_tokens
    if budget is not None:
        thinking = {'type': 'disabled'} if budget == 0 else {'type': 'enabled', 'budget_tokens': budget}
    elif config.reasoning_effort == 'none':
        thinking = {'type': 'disabled'}
    else:
        thinking = None
    extension = extra_body.get('thinking')
    if thinking is not None and extension is not None and extension != thinking:
        raise ValueError('extra_body cannot override thinking')
    result = thinking if thinking is not None else extension
    if result is not None and not isinstance(result, dict):
        raise ValueError('Anthropic thinking must be an object')
    return result or {}


def validate_anthropic_thinking_budget(budget, max_tokens: int) -> None:
    if isinstance(budget, bool) or not isinstance(budget, int) or budget < 1024 or budget >= max_tokens:
        raise ValueError('Anthropic thinking budget must be at least 1024 and less than max_tokens')


def validate_generation_constraints(protocol: str, config, *, model: str, base_url: str | None,
                                    endpoint_kind: str = 'auto', legacy_chat: bool = False,
                                    extra_body=None, token_limit_settings=None) -> None:
    """Validate configuration alone, without clients, prompts, storage or IO."""
    extra = extra_body or {}
    limits = resolve_token_limits(protocol, model, token_limit_settings)
    if limits.max_output_tokens is not None and config.max_tokens > limits.max_output_tokens:
        raise ValueError(f'max_tokens={config.max_tokens} exceeds model output limit {limits.max_output_tokens}')
    validate_reasoning_capability(protocol, model, base_url, config.reasoning_effort, extra_body=extra)
    protected = _PROTECTED_EXTENSION_FIELDS.get(protocol, set()).intersection(extra)
    if protected:
        raise ValueError('extra_body cannot supply protected request field ' + ', '.join(sorted(protected)))
    if 'model' in extra and extra['model'] != model:
        raise ValueError('extra_body cannot override the resolved model')
    for parameter in ('max_tokens', 'max_output_tokens', 'max_completion_tokens'):
        if parameter in extra and extra[parameter] != config.max_tokens:
            raise ValueError(f'extra_body cannot override {parameter}')
    generation = extra.get('generationConfig')
    if isinstance(generation, dict) and generation.get('maxOutputTokens', config.max_tokens) != config.max_tokens:
        raise ValueError('extra_body cannot override maxOutputTokens')
    if protocol == 'openai':
        if config.thinking_budget_tokens is not None:
            raise ValueError('thinking_budget_tokens is not supported by OpenAI; use reasoning_effort')
        endpoint = openai_endpoint(endpoint_kind, legacy_chat=legacy_chat, stop_sequences=config.stop_sequences)
        effort, _ = extract_openai_reasoning_controls(extra, config.reasoning_effort)
        if effort is not None and 'temperature' in extra:
            raise ValueError('extra_body temperature cannot be combined with reasoning_effort')
        if 'temperature' in extra and extra['temperature'] != config.temperature:
            raise ValueError('extra_body cannot override temperature')
        if config.top_p is not None and effort not in (None, 'none'):
            raise ValueError('top_p is unavailable with enabled OpenAI reasoning')
        if endpoint == 'chat' and config.stop_sequences and len(config.stop_sequences) > 4:
            raise ValueError('OpenAI Chat supports at most 4 stop_sequences')
        for parameter in ('top_p', 'stop'):
            if parameter in extra:
                raise ValueError(f'extra_body {parameter} must use the explicit generation control')
    elif protocol == 'anthropic':
        if config.reasoning_effort not in (None, 'none'):
            raise ValueError('Anthropic Messages uses thinking_budget_tokens; reasoning_effort is not supported')
        thinking = anthropic_thinking(config, extra)
        if thinking.get('type') == 'enabled':
            validate_anthropic_thinking_budget(thinking.get('budget_tokens'), config.max_tokens)
            if config.top_p is not None:
                raise ValueError('top_p is unavailable with enabled Anthropic thinking')
            if 'temperature' in extra:
                raise ValueError('extra_body temperature cannot be combined with enabled thinking')
        if 'temperature' in extra and extra['temperature'] != config.temperature:
            raise ValueError('extra_body cannot override temperature')
        for parameter in ('top_p', 'stop_sequences'):
            if parameter in extra:
                raise ValueError(f'extra_body {parameter} must use the explicit generation control')
    elif protocol == 'gemini':
        if config.reasoning_effort not in (None, 'none'):
            raise ValueError('Gemini thinking uses thinking_budget_tokens; reasoning_effort level is unavailable for this adapter')
        if config.stop_sequences and len(config.stop_sequences) > 5:
            raise ValueError('Gemini supports at most 5 stop_sequences')
        if isinstance(generation, dict):
            for parameter in ('topP', 'stopSequences'):
                if parameter in generation:
                    raise ValueError(f'extra_body {parameter} must use the explicit generation control')
            if 'temperature' in generation and generation['temperature'] != config.temperature:
                raise ValueError('extra_body cannot override temperature')
            thinking_budget = config.thinking_budget_tokens
            if thinking_budget is None and config.reasoning_effort == 'none':
                thinking_budget = 0
            if (thinking_budget is not None and 'thinkingConfig' in generation
                    and generation['thinkingConfig'] != {'thinkingBudget': thinking_budget}):
                raise ValueError('extra_body cannot override thinkingConfig')
