"""OpenAI LLM 提供商实现"""
import asyncio
import logging
import re
import openai
import httpx
from typing import Any, AsyncIterator

from openai import AsyncOpenAI

from domain.ai.services.llm_service import GenerationConfig, GenerationResult
from domain.ai.value_objects.prompt import Prompt
from domain.ai.value_objects.token_usage import TokenUsage
from application.ai.llm_retry_policy import LLM_MAX_TOTAL_ATTEMPTS, is_retryable_llm_error, llm_retry_delay_seconds
from application.ai.usage_context import current_usage, parse_usage, record_stream_usage, record_response_usage
from infrastructure.ai.config.settings import Settings
from infrastructure.ai.http_timeout import build_httpx_timeout
from .base import BaseProvider
from .model_resolution import require_resolved_model_id
from .generation_constraints import extract_openai_reasoning_controls, openai_endpoint

logger = logging.getLogger(__name__)


class OpenAIProvider(BaseProvider):
    """OpenAI LLM 提供商实现

    通过 endpoint_kind 选择协议：chat 固定 C，responses 固定 R；
    auto 优先 Responses，仅在端点不支持时协商到 Chat；保留旧版标志兼容。
    """

    _fallback_to_chat_cache: set[tuple[str, str, str, str]] = set()

    def __init__(self, settings: Settings):
        super().__init__(settings)

        if not settings.api_key and settings.auth_mode != 'none':
            raise ValueError("API key is required for OpenAIProvider")
        if settings.auth_mode == 'none' and not settings.base_url:
            raise ValueError('auth_mode=none requires an explicit custom endpoint')

        self._force_responses = settings.endpoint_kind == 'responses'
        self._use_legacy = settings.endpoint_kind == 'chat' or (settings.endpoint_kind == 'auto' and settings.use_legacy_chat_completions)

        client_kwargs = {
            # The SDK requires a nonempty constructor credential even for
            # local no-auth services. Its header is removed by the request
            # hook below before any bytes are sent.
            "api_key": settings.api_key or 'plotpilot-local-no-auth',
            "timeout": settings.timeout_seconds,
            "default_headers": settings.extra_headers or None,
            "default_query": settings.extra_query or None,
            "max_retries": 0,
        }
        if settings.base_url:
            client_kwargs["base_url"] = settings.base_url

        self._http_client = httpx.AsyncClient(
            timeout=build_httpx_timeout(settings.http_timeout_settings),
            trust_env=False,
            event_hooks={'request': [self._strip_auth_header]} if settings.auth_mode == 'none' else None,
        )
        client_kwargs["http_client"] = self._http_client
        self.async_client = AsyncOpenAI(**client_kwargs)

    @staticmethod
    async def _strip_auth_header(request):
        request.headers.pop('authorization', None)

    def _endpoint_cache_key(self, config: GenerationConfig):
        return (self.settings.protocol or 'openai',
                (self.settings.base_url or 'https://api.openai.com/v1').rstrip('/'),
                config.model or self.settings.default_model, self.settings.endpoint_kind)

    def _may_fallback(self, error: Exception) -> bool:
        """Negotiate protocols only for endpoint failures, never request failures.

        SDK status and structured error fields take precedence over message
        text: a proxy may mention an upstream 404 in an auth or overload error.
        Non-HTTP gateway errors need an explicit unsupported API declaration.
        """
        if self._force_responses or self._use_legacy:
            return False
        if isinstance(error, (ValueError, TypeError, openai.APIConnectionError,
                              httpx.TransportError, TimeoutError, ConnectionError)):
            return False

        status = getattr(error, 'status_code', None)
        if status is None and isinstance(error, httpx.HTTPStatusError):
            status = error.response.status_code
        if status is not None and status not in (400, 404, 405, 422, 501):
            return False

        body = getattr(error, 'body', None)
        if isinstance(body, dict) and isinstance(body.get('error'), dict):
            body = body['error']
        details = body if isinstance(body, dict) else {}
        code = str(details.get('code') or getattr(error, 'code', '') or '').lower()
        message = str(details.get('message') or getattr(error, 'message', None) or error).lower()
        if (details.get('param') or getattr(error, 'param', None)
                or code in {'model_not_found', 'invalid_model', 'unsupported_model',
                            'invalid_api_key', 'authentication_error', 'permission_denied',
                            'rate_limit_exceeded', 'insufficient_quota', 'unsupported_parameter'}):
            return False
        if ('model' in message and any(marker in message for marker in
                ('model_not_found', 'invalid_model', 'not found', 'does not exist', 'no such model'))):
            return False
        if status in (404, 405, 501):
            return True
        if code in {'unsupported_endpoint', 'unknown_endpoint', 'unsupported_api', 'unsupported_protocol'}:
            return True
        return any(re.search(pattern, message) for pattern in (
            r'\b(?:unsupported|unknown|unrecognized)\s+(?:api\s+)?(?:endpoint|route|path|responses(?:\s+api)?)\b',
            r'\b(?:responses(?:\s+api)?|endpoint|route)(?:\s+(?:is|are))?\s+(?:unsupported|not supported|not implemented|not found|does not exist)\b',
            r'\b(?:endpoint|gateway|server|service|model)\b[^.;\n]{0,60}\b(?:does not|do not|doesn\x27t)\s+support\s+(?:the\s+)?responses(?:\s+api)?\b',
            r'/responses[\x27\"]?(?:\s+(?:endpoint|api))?(?:\s+is)?\s+(?:unsupported|not supported|not implemented|not found|does not exist)\b',
        ))

    def _should_use_responses(self, config: GenerationConfig) -> bool:
        force_responses = getattr(self, '_force_responses', self.settings.endpoint_kind == 'responses')
        endpoint = openai_endpoint('responses' if force_responses else self.settings.endpoint_kind,
                                   legacy_chat=self._use_legacy,
                                   stop_sequences=config.stop_sequences)
        if endpoint == 'chat':
            return False
        return not self._use_legacy and (force_responses or self._endpoint_cache_key(config) not in self.__class__._fallback_to_chat_cache)

    @staticmethod
    def _chat_completion_finished(chunk: Any) -> bool:
        choices = getattr(chunk, 'choices', None) or []
        if not choices:
            return False
        reason = getattr(choices[0], 'finish_reason', None)
        if isinstance(reason, str) and reason:
            if reason != 'stop':
                raise RuntimeError(f'Chat completion did not finish normally: {reason}')
            return True
        return False

    async def generate(
        self,
        prompt: Prompt,
        config: GenerationConfig
    ) -> GenerationResult:
        try:
            base_url = self.settings.base_url or "https://api.openai.com/v1"
            use_responses = self._should_use_responses(config)

            if use_responses:
                try:
                    return await self._generate_via_responses(prompt, config)
                except (openai.NotFoundError, openai.BadRequestError) as e:
                    if not self._may_fallback(e):
                        raise
                    logger.info(f"Responses API unsupported for {base_url}, falling back to chat completions: {str(e)}")
                    self.__class__._fallback_to_chat_cache.add(self._endpoint_cache_key(config))
                except Exception as e:
                    # Some gateways use explicit endpoint error messages in
                    # place of standard HTTP status codes.
                    if self._may_fallback(e):
                        logger.info(f"Gateway returned error for Responses API ({base_url}), falling back: {str(e)}")
                        self.__class__._fallback_to_chat_cache.add(self._endpoint_cache_key(config))
                    else:
                        raise

            # 使用降级的 Chat Completions API
            return await self._generate_via_chat(prompt, config)
        except RuntimeError:
            raise
        except ValueError:
            raise
        except Exception as e:
            raise RuntimeError(f"Failed to generate text: {str(e)}") from e

    async def _generate_via_chat(self, prompt: Prompt, config: GenerationConfig) -> GenerationResult:
        """Chat Completions API 非流式生成

        空正文可在统一重试预算内尝试流式聚合；格式约束始终保留。
        """
        messages = self._build_messages(prompt)
        last_error: Exception | None = None

        capture = current_usage()
        total_attempts = 1 if capture is not None and capture.metadata.get('budget_enforced') else LLM_MAX_TOTAL_ATTEMPTS
        for attempt in range(total_attempts):
            request_kwargs = self._build_chat_request_kwargs(messages, config)
            receipt = dict(self.last_request_receipt)
            try:
                response = await self.async_client.chat.completions.create(**request_kwargs)

                token_usage = parse_usage(getattr(response, 'usage', None), 'prompt_tokens', 'completion_tokens')
                record_response_usage(token_usage)
                if not self._chat_completion_finished(response):
                    raise RuntimeError('Chat completion is missing a normal finish_reason')
                content = self._extract_text_from_response(response)

                if not content:
                    if capture is not None and capture.metadata.get('budget_enforced'):
                        raise RuntimeError('Empty response; automatic fallback disabled while a book budget is enforced')
                    logger.warning(
                        "OpenAI-compatible response returned empty non-stream content; "
                        "falling back to streaming aggregation (attempt=%d/%d)",
                        attempt + 1,
                        LLM_MAX_TOTAL_ATTEMPTS,
                    )
                    content, token_usage = await self._generate_via_stream(request_kwargs)
                    return self._result_with_receipt(GenerationResult(content=content, token_usage=token_usage), receipt)

                return self._result_with_receipt(GenerationResult(
                    content=content,
                    token_usage=token_usage,
                ), receipt)
            except Exception as exc:
                last_error = exc
                if attempt >= total_attempts - 1 or not is_retryable_llm_error(exc):
                    raise
                record_response_usage(None)
                delay = llm_retry_delay_seconds(exc, attempt)
                if delay > 120:
                    raise
                logger.warning(
                    "OpenAI-compatible provider transient failure; retrying in %.1fs "
                    "(attempt=%d/%d): %s",
                    delay,
                    attempt + 1,
                    LLM_MAX_TOTAL_ATTEMPTS,
                    exc,
                )
                await asyncio.sleep(delay)

        if last_error is not None:
            raise last_error
        raise RuntimeError("API returned empty content")

    async def stream_generate(
        self,
        prompt: Prompt,
        config: GenerationConfig
    ) -> AsyncIterator[str]:
        try:
            base_url = self.settings.base_url or "https://api.openai.com/v1"
            use_responses = self._should_use_responses(config)

            if use_responses:
                yielded_response_text = False
                responses_completed = False
                try:
                    # 尝试走 Responses 流式 API
                    request_kwargs = self._build_responses_request_kwargs(prompt, config, stream=True)
                    stream = await self.async_client.responses.create(**request_kwargs)
                    async for chunk in stream:
                        response = getattr(chunk, 'response', None)
                        if response is not None and getattr(chunk, 'type', '') == 'response.completed':
                            record_stream_usage(getattr(response, 'usage', None))
                        if getattr(chunk, 'type', '') == 'response.completed':
                            responses_completed = True
                        if getattr(chunk, 'type', '') in ('error', 'response.failed', 'response.incomplete'):
                            raise RuntimeError('Responses stream did not complete successfully')
                        content = self._extract_text_from_responses_chunk(chunk)
                        if content:
                            yielded_response_text = True
                            yield content
                    if not responses_completed:
                        raise RuntimeError('Responses stream ended before response.completed')
                    return
                except (openai.NotFoundError, openai.BadRequestError) as e:
                    if yielded_response_text or not self._may_fallback(e):
                        raise
                    self.__class__._fallback_to_chat_cache.add(self._endpoint_cache_key(config))
                    logger.info(f"Stream: Responses API unsupported for {base_url}, falling back.")
                except Exception as e:
                    if not yielded_response_text and self._may_fallback(e):
                        self.__class__._fallback_to_chat_cache.add(self._endpoint_cache_key(config))
                        logger.info(f"Stream: Gateway returned error for Responses API ({base_url}), falling back.")
                    else:
                        logger.error(f"[Responses Stream] Failed: {e}")
                        raise

            # 降级：走原来的 Chat Completions 流式 API
            messages = self._build_messages(prompt)
            request_kwargs = self._build_chat_request_kwargs(messages, config, stream=True)
            stream = await self.async_client.chat.completions.create(**request_kwargs)
            chat_completed = False
            async for chunk in stream:
                record_stream_usage(getattr(chunk, 'usage', None), 'prompt_tokens', 'completion_tokens')
                chat_completed = self._chat_completion_finished(chunk) or chat_completed
                content = self._extract_text_from_stream_chunk(chunk)
                if content:
                    yield content
            if not chat_completed:
                raise RuntimeError('Chat stream ended before a normal finish_reason')
        except ValueError:
            # Configuration/capability failures are deterministic and must be
            # observable as such; wrapping them as transport errors obscures
            # the fact that no HTTP request was attempted.
            raise
        except Exception as e:
            logger.error(f"[Stream] Failed: {e}")
            raise RuntimeError(f"Failed to stream text: {str(e)}") from e

    @staticmethod
    def _build_messages(prompt: Prompt) -> list[dict[str, str]]:
        return [
            {"role": "system", "content": prompt.system},
            {"role": "user", "content": prompt.user}
        ]

    def _build_chat_request_kwargs(
        self,
        messages: list[dict[str, str]],
        config: GenerationConfig,
        *,
        stream: bool = False,
    ) -> dict[str, Any]:
        model_id = require_resolved_model_id(
            config.model,
            self.settings.default_model,
            provider_label="OpenAI 兼容",
        )
        self._validate_output_tokens(config, model_id, protocol='openai', endpoint_kind='chat')
        reasoning_effort = config.reasoning_effort
        extra_body = dict(self.settings.extra_body or {})
        reasoning_effort, extra_body = extract_openai_reasoning_controls(extra_body, reasoning_effort)
        kwargs: dict[str, Any] = {
            "model": model_id,
            "messages": messages,
            "max_completion_tokens" if reasoning_effort is not None else "max_tokens": config.max_tokens,
            "extra_headers": self.settings.extra_headers or None,
            "extra_query": self.settings.extra_query or None,
            "extra_body": extra_body or None,
            "timeout": self.settings.timeout_seconds,
        }
        if reasoning_effort is not None:
            # Reasoning models commonly reject sampling temperature. Omitting it
            # lets the provider apply the model's supported default.
            kwargs['reasoning_effort'] = reasoning_effort
        else:
            kwargs['temperature'] = config.temperature
        if config.top_p is not None:
            kwargs['top_p'] = config.top_p
        if config.stop_sequences:
            kwargs['stop'] = config.stop_sequences

        if config.response_format:
            kwargs["response_format"] = config.response_format

        if stream:
            kwargs["stream"] = True
            kwargs['stream_options'] = {'include_usage': True}
            extra_options = (self.settings.extra_body or {}).get('stream_options')
            if isinstance(extra_options, dict) and extra_options.get('include_usage') is False:
                raise ValueError('extra_body cannot disable stream usage reporting')
        for key, value in extra_body.items():
            if key in kwargs and key != 'stream_options' and kwargs[key] != value:
                raise ValueError(f'extra_body cannot override {key}')
        if stream and isinstance(extra_body.get('stream_options'), dict):
            kwargs['stream_options'].update(extra_body.pop('stream_options'))
            kwargs['stream_options']['include_usage'] = True
            kwargs['extra_body'] = extra_body or None
        self._validate_context_window(kwargs, config, model_id, protocol='openai')
        self._record_request_receipt(kwargs, config, endpoint='openai.chat', degraded=not self._use_legacy and self._endpoint_cache_key(config) in self.__class__._fallback_to_chat_cache)
        return kwargs

    def _build_responses_request_kwargs(
        self,
        prompt: Prompt,
        config: GenerationConfig,
        *,
        stream: bool = False,
    ) -> dict[str, Any]:
        model_id = require_resolved_model_id(
            config.model,
            self.settings.default_model,
            provider_label="OpenAI 兼容",
        )
        self._validate_output_tokens(config, model_id, protocol='openai', endpoint_kind='responses')
        reasoning_effort = config.reasoning_effort
        extra_body = dict(self.settings.extra_body or {})
        reasoning_effort, extra_body = extract_openai_reasoning_controls(extra_body, reasoning_effort)
        kwargs: dict[str, Any] = {
            "model": model_id,
            "instructions": prompt.system,
            "input": [{"role": "user", "content": prompt.user}],
            "max_output_tokens": config.max_tokens,
        }
        if reasoning_effort is not None:
            kwargs['reasoning'] = {'effort': reasoning_effort}
        else:
            kwargs['temperature'] = config.temperature
        if config.top_p is not None:
            kwargs['top_p'] = config.top_p
        if config.response_format:
            fmt = config.response_format
            if fmt.get('type') == 'json_object':
                kwargs['text'] = {'format': {'type': 'json_object'}}
            elif fmt.get('type') == 'json_schema':
                schema = dict(fmt.get('json_schema') or {})
                if not isinstance(schema.get('schema'), dict):
                    raise ValueError('json_schema requires a schema object')
                schema.setdefault('name', 'response')
                kwargs['text'] = {'format': {'type': 'json_schema', **schema}}
            elif fmt.get('type') == 'text':
                kwargs['text'] = {'format': {'type': 'text'}}

        if stream:
            kwargs["stream"] = True
        extensions: dict[str, Any] = {}
        for key, value in extra_body.items():
            if key == 'reasoning' and isinstance(value, dict):
                # ``effort`` was extracted above; retain vendor-supported
                # fields such as ``summary`` in the native Responses object.
                if isinstance(kwargs.get('reasoning'), dict):
                    kwargs['reasoning'].update(value)
                else:
                    kwargs['reasoning'] = dict(value)
                continue
            if key in kwargs and kwargs[key] != value:
                raise ValueError(f'extra_body cannot override {key}')
            if key not in kwargs:
                # Vendor extensions belong in the SDK's extra_body. Passing
                # them as keyword arguments fails before sending any request.
                extensions[key] = value
        if extensions:
            kwargs['extra_body'] = extensions

        self._validate_context_window(kwargs, config, model_id, protocol='openai')
        self._record_request_receipt(kwargs, config, endpoint='openai.responses')
        return kwargs

    async def _generate_via_responses(self, prompt: Prompt, config: GenerationConfig) -> GenerationResult:
        """Responses API 非流式生成"""
        request_kwargs = self._build_responses_request_kwargs(prompt, config)
        receipt = dict(self.last_request_receipt)
        response = await self.async_client.responses.create(**request_kwargs)
        token_usage = (parse_usage(getattr(response, 'usage', None)) or
                       parse_usage(getattr(response, 'usage', None), 'prompt_tokens', 'completion_tokens'))
        record_response_usage(token_usage)
        if getattr(response, 'status', None) != 'completed':
            raise RuntimeError('Responses request did not complete successfully')

        output = getattr(response, "output", None)
        content_parts: list[str] = []
        if output:
            for item in output:
                if getattr(item, "type", "") == "message":
                    for part in getattr(item, "content", []):
                        if getattr(part, "type", "") in ("text", "output_text"):
                            piece = str(getattr(part, "text", "")).strip()
                            if piece:
                                content_parts.append(piece)
        content = "\n".join(content_parts).strip()
        if not content:
            raise RuntimeError("Responses API returned empty content")

        return self._result_with_receipt(GenerationResult(
            content=content,
            token_usage=token_usage
        ), receipt)

    @staticmethod
    def _extract_text_from_responses_chunk(chunk: Any) -> str:
        """原生 Responses stream 解析封装"""
        try:
            event_type = getattr(chunk, "type", "")
            if event_type == "response.output_text.delta":
                delta = getattr(chunk, "delta", None)
                if isinstance(delta, str):
                    return delta
            elif event_type in ("response.content_part.added", "response.content_part.delta"):
                part = getattr(chunk, "part", None)
                if part and getattr(part, "type", "") == "text":
                    return getattr(part, "text", "")
                delta = getattr(chunk, "delta", None)
                if isinstance(delta, str):
                    return delta
            elif event_type == "message.delta":
                delta = getattr(chunk, "delta", None)
                if delta:
                     content = getattr(delta, "content", None)
                     if isinstance(content, str):
                         return content
        except Exception:
            pass
        return ""

    @staticmethod
    def _normalize_chat_completion_content(content: Any) -> str:
        """兼容 message.content 为 str 或多段 content part 列表。

        🔥 自适应兼容多种网关的响应格式：
        1. 标准 OpenAI: content 是 str
        2. OpenAI 新协议: content 是 list[{type, text}]
        3. DeepSeek-R1: content 是 str，但 message.reasoning_content 单独存在
        4. 部分网关: content 是 list，包含 thinking/reasoning 类型 part
        5. 极端情况: content 是 None（空回复）
        """
        if content is None:
            return ""
        if isinstance(content, str):
            return content.strip()

        if isinstance(content, list):
            parts: list[str] = []
            for item in content:
                if isinstance(item, dict):
                    item_type = (item.get("type") or "").lower()
                    # 🔥 跳过推理/思考/拒绝类型的 content part
                    # DeepSeek-R1: reasoning_content 在 message 级别，不在 content part 中
                    # 但部分网关会把 thinking 放在 content part 里
                    if item_type in ("reasoning", "thinking", "refusal", "thought"):
                        continue
                    text_val = item.get("text")
                    if isinstance(text_val, str) and text_val.strip():
                        parts.append(text_val)
                else:
                    text_attr = getattr(item, "text", None)
                    if isinstance(text_attr, str) and text_attr.strip():
                        parts.append(text_attr)
            return "\n".join(parts).strip()

        return str(content).strip()

    @staticmethod
    def _extract_text_from_response(response: Any) -> str:
        if not getattr(response, "choices", None):
            return ""

        message = getattr(response.choices[0], "message", None)
        content = getattr(message, "content", None)
        result = OpenAIProvider._normalize_chat_completion_content(content)

        # 🔥 DeepSeek-R1 等模型：正文在 message.content，思考在 message.reasoning_content
        # reasoning_content 不需要返回给调用方（已经由 llm_output_sanitize 处理）
        # 但如果 content 为空且 reasoning_content 不为空，说明模型只输出了思考没有正文
        if not result.strip():
            reasoning = getattr(message, "reasoning_content", None)
            if reasoning and isinstance(reasoning, str) and reasoning.strip():
                logger.debug("message.content 为空但有 reasoning_content，模型可能只输出了推理")

        return result

    @staticmethod
    def _extract_text_from_stream_chunk(chunk: Any) -> str:
        if not getattr(chunk, "choices", None):
            # 🔥 容错：部分网关返回的流式 chunk 没有 choices 字段
            # 例如 DeepSeek-R1 的 reasoning_content 字段在顶层
            return ""

        choice = chunk.choices[0]
        delta = getattr(choice, "delta", None)
        content = getattr(delta, "content", None)
        if isinstance(content, str):
            return content
        if isinstance(content, list):
            return OpenAIProvider._normalize_chat_completion_content(content)
        return ""

    @staticmethod
    def _extract_text_from_stream_chunk(chunk: Any) -> str:
        if not getattr(chunk, "choices", None):
            return ""

        delta = getattr(chunk.choices[0], "delta", None)
        content = getattr(delta, "content", None)
        if isinstance(content, str):
            return content
        if isinstance(content, list):
            return OpenAIProvider._normalize_chat_completion_content(content)
        return ""

    async def _generate_via_stream(self, request_kwargs: dict[str, Any]) -> tuple[str, TokenUsage | None]:
        stream = await self.async_client.chat.completions.create(
            **{**request_kwargs, "stream": True, 'stream_options': {'include_usage': True}}
        )

        parts: list[str] = []
        token_usage = None
        completed = False

        async for chunk in stream:
            completed = self._chat_completion_finished(chunk) or completed
            content = self._extract_text_from_stream_chunk(chunk)
            if content:
                parts.append(content)

            usage = getattr(chunk, "usage", None)
            if usage is not None:
                token_usage = parse_usage(usage, 'prompt_tokens', 'completion_tokens')

        content = "".join(parts).strip()
        record_response_usage(token_usage)
        if not completed:
            raise RuntimeError('Chat stream ended before a normal finish_reason')
        if not content:
            raise RuntimeError("API returned empty content")

        return content, token_usage
