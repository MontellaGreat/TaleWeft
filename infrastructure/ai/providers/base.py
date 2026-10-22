"""LLM 提供商基类"""
import logging
import json
import hashlib
from abc import ABC
from domain.ai.services.llm_service import LLMService
from infrastructure.ai.config.settings import Settings
from infrastructure.ai.providers.token_limits import resolve_token_limits
from infrastructure.ai.providers.capabilities import validate_reasoning_capability
from domain.ai.services.token_budget import conservative_token_count
from application.ai.usage_context import current_usage

logger = logging.getLogger(__name__)


class BaseProvider(LLMService, ABC):
    """LLM 提供商基类

    所有 LLM 提供商的抽象基类，继承自 LLMService 接口。
    提供 aclose() 清理接口用于关闭 HTTP 连接池。
    """

    def __init__(self, settings: Settings):
        """初始化提供商

        Args:
            settings: AI 配置设置
        """
        self.settings = settings

    def _record_request_receipt(self, payload, config, *, endpoint: str, degraded: bool = False, **details):
        """Record safe, effective protocol parameters rather than intended defaults."""
        effective = dict(payload)
        effective.update(payload.get('extra_body') or {})
        generation = effective.get('generationConfig') or {}
        thinking = effective.get('thinking') or generation.get('thinkingConfig') or {}
        reasoning = effective.get('reasoning') or {}
        response_format = effective.get('response_format') or (effective.get('text') or {}).get('format') or {}
        schema = (response_format.get('json_schema') or {}).get('schema', response_format.get('schema'))
        format_type = response_format.get('type', 'text')
        mode = 'native'
        if generation.get('responseMimeType') == 'application/json':
            schema = generation.get('responseSchema')
            format_type = 'json_schema' if schema is not None else 'json_object'
        if details.get('structured_output') == 'prompt_json_instruction':
            format_type = (config.response_format or {}).get('type', 'text')
            schema = ((config.response_format or {}).get('json_schema') or {}).get('schema')
            mode = 'prompt_json_instruction'
        receipt = {
            'protocol': self.settings.protocol or endpoint.split('.')[0],
            'endpoint': endpoint,
            'model': config.model or self.settings.default_model,
            'max_tokens': effective.get('max_output_tokens', effective.get('max_completion_tokens', effective.get('max_tokens', generation.get('maxOutputTokens')))),
            'reasoning_effort': effective.get('reasoning_effort', reasoning.get('effort')),
            'requested_reasoning_effort': getattr(config, 'reasoning_effort', None),
            'thinking_budget_tokens': thinking.get('budget_tokens', thinking.get('thinkingBudget')),
            'thinking_type': thinking.get('type'),
            'temperature': effective.get('temperature', generation.get('temperature')),
            'temperature_omitted': 'temperature' not in effective and 'temperature' not in generation,
            'top_p': effective.get('top_p', generation.get('topP')),
            'stop_sequences': effective.get('stop', effective.get('stop_sequences', generation.get('stopSequences'))),
            'response_format_type': format_type,
            'response_schema_hash': hashlib.sha256(json.dumps(schema, sort_keys=True, ensure_ascii=False, separators=(',', ':')).encode()).hexdigest() if schema is not None else None,
            'structured_output_mode': mode,
            'degraded': degraded,
            **details,
        }
        self.last_request_receipt = receipt
        capture = current_usage()
        if capture is not None:
            capture.metadata['request_receipt'] = dict(receipt)
        return receipt

    def _result_with_receipt(self, result, receipt=None):
        result.request_receipt = dict(receipt if receipt is not None else getattr(self, 'last_request_receipt', {}))
        return result

    def _validate_output_tokens(self, config, model_id: str, *, protocol: str):
        limits = resolve_token_limits(protocol, model_id, self.settings)
        if limits.max_output_tokens is not None and config.max_tokens > limits.max_output_tokens:
            raise ValueError(f'max_tokens={config.max_tokens} exceeds model output limit {limits.max_output_tokens}')
        # Gateway extension parameters must not replace the validated model or
        # output budget behind the caller/trace's back.
        extra = self.settings.extra_body or {}
        if 'model' in extra and extra['model'] != model_id:
            raise ValueError('extra_body cannot override the resolved model')
        for name in ('max_tokens', 'max_output_tokens', 'max_completion_tokens'):
            if name in extra and extra[name] != config.max_tokens:
                raise ValueError(f'extra_body cannot override {name}')
        generation = extra.get('generationConfig')
        if isinstance(generation, dict) and generation.get('maxOutputTokens', config.max_tokens) != config.max_tokens:
            raise ValueError('extra_body cannot override maxOutputTokens')
        self._validate_reasoning_capability({}, config, model_id, protocol=protocol)
        return limits

    def _validate_context_window(self, payload, config, model_id: str, *, protocol: str):
        limits = self._validate_output_tokens(config, model_id, protocol=protocol)
        self._validate_reasoning_capability(payload, config, model_id, protocol=protocol)
        if limits.context_window is not None:
            # Include final serialization and framing, using a conservative
            # upper bound when no matching provider tokenizer is installed.
            prompt_bound = conservative_token_count(json.dumps(payload, ensure_ascii=False, default=str)) + 32
            if prompt_bound + config.max_tokens > limits.context_window:
                raise ValueError('Serialized input and requested output exceed the model context window')
        return limits

    def _validate_reasoning_capability(self, payload, config, model_id: str, *, protocol: str):
        """Validate explicit and extension reasoning controls before HTTP.

        OpenAI-compatible gateways accept arbitrary ``extra_body`` fields.  A
        known non-reasoning model must not be able to bypass the profile gate
        by placing ``reasoning_effort`` or ``reasoning.effort`` there, while an
        unknown/custom model remains pass-through.  The shared helper also
        inspects the final serialized payload so this check is kept next to
        the actual request construction.
        """
        validate_reasoning_capability(
            protocol,
            model_id,
            self.settings.base_url,
            getattr(config, 'reasoning_effort', None),
            payload=payload,
            extra_body=self.settings.extra_body,
        )

    async def aclose(self) -> None:
        """关闭 Provider 持有的 HTTP 连接池，释放系统资源（TCP 连接、TLS 上下文等）。

        子类可覆写此方法以关闭各自的 httpx client。
        默认实现自动检测并关闭常见的 client 属性。
        """
        closed_any = False
        # 关闭同步 httpx.Client
        for attr in ('_http_client_sync',):
            client = getattr(self, attr, None)
            if client is not None and hasattr(client, 'is_closed') and not client.is_closed:
                try:
                    client.close()
                    closed_any = True
                except Exception:
                    pass
        # 关闭异步 httpx.AsyncClient
        for attr in ('_http_client_async', '_http_client', '_stream_http_client'):
            client = getattr(self, attr, None)
            if client is not None and hasattr(client, 'aclose'):
                try:
                    await client.aclose()
                    closed_any = True
                except Exception:
                    pass
        if closed_any:
            logger.debug("Provider HTTP 连接池已关闭: %s", self.__class__.__name__)
