from __future__ import annotations

import logging
import time
import json
import asyncio
import hashlib
from contextlib import nullcontext
from typing import AsyncIterator, Optional

from application.ai.llm_control_service import LLMControlService, LLMProfile
from application.ai.trace_context import (
    content_hash,
    ensure_trace,
    preview_text,
    prompt_preview,
    prompt_to_hash_payload,
)
from domain.ai.services.llm_service import GenerationConfig, GenerationResult, LLMService
from domain.ai.value_objects.prompt import Prompt
from infrastructure.ai.config.settings import Settings
from infrastructure.ai.providers.anthropic_provider import AnthropicProvider
from infrastructure.ai.providers.gemini_provider import GeminiProvider
from infrastructure.ai.providers.mock_provider import MockProvider
from infrastructure.ai.providers.openai_provider import OpenAIProvider
from infrastructure.ai.providers.token_limits import resolve_token_limits
from infrastructure.ai.trace_recorder import get_trace_recorder
from application.ai.usage_context import capture_usage, current_usage, response_usage
from infrastructure.ai.usage_ledger import UsageLedger
from domain.ai.services.token_budget import conservative_token_count
from infrastructure.ai.url_utils import (
    normalize_anthropic_base_url,
    normalize_gemini_base_url,
    normalize_openai_base_url,
)

_DEFAULT_CONFIG = GenerationConfig()
logger = logging.getLogger(__name__)


class LLMProviderFactory:
    def __init__(self, control_service: Optional[LLMControlService] = None):
        self.control_service = control_service or LLMControlService()

    def create_from_profile(self, profile: Optional[LLMProfile]) -> LLMService:
        if profile is None:
            raise ValueError('未配置可用的模型档案；请先在模型控制台配置并激活模型')

        resolved = self.control_service.resolve_profile(profile)
        if not resolved.model.strip():
            raise ValueError('未配置模型 ID；请在模型档案中指定真实模型')
        if not resolved.api_key.strip() and getattr(resolved, 'auth_mode', 'api_key') != 'none':
            raise ValueError('API key is required for the selected profile')

        settings = self._profile_to_settings(resolved)
        if resolved.protocol == "anthropic":
            return AnthropicProvider(settings)
        if resolved.protocol == "gemini":
            return GeminiProvider(settings)
        if resolved.protocol == "openai":
            return OpenAIProvider(settings)
        raise ValueError(f'Unsupported LLM protocol: {resolved.protocol}')

    def create_active_provider(self) -> LLMService:
        return self.create_from_profile(self.control_service.resolve_active_profile())

    def create_from_profile_id(self, profile_id: str) -> LLMService:
        """根据档案 ID 创建动态 Provider；找不到档案时明确报错。"""
        return DynamicLLMService(self, profile_id=profile_id)

    def _profile_to_settings(self, profile: LLMProfile) -> Settings:
        if profile.protocol == "anthropic":
            normalized_base_url = normalize_anthropic_base_url(profile.base_url)
        elif profile.protocol == "gemini":
            normalized_base_url = normalize_gemini_base_url(profile.base_url)
        else:
            normalized_base_url = normalize_openai_base_url(profile.base_url)

        return Settings(
            default_model=profile.model,
            default_temperature=profile.temperature,
            default_max_tokens=profile.max_tokens,
            api_key=profile.api_key,
            base_url=normalized_base_url,
            timeout_seconds=profile.timeout_seconds,
            extra_headers=profile.extra_headers,
            extra_query=profile.extra_query,
            extra_body=profile.extra_body,
            provider_name=profile.name,
            protocol=profile.protocol,
            use_legacy_chat_completions=profile.use_legacy_chat_completions,
            endpoint_kind=getattr(profile, 'endpoint_kind', 'auto'),
            auth_mode=getattr(profile, 'auth_mode', 'api_key'),
            reasoning_effort=getattr(profile, 'reasoning_effort', None),
            thinking_budget_tokens=getattr(profile, 'thinking_budget_tokens', None),
            top_p=getattr(profile, 'top_p', None),
            stop_sequences=getattr(profile, 'stop_sequences', None),
            max_output_tokens_limit=profile.max_output_tokens_limit,
            context_window_limit=profile.context_window_limit,
            profile_id=profile.id,
            currency=profile.currency,
            input_price_per_million=profile.input_price_per_million,
            output_price_per_million=profile.output_price_per_million,
        )


def _make_cache_key(profile: LLMProfile) -> str:
    """生成 Provider 缓存键，配置变化时自动重建 Provider。"""
    key_parts = [
        profile.protocol or "",
        (profile.base_url or "").rstrip("/"),
        (profile.model or "").strip(),
        hashlib.sha256((profile.api_key or '').encode()).hexdigest(),
        str(profile.temperature),
        str(profile.max_tokens),
        str(profile.timeout_seconds),
        str(profile.use_legacy_chat_completions),
        str(getattr(profile, 'endpoint_kind', 'auto')),
        str(getattr(profile, 'auth_mode', 'api_key')),
        str(getattr(profile, 'reasoning_effort', None)),
        str(getattr(profile, 'thinking_budget_tokens', None)),
        str(getattr(profile, 'top_p', None)),
        json.dumps(getattr(profile, 'stop_sequences', None), ensure_ascii=False),
        hashlib.sha256(json.dumps({key: getattr(profile, key) for key in ('max_output_tokens_limit', 'context_window_limit',
            'currency', 'input_price_per_million', 'output_price_per_million', 'extra_headers', 'extra_query', 'extra_body')},
            sort_keys=True, ensure_ascii=False).encode()).hexdigest(),
    ]
    settings = Settings(timeout_seconds=profile.timeout_seconds)
    key_parts.extend(
        [
            str(settings.connect_timeout),
            str(settings.read_timeout),
            str(settings.write_timeout),
            str(settings.pool_timeout),
        ]
    )
    return "|".join(key_parts)


class DynamicLLMService(LLMService):
    """动态读取当前激活配置，并在配置不变时复用 Provider。"""

    def __init__(self, factory: Optional[LLMProviderFactory] = None, usage_ledger=None, *, profile_id=None):
        self.factory = factory or LLMProviderFactory()
        self._cached_provider: Optional[LLMService] = None
        self._cached_key: Optional[str] = None
        self._usage_ledger = usage_ledger
        self._profile_id = profile_id

    async def _resolve_provider(self) -> LLMService:
        if self._profile_id:
            profile = next((p for p in self.factory.control_service.get_config().profiles if p.id == self._profile_id), None)
            if profile is None:
                await self._close_cached_provider()
                raise ValueError(f'LLM profile not found: {self._profile_id}')
        else:
            profile = self.factory.control_service.resolve_active_profile()
        if profile is None:
            await self._close_cached_provider()
            raise ValueError('未配置可用的模型档案；请先在模型控制台配置并激活模型')
        key = _make_cache_key(profile)

        if key == self._cached_key and self._cached_provider is not None:
            return self._cached_provider

        await self._close_cached_provider()

        provider = self.factory.create_from_profile(profile)
        self._cached_provider = provider
        self._cached_key = key
        logger.debug(
            "Provider 缓存未命中，创建新实例: protocol=%s model=%s",
            getattr(profile, "protocol", "?"),
            getattr(profile, "model", "?"),
        )
        return provider

    async def _close_cached_provider(self) -> None:
        """关闭旧 Provider 的 HTTP 连接资源。"""
        old = self._cached_provider
        if old is None:
            return
        try:
            if hasattr(old, "aclose"):
                await old.aclose()
            else:
                self._close_sync_clients(old)
                await self._close_async_clients(old)
        except Exception as exc:
            logger.debug("关闭旧 Provider 时出现可忽略异常: %s", exc)
        finally:
            self._cached_provider = None
            self._cached_key = None

    @staticmethod
    def _close_sync_clients(provider: LLMService) -> None:
        for attr in ("_http_client_sync",):
            client = getattr(provider, attr, None)
            if client is not None and hasattr(client, "is_closed") and not client.is_closed:
                client.close()

    @staticmethod
    async def _close_async_clients(provider: LLMService) -> None:
        for attr in ("_http_client_async", "_http_client", "_stream_http_client"):
            client = getattr(provider, attr, None)
            if client is not None and hasattr(client, "aclose"):
                await client.aclose()

    @staticmethod
    def _merge_config(config: GenerationConfig, provider: LLMService) -> GenerationConfig:
        settings = getattr(provider, "settings", None)
        if settings is None:
            return config

        model = config.model
        if not model or model == _DEFAULT_CONFIG.model:
            model = settings.default_model

        max_tokens = config.max_tokens
        if not getattr(config, 'max_tokens_explicit', True):
            max_tokens = settings.default_max_tokens

        temperature = config.temperature
        if not config.temperature_explicit:
            temperature = settings.default_temperature

        # Request-level reasoning controls override the whole profile strategy.
        # An explicit "none" or zero budget must suppress the profile default.
        reasoning_effort = config.reasoning_effort
        thinking_budget = config.thinking_budget_tokens
        if reasoning_effort is None and thinking_budget is None:
            reasoning_effort = getattr(settings, 'reasoning_effort', None)
            thinking_budget = getattr(settings, 'thinking_budget_tokens', None)
        return GenerationConfig(
            model=model,
            max_tokens=max_tokens,
            temperature=temperature,
            response_format=config.response_format,
            reasoning_effort=reasoning_effort,
            thinking_budget_tokens=thinking_budget,
            top_p=config.top_p if config.top_p is not None else getattr(settings, 'top_p', None),
            stop_sequences=config.stop_sequences if config.stop_sequences is not None else getattr(settings, 'stop_sequences', None),
            temperature_explicit=config.temperature_explicit,
        )

    @staticmethod
    def _request_metadata(
        provider: LLMService,
        effective_config: GenerationConfig,
        *,
        stream: bool = False,
    ) -> tuple[str, str, str, dict]:
        settings = getattr(provider, "settings", None)
        provider_label = provider.__class__.__name__
        generation_profile = getattr(settings, "provider_name", None) or provider_label
        model = effective_config.model or getattr(settings, "default_model", "")
        metadata = {
            "provider": provider_label,
            "protocol": getattr(settings, "protocol", None),
            "base_url": getattr(settings, "base_url", None),
            "temperature": effective_config.temperature,
            "max_tokens": effective_config.max_tokens,
            "effective_max_tokens": effective_config.max_tokens,
            "response_format": effective_config.response_format,
            "reasoning_effort": effective_config.reasoning_effort,
            "thinking_budget_tokens": effective_config.thinking_budget_tokens,
            "top_p": effective_config.top_p,
            "stop_sequences": effective_config.stop_sequences,
            "endpoint_kind": getattr(settings, 'endpoint_kind', 'auto'),
            "auth_mode": getattr(settings, 'auth_mode', 'api_key'),
        }
        limits = resolve_token_limits(getattr(settings, 'protocol', '') or '', model, settings)
        metadata.update(max_output_tokens_limit=limits.max_output_tokens,
                        context_window_limit=limits.context_window,
                        token_limit_source=limits.source)
        if stream:
            metadata["stream"] = True
        return provider_label, generation_profile, model, metadata

    def _reserve_usage(self, provider, prompt, config, capture):
        settings = getattr(provider, 'settings', None)
        model = config.model or getattr(settings, 'default_model', '')
        if hasattr(provider, '_validate_output_tokens'):
            provider._validate_output_tokens(config, model, protocol=getattr(settings, 'protocol', '') or '')
        trace = ensure_trace(novel_id=capture.novel_id or None)
        capture.novel_id = capture.novel_id or trace.novel_id
        ledger = self._usage_ledger or UsageLedger()
        serialized = json.dumps({'system': prompt.system, 'user': prompt.user,
            'response_format': config.response_format,
            'extra_body': getattr(settings, 'extra_body', {})}, ensure_ascii=False, default=str)
        request_id = ledger.reserve(novel_id=capture.novel_id, attempt_id=capture.attempt_id,
            settings=settings, model=model, input_bound=conservative_token_count(serialized) + 1024,
            max_tokens=config.max_tokens)
        budget = ledger.db.fetch_one('SELECT limit_nanos FROM ai_usage_budgets WHERE novel_id=?', (capture.novel_id,))
        capture.metadata['budget_enforced'] = budget is not None and budget['limit_nanos'] is not None
        return ledger, request_id

    async def generate(self, prompt: Prompt, config: GenerationConfig) -> GenerationResult:
        provider = await self._resolve_provider()
        effective = self._merge_config(config, provider)
        scope = nullcontext(current_usage()) if current_usage() is not None else capture_usage()
        with scope as capture:
            ledger, request_id = self._reserve_usage(provider, prompt, effective, capture)
            try:
                result = await self._generate(prompt, effective)
            except BaseException:
                capture.token_usage = response_usage(capture)
                capture.metadata.update(ledger.finish(request_id, usage=capture.token_usage, status='failed'))
                raise
            capture.token_usage = response_usage(capture, result.token_usage) if not isinstance(provider, MockProvider) else None
            capture.metadata.update(ledger.finish(request_id, usage=capture.token_usage, status='succeeded'))
            result.accounting = dict(capture.metadata)
            result.token_usage = capture.token_usage
            return result

    async def _generate(self, prompt: Prompt, config: GenerationConfig) -> GenerationResult:
        provider = await self._resolve_provider()
        effective_config = self._merge_config(config, provider)
        trace = ensure_trace(operation="llm_generate", metadata={"entry": "DynamicLLMService.generate"})
        request_span_id = trace.new_span_id("llm-request")
        recorder = get_trace_recorder()
        _, generation_profile, model, request_metadata = self._request_metadata(provider, effective_config)

        recorder.record_span(
            phase="llm_request",
            trace_context=trace,
            span_id=request_span_id,
            stage=trace.stage,
            stage_label=trace.stage_label,
            node_type="llm",
            model=model,
            generation_profile=generation_profile,
            prompt_hash=content_hash(prompt_to_hash_payload(prompt)),
            prompt_preview=prompt_preview(prompt),
            prompt_full=prompt_to_hash_payload(prompt),
            metadata=request_metadata,
        )
        started = time.perf_counter()
        try:
            result = await provider.generate(prompt, effective_config)
        except Exception as exc:
            recorder.record_span(
                phase="error",
                trace_context=trace,
                parent_span_id=request_span_id,
                stage=trace.stage,
                stage_label=trace.stage_label,
                node_type="llm",
                model=model,
                generation_profile=generation_profile,
                latency_ms=int((time.perf_counter() - started) * 1000),
                error=str(exc),
                metadata={**request_metadata, "stage": "provider.generate"},
            )
            raise

        capture = current_usage()
        usage = response_usage(capture, getattr(result, 'token_usage', None)) if capture is not None else getattr(result, 'token_usage', None)
        if isinstance(provider, MockProvider):
            usage = None
        content = getattr(result, "content", "") or ""
        request_metadata['request_receipt'] = getattr(result, 'request_receipt', {})
        recorder.record_span(
            phase="llm_response",
            trace_context=trace,
            parent_span_id=request_span_id,
            stage=trace.stage,
            stage_label=trace.stage_label,
            node_type="llm",
            model=model,
            generation_profile=generation_profile,
            response_hash=content_hash(content),
            response_preview=preview_text(content),
            response_full=content,
            token_input=getattr(usage, "input_tokens", None),
            token_output=getattr(usage, "output_tokens", None),
            latency_ms=int((time.perf_counter() - started) * 1000),
            metadata={**request_metadata, "content_length": len(content)},
        )
        return result

    async def stream_generate(self, prompt: Prompt, config: GenerationConfig) -> AsyncIterator[str]:
        provider = await self._resolve_provider()
        effective = self._merge_config(config, provider)
        scope = nullcontext(current_usage()) if current_usage() is not None else capture_usage()
        with scope as capture:
            ledger, request_id = self._reserve_usage(provider, prompt, effective, capture)
            status = 'failed'
            try:
                yielded = False
                async for chunk in self._stream_generate(prompt, effective):
                    yielded = yielded or bool(chunk and chunk.strip())
                    yield chunk
                if not yielded:
                    raise RuntimeError('Provider stream returned empty content')
                status = 'succeeded'
            except (GeneratorExit, asyncio.CancelledError):
                status = 'cancelled'
                raise
            finally:
                usage = capture.token_usage if status == 'succeeded' and not isinstance(provider, MockProvider) else None
                capture.metadata.update(ledger.finish(request_id, usage=usage, status=status))
                capture.token_usage = usage

    async def _stream_generate(self, prompt: Prompt, config: GenerationConfig) -> AsyncIterator[str]:
        provider = await self._resolve_provider()
        effective_config = self._merge_config(config, provider)
        trace = ensure_trace(
            operation="llm_stream_generate",
            metadata={"entry": "DynamicLLMService.stream_generate"},
        )
        request_span_id = trace.new_span_id("llm-stream-request")
        recorder = get_trace_recorder()
        _, generation_profile, model, request_metadata = self._request_metadata(
            provider,
            effective_config,
            stream=True,
        )

        recorder.record_span(
            phase="llm_request",
            trace_context=trace,
            span_id=request_span_id,
            stage=trace.stage,
            stage_label=trace.stage_label,
            node_type="llm",
            model=model,
            generation_profile=generation_profile,
            prompt_hash=content_hash(prompt_to_hash_payload(prompt)),
            prompt_preview=prompt_preview(prompt),
            prompt_full=prompt_to_hash_payload(prompt),
            metadata=request_metadata,
        )
        started = time.perf_counter()
        response_parts: list[str] = []
        preview_parts: list[str] = []
        preview_chars = 0
        total_chars = 0
        try:
            async for chunk in provider.stream_generate(prompt, effective_config):
                if chunk:
                    response_parts.append(chunk)
                    total_chars += len(chunk)
                    if preview_chars < 320:
                        preview_parts.append(chunk)
                        preview_chars += len(chunk)
                yield chunk
        except Exception as exc:
            recorder.record_span(
                phase="error",
                trace_context=trace,
                parent_span_id=request_span_id,
                stage=trace.stage,
                stage_label=trace.stage_label,
                node_type="llm",
                model=model,
                generation_profile=generation_profile,
                latency_ms=int((time.perf_counter() - started) * 1000),
                error=str(exc),
                metadata={**request_metadata, "stage": "provider.stream_generate"},
            )
            raise
        else:
            content_for_hash = "".join(response_parts)
            if current_usage() is not None:
                request_metadata['request_receipt'] = current_usage().metadata.get('request_receipt', {})
            usage = current_usage().token_usage if current_usage() is not None else None
            recorder.record_span(
                phase="llm_response",
                trace_context=trace,
                parent_span_id=request_span_id,
                stage=trace.stage,
                stage_label=trace.stage_label,
                node_type="llm",
                model=model,
                generation_profile=generation_profile,
                response_hash=content_hash(content_for_hash),
                response_preview=preview_text("".join(preview_parts)),
                response_full=content_for_hash,
                token_input=getattr(usage, 'input_tokens', None),
                token_output=getattr(usage, 'output_tokens', None),
                latency_ms=int((time.perf_counter() - started) * 1000),
                metadata={**request_metadata, "content_length": total_chars},
            )

    async def aclose(self) -> None:
        """异步关闭缓存 Provider。"""
        await self._close_cached_provider()
