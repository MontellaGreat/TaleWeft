"""Capabilities of the shipped text adapters, with explicit limits on evidence.

These entries describe parameter mappings. A reachable model directory does not
prove that a particular gateway accepts a reasoning option.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any
from urllib.parse import urlparse


_SUPPORTED_PROTOCOLS = ('openai', 'anthropic', 'gemini')

# The adapter can only make a hard claim for model IDs that have been reviewed
# against the protocol contract.  Keep this allow-list deliberately small: an
# alias, a future dated snapshot, or a provider-specific variant must remain
# ``model_dependent`` until it has been probed.  In particular, do not use a
# prefix such as ``gpt-5`` or ``o3`` as a rejection rule; that would turn a
# newly released model into a false negative.
_OPENAI_REASONING_MODELS = frozenset({
    'o1', 'o1-mini', 'o1-preview',
    'o3', 'o3-mini', 'o3-mini-2025-01-31',
    'o4-mini', 'o4-mini-2025-04-16',
})
_OPENAI_GPT5_REASONING_MODELS = frozenset({
    'gpt-5', 'gpt-5-mini', 'gpt-5-nano',
})
_OPENAI_NON_REASONING_MODELS = frozenset({
    'gpt-3.5-turbo', 'gpt-3.5-turbo-0125',
    'gpt-4o', 'gpt-4o-mini', 'gpt-4o-2024-05-13', 'gpt-4o-2024-08-06',
    'gpt-4o-2024-11-20', 'gpt-4o-mini-2024-07-18',
    'gpt-4.1', 'gpt-4.1-mini', 'gpt-4.1-nano',
    'gpt-4-turbo', 'gpt-4-turbo-2024-04-09', 'chatgpt-4o-latest',
})
_REASONING_EFFORTS = frozenset({'none', 'minimal', 'low', 'medium', 'high', 'xhigh', 'max'})


def registered_protocols() -> tuple[str, ...]:
    """Return protocol IDs with a shipped adapter.

    Keeping this registry next to the capability matrix prevents the control
    panel from advertising a protocol that the factory cannot instantiate.
    The tuple is intentionally immutable so callers cannot mutate process-wide
    adapter state.
    """
    return _SUPPORTED_PROTOCOLS


def _feature_matrix(features: dict[str, str], mappings: dict[str, str]) -> dict[str, dict[str, str | None]]:
    """Expose status and payload location without removing the legacy map."""
    return {
        name: {'status': status, 'parameter': mappings.get(name)}
        for name, status in features.items()
    }


def _openai_reasoning_capability(model: str, base_url: str | None) -> tuple[str, list[str], str]:
    """Classify reasoning support without treating the whole OpenAI catalog alike."""
    normalized = (model or '').strip().lower()
    raw_base_url = (base_url or '').strip()
    parsed_host = urlparse(raw_base_url).hostname if raw_base_url else 'api.openai.com'
    # An explicitly supplied URL without a parseable host is safer to treat as
    # a custom/unknown gateway than to silently apply the official catalog.
    host = (parsed_host or '').lower()
    if host != 'api.openai.com':
        return 'model_dependent', [], 'compatible_gateway_requires_probe'

    if normalized in _OPENAI_REASONING_MODELS:
        return 'configurable', ['low', 'medium', 'high'], 'official_model_family_inference'
    if normalized in _OPENAI_GPT5_REASONING_MODELS:
        return 'configurable', ['minimal', 'low', 'medium', 'high'], 'official_model_family_inference'
    if normalized in _OPENAI_NON_REASONING_MODELS:
        return 'unavailable', [], 'official_non_reasoning_family_inference'
    return 'model_dependent', [], 'unknown_model_requires_probe'


def reasoning_capability(protocol: str, model: str, base_url: str | None = None) -> tuple[str, list[str], str]:
    """Return the reviewed reasoning contract for a provider/model pair.

    ``model_dependent`` is intentionally permissive.  A custom OpenAI
    compatible gateway can expose a model with an ID that happens to look like
    an official model, so only the official host plus an exact reviewed ID is
    safe to reject or constrain.
    """
    if protocol == 'openai':
        return _openai_reasoning_capability(model, base_url)
    if protocol in {'anthropic', 'gemini'}:
        return 'unavailable', [], f'{protocol}_uses_thinking_budget'
    return 'model_dependent', [], 'unknown_protocol_requires_probe'


def _reasoning_efforts_from_payload(payload: Any) -> list[Any]:
    """Read only protocol reasoning locations from an extension payload.

    Do not recursively inspect arbitrary ``effort`` keys: tools and vendor
    extensions may use that word for an unrelated setting.  These are the
    locations which the shipped OpenAI adapters serialize as reasoning.
    """
    if not isinstance(payload, dict):
        return []
    values: list[Any] = []
    if 'reasoning_effort' in payload:
        values.append(payload.get('reasoning_effort'))
    reasoning = payload.get('reasoning')
    if isinstance(reasoning, dict) and 'effort' in reasoning:
        values.append(reasoning.get('effort'))
    generation = payload.get('generationConfig')
    if isinstance(generation, dict):
        nested = generation.get('reasoning') or generation.get('reasoningConfig')
        if isinstance(nested, dict) and 'effort' in nested:
            values.append(nested.get('effort'))
    nested_extra = payload.get('extra_body')
    if isinstance(nested_extra, dict):
        values.extend(_reasoning_efforts_from_payload(nested_extra))
    return values


def validate_reasoning_capability(
    protocol: str,
    model: str,
    base_url: str | None,
    reasoning_effort: str | None = None,
    *,
    payload: Any = None,
    extra_body: Any = None,
) -> None:
    """Reject only a *known* unsupported reasoning combination.

    This is shared by profile validation and provider request construction, so
    an invalid saved profile cannot reach HTTP while unknown/custom gateways
    remain usable.  ``extra_body`` and the final payload are inspected too;
    otherwise a nested vendor field could bypass the control-panel setting.
    """
    # Anthropic/Gemini use a token budget rather than OpenAI's categorical
    # effort field; their adapters retain the protocol-specific validation.
    if protocol != 'openai':
        return
    effort_values: list[Any] = []
    if reasoning_effort is not None:
        effort_values.append(reasoning_effort)
    effort_values.extend(_reasoning_efforts_from_payload(extra_body))
    effort_values.extend(_reasoning_efforts_from_payload(payload))
    normalized: list[str] = []
    unknown_values: list[Any] = []
    for value in effort_values:
        if isinstance(value, str):
            value = value.strip().lower()
        # ``none`` is an explicit protocol value, distinct from omitting the
        # field (which lets the provider choose its default).
        if value is None:
            continue
        if isinstance(value, str) and value in _REASONING_EFFORTS:
            normalized.append(value)
        else:
            # Extension payloads are user/vendor supplied.  Keep malformed or
            # vendor-specific values as an opaque marker (rather than hashing
            # an unhashable dict); unknown gateways may still consume them.
            unknown_values.append(value)
    if not normalized and not unknown_values:
        return
    if len(set(normalized)) > 1 or (normalized and unknown_values):
        raise ValueError('explicit reasoning_effort conflicts with extra_body reasoning control')
    status, allowed, basis = reasoning_capability(protocol, model, base_url)
    if status == 'unavailable':
        raise ValueError(
            f'模型 {model or "(未填写)"} 不支持 reasoning_effort；请清空思考程度或更换支持推理的模型'
        )
    if status == 'configurable':
        invalid = sorted({value for value in normalized if value not in allowed})
        if unknown_values:
            invalid.append('<非标准值>')
        if invalid:
            raise ValueError(
                f'模型 {model} 不支持 reasoning_effort={", ".join(invalid)}；可用值：{", ".join(allowed)}'
            )


def adapter_capabilities(protocol: str, model: str, endpoint_kind: str = 'auto', *, base_url: str | None = None) -> dict[str, Any]:
    observed = datetime.now(timezone.utc)
    observed_at = observed.isoformat()
    freshness = {'observed_at': observed_at, 'expires_at': (observed + timedelta(hours=24)).isoformat(),
                 'capability_source': 'adapter_parameter_mapping'}
    common = {'text': 'supported', 'stream': 'supported', 'tools': 'unavailable',
              'vision': 'unavailable', 'audio': 'unavailable'}
    if protocol == 'openai':
        if endpoint_kind not in {'auto', 'chat', 'responses'}:
            raise ValueError('OpenAI endpoint_kind must be auto, chat, or responses')
        endpoint = 'chat' if endpoint_kind == 'chat' else 'responses'
        reasoning_status, reasoning_efforts, reasoning_basis = _openai_reasoning_capability(model, base_url)
        features = {**common, 'reasoning_effort': 'configurable', 'thinking_budget_tokens': 'unavailable',
                    'json_object': 'configurable', 'json_schema': 'configurable', 'top_p': 'configurable',
                    'stop_sequences': 'unavailable' if endpoint_kind == 'responses' else 'configurable'}
        mappings = ({'reasoning_effort': 'reasoning.effort', 'max_tokens': 'max_output_tokens',
                     'top_p': 'top_p', 'json_object': 'text.format', 'json_schema': 'text.format'}
                    if endpoint == 'responses' else
                    {'reasoning_effort': 'reasoning_effort', 'max_tokens': 'max_completion_tokens|max_tokens',
                     'top_p': 'top_p', 'json_object': 'response_format', 'json_schema': 'response_format',
                     'stop_sequences': 'stop'})
        if endpoint_kind == 'auto':
            mappings['stop_sequences'] = 'stop'
        # Auto uses Responses when no stop sequence is requested and Chat when
        # stop sequences are present.  The provider makes this choice at call
        # time; exposing it here prevents the UI from treating auto as a
        # single endpoint with contradictory capabilities.
        selection = {'requested': endpoint_kind, 'default': 'responses' if endpoint_kind == 'auto' else endpoint,
                     'with_stop_sequences': ('chat' if endpoint_kind == 'auto' else
                                             'rejected' if endpoint_kind == 'responses' else 'chat')}
        # The payload mapping is available at the adapter level, while model
        # acceptance still depends on the selected model family.  Do not let a
        # generic/custom model appear to support every reasoning level.
        features['reasoning_effort'] = reasoning_status
        result = {'protocol_id': f'openai.{endpoint}', 'endpoint_kind': endpoint,
                'source': 'adapter_parameter_mapping', 'confidence': 'endpoint_acceptance_required',
                **freshness, 'model': model,
                'requested_endpoint_kind': endpoint_kind,
                'endpoint_selection': selection,
                'features': features,
                'reasoning_efforts': reasoning_efforts,
                'reasoning_capability_basis': reasoning_basis,
                'thinking_budget': None,
                'parameter_mapping': mappings,
                'constraints': ['思考程度取值由具体模型决定；不支持时调用报错。',
                                '启用推理时省略 temperature，显式 top_p 会报冲突；实际协议以调用回执为准。',
                                'Chat 最多 4 个停止序列；Responses 不支持停止序列；auto 配置停止序列时选择 Chat。']}
        result['feature_matrix'] = _feature_matrix(features, mappings)
        if endpoint_kind == 'auto':
            result['feature_matrix']['stop_sequences'].update({
                'endpoint': 'openai.chat',
                'condition': '自动端点模式仅在存在停止序列时选择 Chat Completions',
            })
        return result
    if protocol in {'anthropic', 'gemini'}:
        allowed_endpoints = {'auto', 'messages'} if protocol == 'anthropic' else {'auto', 'generate_content'}
        if endpoint_kind not in allowed_endpoints:
            raise ValueError(f'{protocol} endpoint_kind is not supported: {endpoint_kind}')
        endpoint = 'messages' if protocol == 'anthropic' else 'generate_content'
        features = {**common, 'reasoning_effort': 'unavailable', 'thinking_budget_tokens': 'configurable',
                    'json_object': 'configurable', 'json_schema': 'configurable' if protocol == 'gemini' else 'unavailable',
                    'top_p': 'configurable', 'stop_sequences': 'configurable'}
        mappings = ({'thinking_budget_tokens': 'thinking.budget_tokens', 'max_tokens': 'max_tokens',
                     'top_p': 'top_p', 'stop_sequences': 'stop_sequences', 'json_object': 'prompt_instruction'}
                    if protocol == 'anthropic' else
                    {'thinking_budget_tokens': 'generationConfig.thinkingConfig.thinkingBudget',
                     'max_tokens': 'generationConfig.maxOutputTokens', 'top_p': 'generationConfig.topP',
                     'stop_sequences': 'generationConfig.stopSequences', 'json_object': 'generationConfig.responseMimeType',
                     'json_schema': 'generationConfig.responseSchema'})
        result = {'protocol_id': f'{protocol}.{endpoint}',
                'endpoint_kind': 'messages' if protocol == 'anthropic' else 'generate_content',
                'source': 'adapter_parameter_mapping', 'confidence': 'endpoint_acceptance_required',
                **freshness, 'model': model,
                'requested_endpoint_kind': endpoint_kind,
                'endpoint_selection': {'requested': endpoint_kind, 'default': endpoint},
                'features': features,
                'reasoning_efforts': [],
                'thinking_budget': {'off': 0, 'minimum_enabled': 1024 if protocol == 'anthropic' else 1,
                                    'maximum': 'less_than_max_tokens' if protocol == 'anthropic' else 'model_specific'},
                'parameter_mapping': mappings,
                'constraints': ['思考预算需模型支持；不支持时调用报错。',
                                ('启用 thinking 时不能显式设置 top_p；停止序列最多 16 项。' if protocol == 'anthropic'
                                 else 'thinking 可与 top_p 共存；停止序列最多 5 项。'),
                                ('JSON 通过提示词约束，未提供原生 schema 保证。' if protocol == 'anthropic'
                                 else 'JSON schema 由 responseSchema 传给模型，支持范围需模型确认。'),
                                '高级工具、多模态不在当前正文适配器范围内。']}
        result['feature_matrix'] = _feature_matrix(features, mappings)
        return result
    features = {key: 'unavailable' for key in common}
    return {'protocol_id': protocol, 'model': model, 'requested_endpoint_kind': endpoint_kind,
            'features': features, 'feature_matrix': _feature_matrix(features, {}),
            'reasoning_efforts': [], 'thinking_budget': None, 'source': 'unregistered',
            'confidence': 'unknown', 'observed_at': observed_at, 'constraints': ['协议未注册。']}
