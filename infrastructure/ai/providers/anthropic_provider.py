"""Anthropic LLM 提供商实现"""
import json
import logging
from contextlib import aclosing
from typing import Any, AsyncIterator

import httpx
from anthropic import Anthropic, AsyncAnthropic, DefaultHttpxClient, DefaultAsyncHttpxClient

from domain.ai.services.llm_service import GenerationConfig, GenerationResult
from domain.ai.value_objects.prompt import Prompt
from domain.ai.value_objects.token_usage import TokenUsage
from application.ai.usage_context import current_usage, parse_usage, record_stream_usage, record_response_usage
from infrastructure.ai.config.settings import Settings
from infrastructure.ai.http_timeout import build_httpx_timeout
from .base import BaseProvider
from .model_resolution import require_resolved_model_id
from infrastructure.ai.url_utils import anthropic_messages_url

logger = logging.getLogger(__name__)


def _json_response_instruction(response_format: dict[str, Any]) -> str:
    """Build prompt-side JSON constraints for Anthropic Messages API."""
    fmt_type = response_format.get("type")
    if fmt_type == "json_schema":
        schema_payload = response_format.get("json_schema") or {}
        schema = schema_payload.get("schema") or {}
        schema_name = schema_payload.get("name") or "response"
        try:
            schema_text = json.dumps(schema, ensure_ascii=False, separators=(",", ":"))
        except Exception:
            schema_text = str(schema)
        return (
            "\n\n请只输出一个有效 JSON 对象，不要包含 Markdown 或额外文字。"
            f"\nJSON 对象需符合 `{schema_name}` schema：{schema_text}"
        )
    if fmt_type == "json_object":
        return "\n\n请只输出一个有效 JSON 对象，不要包含 Markdown 或额外文字。"
    return ""


def _extract_text_from_content_block(block: Any) -> str:
    """尽量从兼容端点返回的 content block 中提取文本。"""
    if block is None:
        return ""
    if (block.get('type') if isinstance(block, dict) else getattr(block, 'type', None)) in {'thinking', 'redacted_thinking', 'reasoning'}:
        return ""

    if isinstance(block, str):
        return block

    text = getattr(block, "text", None)
    if isinstance(text, str) and text.strip():
        return text

    if isinstance(block, dict):
        for key in ("text", "content", "value"):
            value = block.get(key)
            if isinstance(value, str) and value.strip():
                return value
        if block.get("type") == "json" and block.get("json") is not None:
            try:
                return json.dumps(block["json"], ensure_ascii=False)
            except Exception:
                return str(block["json"])

    block_type = getattr(block, "type", None)
    if block_type in {"json", "input_json", "output_json"}:
        json_payload = getattr(block, "json", None)
        if json_payload is not None:
            try:
                return json.dumps(json_payload, ensure_ascii=False)
            except Exception:
                return str(json_payload)

    return ""


class AnthropicProvider(BaseProvider):
    """Anthropic LLM 提供商实现

    使用 Anthropic API 实现 LLM 服务。

    双端点策略：
    - generate() (规划/分析): 使用官方 SDK，走官方 API (HTTPS)
    - stream_generate() (正文生成): 使用自定义 httpx，走代理服务器
    """

    def __init__(self, settings: Settings):
        """初始化 Anthropic 提供商

        Args:
            settings: AI 配置设置

        Raises:
            ValueError: 如果 API key 未设置
        """
        super().__init__(settings)

        if not settings.api_key:
            raise ValueError("API key is required for AnthropicProvider")

        # 归一化 base_url：去掉尾部 /v1（SDK 内部会自动拼 /v1/messages）
        base = settings.base_url.rstrip("/") if settings.base_url else None
        if base and base.endswith("/v1"):
            base = base[:-3]

        official_client_kw = {
            "api_key": settings.api_key,
            "timeout": 300.0,  # 5 分钟超时
            "max_retries": 0,
            "default_headers": {
                "User-Agent": "claude-cli/2.1.87 (external, cli)",
                **(settings.extra_headers or {}),
            },
            "default_query": settings.extra_query or None,
        }
        if base:
            official_client_kw["base_url"] = base

        # SDK 内置 httpx 默认 trust_env=True，会走系统 HTTP(S)_PROXY，本机代理 TLS 常导致 ConnectError。
        _sdk_timeout = build_httpx_timeout(settings.http_timeout_settings)
        # Use the SDK's public client factories: recent SDKs use httpx2 while
        # older supported releases use httpx. A timeout tuple works with both.
        sdk_timeout = (_sdk_timeout.connect, _sdk_timeout.read, _sdk_timeout.write, _sdk_timeout.pool)
        official_client_kw["timeout"] = sdk_timeout
        self._http_client_sync = DefaultHttpxClient(timeout=sdk_timeout, trust_env=False)
        self._http_client_async = DefaultAsyncHttpxClient(timeout=sdk_timeout, trust_env=False)
        self.client = Anthropic(**official_client_kw, http_client=self._http_client_sync)
        self.async_client = AsyncAnthropic(**official_client_kw, http_client=self._http_client_async)

        # 流式端点专用 httpx client（长生命周期，跨请求复用连接池）
        self._stream_http_client = httpx.AsyncClient(
            timeout=build_httpx_timeout(self.settings.http_timeout_settings),
            trust_env=False,
        )

        # 兼容旧字段：若其他模块引用，保留归一化后的值
        self.proxy_base_url = base
    async def generate(
        self,
        prompt: Prompt,
        config: GenerationConfig
    ) -> GenerationResult:
        """生成文本

        Args:
            prompt: 提示词
            config: 生成配置

        Returns:
            生成结果

        Raises:
            RuntimeError: 当 API 调用失败或返回空内容时
        """
        try:
            model_id = require_resolved_model_id(
                config.model,
                self.settings.default_model,
                provider_label="Anthropic / Claude",
            )
            self._validate_output_tokens(config, model_id, protocol='anthropic')
            # Use the same builder as streaming. Previously only the streaming
            # path merged ``extra_body``; a thinking setting therefore vanished
            # on ordinary calls and looked as if the control had been ignored.
            _, create_kwargs = self._build_message_request(prompt, config, stream=False)
            receipt = dict(self.last_request_receipt)

            # 使用 async_client 避免阻塞 asyncio 事件循环
            self._validate_context_window(create_kwargs, config, model_id, protocol='anthropic')
            response = await self.async_client.messages.create(**create_kwargs)
            token_usage = parse_usage(getattr(response, 'usage', None))
            record_response_usage(token_usage)
            stop_reason = getattr(response, 'stop_reason', None)
            if stop_reason not in {'end_turn', 'stop_sequence'}:
                raise RuntimeError(f'Anthropic completion did not finish normally: {stop_reason}')

            # 防御性检查：验证 content 列表非空
            if not response.content:
                raise RuntimeError("API returned empty content")

            parts = []
            for block in response.content:
                text = _extract_text_from_content_block(block)
                if text:
                    parts.append(text)

            content = "\n".join(part.strip() for part in parts if part and part.strip()).strip()
            if not content:
                raise RuntimeError("API returned no text content")

            # 创建 token 使用统计
            token_usage = parse_usage(getattr(response, 'usage', None))

            return self._result_with_receipt(GenerationResult(content=content, token_usage=token_usage), receipt)

        except RuntimeError:
            raise
        except Exception as e:
            raise RuntimeError(f"Failed to generate text: {str(e)}") from e

    def _build_message_request(
        self,
        prompt: Prompt,
        config: GenerationConfig,
        *,
        stream: bool = False,
    ) -> tuple[str, dict[str, Any]]:
        """构建 Messages API 请求体，供 generate / stream 共用。"""
        model_id = require_resolved_model_id(
            config.model,
            self.settings.default_model,
            provider_label="Anthropic / Claude",
        )
        self._validate_output_tokens(config, model_id, protocol='anthropic')
        payload: dict[str, Any] = {
            "model": model_id,
            "max_tokens": config.max_tokens,
            "system": prompt.system,
            "messages": [{"role": "user", "content": prompt.user}],
        }
        if config.reasoning_effort not in (None, "none"):
            raise ValueError(
                "Anthropic Messages uses thinking_budget_tokens; reasoning_effort is not supported"
            )
        if config.thinking_budget_tokens is not None:
            budget = config.thinking_budget_tokens
            if budget == 0:
                payload['thinking'] = {'type': 'disabled'}
                payload['temperature'] = config.temperature
            else:
                if budget < 1024 or budget >= config.max_tokens:
                    raise ValueError('Anthropic thinking budget must be at least 1024 and less than max_tokens')
                payload["thinking"] = {"type": "enabled", "budget_tokens": budget}
            # Extended thinking requests must not be combined with a sampling
            # temperature. The API applies its constrained default.
        else:
            payload["temperature"] = config.temperature
            if config.reasoning_effort == 'none':
                payload['thinking'] = {'type': 'disabled'}
        if config.top_p is not None:
            if payload.get('thinking', {}).get('type') == 'enabled':
                raise ValueError('top_p is unavailable with enabled Anthropic thinking')
            payload['top_p'] = config.top_p
        if config.stop_sequences:
            payload['stop_sequences'] = config.stop_sequences
        if config.response_format:
            instruction = _json_response_instruction(config.response_format)
            if instruction:
                payload["system"] = payload["system"] + instruction
        if stream:
            payload["stream"] = True
        # Provider extensions remain available, but cannot silently replace the
        # resolved request. This makes non-stream and stream behavior identical.
        for key, value in (self.settings.extra_body or {}).items():
            if key in {'messages', 'system', 'stream', 'input', 'instructions', 'response_format',
                       'tools', 'tool_choice'}:
                raise ValueError(f'extra_body cannot supply protected request field {key}')
            if key in {'top_p', 'stop_sequences'}:
                raise ValueError(f'extra_body {key} must use the explicit generation control')
            if key == 'temperature' and payload.get('thinking', {}).get('type') == 'enabled':
                raise ValueError('extra_body temperature cannot be combined with enabled thinking')
            if key in payload and payload[key] != value:
                raise ValueError(f"extra_body cannot override {key}")
            payload.setdefault(key, value)
        thinking = payload.get('thinking') or {}
        if thinking.get('type') == 'enabled':
            if config.top_p is not None:
                raise ValueError('top_p is unavailable with enabled Anthropic thinking')
            budget = thinking.get('budget_tokens')
            if isinstance(budget, bool) or not isinstance(budget, int) or budget < 1024 or budget >= config.max_tokens:
                raise ValueError('Anthropic thinking budget must be at least 1024 and less than max_tokens')
            if 'thinking' in (self.settings.extra_body or {}) and config.thinking_budget_tokens is None:
                payload.pop('temperature', None)
        self._validate_context_window(payload, config, model_id, protocol='anthropic')
        self._record_request_receipt(payload, config, endpoint='anthropic.messages',
            **({'structured_output': 'prompt_json_instruction'} if config.response_format else {}))
        return model_id, payload

    @staticmethod
    def _format_stream_error(exc: BaseException) -> str:
        message = str(exc).strip()
        if message:
            return f"{type(exc).__name__}: {message}"
        return f"{type(exc).__name__}: {exc!r}"

    async def _stream_via_httpx(
        self,
        prompt: Prompt,
        config: GenerationConfig,
    ) -> AsyncIterator[str]:
        """通过 httpx 直接解析 SSE，兼容部分网关的非标准流式响应。"""
        url = anthropic_messages_url(self.settings.base_url)
        logger.debug("[Stream] Using httpx endpoint: %s", url)

        headers = {
            "x-api-key": self.settings.api_key,
            "anthropic-version": "2023-06-01",
            "Content-Type": "application/json",
            "Accept": "text/event-stream",
            "User-Agent": "claude-cli/2.1.87 (external, cli)",
            **(self.settings.extra_headers or {}),
        }
        _, payload = self._build_message_request(prompt, config, stream=True)

        logger.debug("[Stream] Calling %s", url)
        async with self._stream_http_client.stream(
            "POST",
            url,
            headers=headers,
            params=self.settings.extra_query or None,
            json=payload,
        ) as response:
            if response.status_code != 200:
                error_body = await response.aread()
                raise RuntimeError(
                    f"API error {response.status_code}: {error_body.decode(errors='replace')}"
                )

            buffer = ""
            events_received = 0
            completed = False
            async with aclosing(response.aiter_text()) as chunks:
                async for chunk in chunks:
                    buffer += chunk.replace("\r\n", "\n")
                    while "\n\n" in buffer:
                        event_text, buffer = buffer.split("\n\n", 1)
                        events_received += 1
                        text_content = self._parse_sse_event(event_text)
                        completed = self._sse_event_completed(event_text) or completed
                        if events_received <= 3:
                            logger.info("[Stream] SSE event #%d event_chars=%d text_chars=%d",
                                        events_received, len(event_text), len(text_content))
                        if text_content:
                            yield text_content
            if buffer.strip():
                text_content = self._parse_sse_event(buffer)
                completed = self._sse_event_completed(buffer) or completed
                if text_content:
                    yield text_content
            if not completed:
                raise RuntimeError('Anthropic SSE ended before message_stop or a compatible completion marker')

    async def _stream_via_sdk(
        self,
        prompt: Prompt,
        config: GenerationConfig,
    ) -> AsyncIterator[str]:
        """通过官方 SDK 流式读取，网关断开 raw SSE 时作为回退。"""
        model_id, payload = self._build_message_request(prompt, config, stream=False)
        logger.info("[Stream] Falling back to SDK stream for model=%s", model_id)
        async with self.async_client.messages.stream(**payload) as stream:
            async for text in stream.text_stream:
                if text:
                    yield text
            if hasattr(stream, 'get_final_message'):
                message = await stream.get_final_message()
                record_stream_usage(getattr(message, 'usage', None))
                reason = getattr(message, 'stop_reason', None)
                if reason not in {'end_turn', 'stop_sequence'}:
                    raise RuntimeError(f'Anthropic SDK stream did not finish normally: {reason}')

    @staticmethod
    def _sse_event_completed(event_text: str) -> bool:
        data = '\n'.join(line[5:].strip() for line in event_text.splitlines() if line.startswith('data:'))
        if data == '[DONE]':
            return True
        try:
            parsed = json.loads(data)
        except (ValueError, TypeError):
            return False
        if not isinstance(parsed, dict):
            return False
        reason = (parsed.get('delta') or {}).get('stop_reason')
        if reason and reason not in {'end_turn', 'stop_sequence'}:
            raise RuntimeError(f'Anthropic SSE did not finish normally: {reason}')
        choices = parsed.get('choices') or []
        if choices and choices[0].get('finish_reason'):
            reason = choices[0]['finish_reason']
            if reason != 'stop':
                raise RuntimeError(f'Compatible SSE did not finish normally: {reason}')
            return True
        return parsed.get('type') == 'message_stop'

    async def stream_generate(
        self,
        prompt: Prompt,
        config: GenerationConfig
    ) -> AsyncIterator[str]:
        """流式生成内容。

        优先 httpx 解析 SSE（兼容部分代理）；若连接被网关提前断开或零输出，
        自动回退到 Anthropic SDK 的 stream API。
        """
        httpx_error: Exception | None = None
        yielded_any = False

        try:
            async for chunk in self._stream_via_httpx(prompt, config):
                yielded_any = True
                yield chunk
        except Exception as e:
            httpx_error = e
            logger.warning(
                "[Stream] httpx SSE failed (%s), will try SDK fallback",
                self._format_stream_error(e),
            )

        if yielded_any:
            if httpx_error is not None:
                raise RuntimeError(f'Stream interrupted after partial output: {self._format_stream_error(httpx_error)}') from httpx_error
            return

        capture = current_usage()
        if capture is not None and capture.metadata.get('budget_enforced') and capture.input_tokens is not None:
            raise RuntimeError('No complete output; automatic SDK fallback disabled while a book budget is enforced')

        sdk_yielded = False
        try:
            async for chunk in self._stream_via_sdk(prompt, config):
                sdk_yielded = True
                yield chunk
        except Exception as sdk_error:
            sdk_detail = self._format_stream_error(sdk_error)
            if httpx_error is not None:
                httpx_detail = self._format_stream_error(httpx_error)
                logger.error(
                    "[Stream] Failed: httpx=%s; sdk=%s",
                    httpx_detail,
                    sdk_detail,
                )
                raise RuntimeError(
                    f"Failed to stream text: httpx={httpx_detail}; sdk={sdk_detail}"
                ) from sdk_error
            logger.error("[Stream] Failed: %s", sdk_detail)
            raise RuntimeError(f"Failed to stream text: {sdk_detail}") from sdk_error

        if not sdk_yielded:
            model_id = config.model or self.settings.default_model
            detail = (
                f"httpx={self._format_stream_error(httpx_error)}"
                if httpx_error
                else "httpx returned no events"
            )
            raise RuntimeError(
                f"Both streaming paths produced zero output for model={model_id}: {detail}"
            )

    def _parse_sse_event(self, event_text: str) -> str:
        """解析单个 SSE 事件，返回文本内容（如果有）。

        兼容多种 SSE 格式：
        - Anthropic 原生: {"type":"content_block_delta","delta":{"type":"text_delta","text":"..."}}
        - OpenAI 兼容:  {"choices":[{"delta":{"content":"..."}}]}
        - 通用 delta:    {"delta":{"text":"..."}} 或 {"text":"..."}
        """
        lines = event_text.strip().split("\n")
        data = None

        for line in lines:
            if line.startswith("data:"):
                data = line[5:].strip()

        if not data:
            return ""

        try:
            parsed = json.loads(data)
        except json.JSONDecodeError:
            return ""

        if parsed.get('type') == 'error':
            raise RuntimeError('Anthropic SSE reported an upstream error')
        if parsed.get('type') in {'thinking', 'thinking_delta', 'redacted_thinking', 'reasoning'}:
            return ''
        message_usage = (parsed.get('message') or {}).get('usage')
        if message_usage:
            # message_start's output=0 is provisional; the final message_delta
            # supplies the actual billed output count.
            record_stream_usage({'input_tokens': message_usage.get('input_tokens')})
        if parsed.get('type') == 'message_delta' and parsed.get('usage'):
            record_stream_usage(parsed['usage'])
        elif parsed.get('usage') and parsed.get('choices') is not None:
            record_stream_usage(parsed['usage'], 'prompt_tokens', 'completion_tokens')

        # Anthropic 原生
        if parsed.get("type") == "content_block_delta":
            delta = parsed.get("delta", {})
            if delta.get("type") == "text_delta":
                return delta.get("text", "")
            return ''

        # Anthropic content_block_start (某些模型把 text 放在这里)
        if parsed.get("type") == "content_block_start":
            block = parsed.get("content_block", {})
            if block.get("type") == "text" and block.get("text"):
                return block["text"]

        # OpenAI / DeepSeek 兼容格式
        choices = parsed.get("choices", [])
        if choices:
            delta = choices[0].get("delta", {})
            content = delta.get("content", "")
            if content:
                return content
            # 某些变体
            text = delta.get("text", "")
            if text:
                return text

        # 通用 fallback: 一层 delta.text 或 delta.content
        delta = parsed.get("delta", {})
        if isinstance(delta, dict):
            text = delta.get("text") or delta.get("content")
            if text:
                return text

        # 最通用: 顶层 text/content 字段
        text = parsed.get("text") or parsed.get("content")
        if isinstance(text, str) and text.strip():
            return text

        return ""
