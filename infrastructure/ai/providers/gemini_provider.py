"""Gemini LLM 提供商实现（官方 generateContent / streamGenerateContent 协议）"""
from __future__ import annotations

import json
import logging
from contextlib import aclosing
from typing import Any, AsyncIterator

import httpx

from domain.ai.services.llm_service import GenerationConfig, GenerationResult
from domain.ai.value_objects.prompt import Prompt
from domain.ai.value_objects.token_usage import TokenUsage
from application.ai.usage_context import parse_usage, record_stream_usage, record_response_usage
from infrastructure.ai.config.settings import Settings
from infrastructure.ai.http_timeout import build_httpx_timeout
from .base import BaseProvider
from .model_resolution import require_resolved_model_id
from infrastructure.ai.url_utils import normalize_gemini_model_id

logger = logging.getLogger(__name__)

DEFAULT_BASE_URL = 'https://generativelanguage.googleapis.com/v1beta'


class GeminiProvider(BaseProvider):
    def __init__(self, settings: Settings):
        super().__init__(settings)
        if not settings.api_key:
            raise ValueError('API key is required for GeminiProvider')
        self.base_url = (settings.base_url or DEFAULT_BASE_URL).rstrip('/')
        # 长生命周期 httpx client（跨请求复用连接池）
        self._http_client = httpx.AsyncClient(
            timeout=build_httpx_timeout(settings.http_timeout_settings),
            trust_env=False,
        )

    async def generate(self, prompt: Prompt, config: GenerationConfig) -> GenerationResult:
        model_id = require_resolved_model_id(
            config.model,
            self.settings.default_model,
            provider_label="Gemini",
        )
        payload = self._build_payload(prompt, config)
        receipt = dict(self.last_request_receipt)
        query = self._build_query()
        url = self._build_url(model_id, 'generateContent')

        response = await self._http_client.post(
            url,
            params=query,
            headers=self._build_headers(stream=False),
            json=payload,
        )
        response.raise_for_status()
        data = response.json()
        record_response_usage(parse_usage(data.get('usageMetadata'), 'promptTokenCount', 'candidatesTokenCount'))
        if not self._validate_finish(data):
            raise RuntimeError('Gemini completion is missing a normal finishReason')

        content = self._extract_text(data)
        if not content.strip():
            raise RuntimeError('Gemini returned empty content')

        usage = data.get('usageMetadata') or {}
        token_usage = parse_usage(usage, 'promptTokenCount', 'candidatesTokenCount')
        return self._result_with_receipt(GenerationResult(content=content, token_usage=token_usage), receipt)

    async def stream_generate(self, prompt: Prompt, config: GenerationConfig) -> AsyncIterator[str]:
        model_id = require_resolved_model_id(
            config.model,
            self.settings.default_model,
            provider_label="Gemini",
        )
        payload = self._build_payload(prompt, config, stream=True)
        query = self._build_query({'alt': 'sse'})
        url = self._build_url(model_id, 'streamGenerateContent')

        async with self._http_client.stream(
            'POST',
            url,
            params=query,
            headers=self._build_headers(stream=True),
            json=payload,
        ) as response:
            response.raise_for_status()
            buffer = ''
            completed = False
            async with aclosing(response.aiter_text()) as chunks:
                async for chunk in chunks:
                    buffer += chunk.replace('\r\n', '\n')
                    while '\n\n' in buffer:
                        event_text, buffer = buffer.split('\n\n', 1)
                        text = self._parse_sse_event(event_text)
                        completed = self._sse_event_completed(event_text) or completed
                        if text:
                            yield text
            if buffer.strip():
                text = self._parse_sse_event(buffer)
                completed = self._sse_event_completed(buffer) or completed
                if text:
                    yield text
            if not completed:
                raise RuntimeError('Gemini SSE ended before a normal finishReason')

    def _build_url(self, model: str, action: str) -> str:
        model_name = normalize_gemini_model_id(model)
        if not model_name:
            raise ValueError('Gemini: 构建请求 URL 时模型名为空')
        return f'{self.base_url}/models/{model_name}:{action}'

    def _build_query(self, extra: dict[str, Any] | None = None) -> dict[str, Any]:
        query: dict[str, Any] = {'key': self.settings.api_key}
        query.update(self.settings.extra_query or {})
        if extra:
            query.update(extra)
        return query

    def _build_headers(self, *, stream: bool) -> dict[str, str]:
        headers = {'Content-Type': 'application/json'}
        if stream:
            headers['Accept'] = 'text/event-stream'
        headers.update(self.settings.extra_headers or {})
        return headers

    def _build_payload(self, prompt: Prompt, config: GenerationConfig, *, stream: bool = False) -> dict[str, Any]:
        model_id = require_resolved_model_id(config.model, self.settings.default_model, provider_label='Gemini')
        self._validate_output_tokens(config, model_id, protocol='gemini')
        generation_config = {
            'temperature': config.temperature,
            'maxOutputTokens': config.max_tokens,
        }
        if config.reasoning_effort not in (None, "none"):
            raise ValueError(
                "Gemini thinking uses thinking_budget_tokens; reasoning_effort level is unavailable for this adapter"
            )
        if config.thinking_budget_tokens is not None:
            generation_config['thinkingConfig'] = {
                'thinkingBudget': config.thinking_budget_tokens,
            }
        elif config.reasoning_effort == 'none':
            generation_config['thinkingConfig'] = {'thinkingBudget': 0}
        if config.top_p is not None:
            generation_config['topP'] = config.top_p
        if config.stop_sequences:
            if len(config.stop_sequences) > 5:
                raise ValueError('Gemini supports at most 5 stop_sequences')
            generation_config['stopSequences'] = config.stop_sequences
        # 🔥 response_format 自适应：
        # Gemini 支持 responseMimeType 但不支持 json_schema 结构定义
        # OpenAI 的 json_object → Gemini 的 responseMimeType: application/json
        # OpenAI 的 json_schema → Gemini 的 responseMimeType + responseSchema
        if config.response_format:
            fmt = config.response_format
            if fmt.get("type") == "json_object":
                generation_config["responseMimeType"] = "application/json"
            elif fmt.get("type") == "json_schema":
                generation_config["responseMimeType"] = "application/json"
                # 如果 json_schema 中有 schema 定义，传递给 Gemini
                schema = fmt.get("json_schema", {}).get("schema")
                if schema is not None:
                    generation_config["responseSchema"] = schema

        payload: dict[str, Any] = {
            'contents': [
                {
                    'role': 'user',
                    'parts': [{'text': prompt.user}],
                }
            ],
            'generationConfig': generation_config,
        }
        if prompt.system.strip():
            payload['systemInstruction'] = {
                'parts': [{'text': prompt.system}],
            }
        extra_body = dict(self.settings.extra_body or {})
        generation_override = extra_body.pop('generationConfig', None)
        if isinstance(generation_override, dict):
            for key, value in generation_override.items():
                if key in {'topP', 'stopSequences'}:
                    raise ValueError(f'extra_body {key} must use the explicit generation control')
                if key in generation_config and value != generation_config[key]:
                    raise ValueError(f'extra_body cannot override {key}')
            payload['generationConfig'].update(generation_override)
        for key, value in extra_body.items():
            if key in {'contents', 'systemInstruction', 'stream', 'input', 'messages', 'response_format',
                       'tools', 'toolConfig'}:
                raise ValueError(f'extra_body cannot supply protected request field {key}')
            if key in payload and payload[key] != value:
                raise ValueError(f'extra_body cannot override {key}')
        payload.update(extra_body)
        self._validate_context_window(payload, config, model_id, protocol='gemini')
        self._record_request_receipt(payload, config, endpoint='gemini.stream_generate_content' if stream else 'gemini.generate_content')
        return payload

    @staticmethod
    def _validate_finish(payload: dict[str, Any]) -> bool:
        candidates = payload.get('candidates') or []
        reasons = [candidate.get('finishReason') for candidate in candidates]
        for reason in reasons:
            if reason and reason != 'STOP':
                raise RuntimeError(f'Gemini generation did not finish normally: {reason}')
        return bool(reasons) and all(reason == 'STOP' for reason in reasons)

    def _sse_event_completed(self, event_text: str) -> bool:
        data = '\n'.join(line[5:].strip() for line in event_text.splitlines() if line.startswith('data:'))
        try:
            payload = json.loads(data)
        except (ValueError, TypeError):
            return False
        items = payload if isinstance(payload, list) else [payload]
        return any([self._validate_finish(item) for item in items if isinstance(item, dict)])

    def _extract_text(self, data: dict[str, Any]) -> str:
        pieces: list[str] = []
        for candidate in data.get('candidates') or []:
            content = candidate.get('content') or {}
            for part in content.get('parts') or []:
                if part.get('thought') is True:
                    continue
                text = part.get('text')
                if text:
                    pieces.append(str(text))
        return ''.join(pieces)

    def _parse_sse_event(self, event_text: str) -> str:
        data_lines: list[str] = []
        for line in event_text.splitlines():
            if line.startswith('data:'):
                data_lines.append(line[5:].strip())

        if not data_lines:
            return ''

        raw_payload = ''.join(data_lines).strip()
        if not raw_payload or raw_payload == '[DONE]':
            return ''

        try:
            payload = json.loads(raw_payload)
        except json.JSONDecodeError:
            logger.debug('Gemini SSE parse skip: %d chars', len(raw_payload))
            return ''

        if isinstance(payload, list):
            for item in payload:
                if isinstance(item, dict):
                    record_stream_usage(item.get('usageMetadata'), 'promptTokenCount', 'candidatesTokenCount')
            return ''.join(self._extract_text(item) for item in payload if isinstance(item, dict))
        if isinstance(payload, dict):
            if payload.get('error'):
                raise RuntimeError('Gemini SSE reported an upstream error')
            record_stream_usage(payload.get('usageMetadata'), 'promptTokenCount', 'candidatesTokenCount')
            return self._extract_text(payload)
        return ''
