"""AI 配置设置"""
from dataclasses import dataclass, field
from typing import Any, Optional

from domain.ai.services.llm_service import DEFAULT_MAX_OUTPUT_TOKENS, GenerationConfig
import math
from infrastructure.ai.http_timeout import DEFAULT_HTTP_TIMEOUT_SETTINGS, HttpTimeoutSettings


@dataclass
class Settings:
    """AI 配置设置

    管理 LLM 提供商的配置参数。
    """

    default_model: str = ""
    default_temperature: float = 0.7
    default_max_tokens: int = DEFAULT_MAX_OUTPUT_TOKENS
    api_key: Optional[str] = None
    #: 兼容自建/转发网关，与官方 provider base_url 一致；未设则走官方默认
    base_url: Optional[str] = None
    timeout_seconds: float = DEFAULT_HTTP_TIMEOUT_SETTINGS.timeout_seconds
    #: 连接超时（秒）：建立 TCP 连接的最大等待时间。设短可快速发现网络不可达
    connect_timeout: float = DEFAULT_HTTP_TIMEOUT_SETTINGS.connect_timeout
    #: 读取超时（秒）：等待服务端响应（首个字节）的最大时间。流式场景下指两个 chunk 之间的间隔
    read_timeout: float = DEFAULT_HTTP_TIMEOUT_SETTINGS.read_timeout
    #: 写入超时（秒）：请求体发送的最大等待时间
    write_timeout: float = DEFAULT_HTTP_TIMEOUT_SETTINGS.write_timeout
    #: 连接池等待超时（秒）：避免高并发时无限等待可用连接
    pool_timeout: float = DEFAULT_HTTP_TIMEOUT_SETTINGS.pool_timeout
    extra_headers: dict[str, str] = field(default_factory=dict)
    extra_query: dict[str, Any] = field(default_factory=dict)
    extra_body: dict[str, Any] = field(default_factory=dict)
    provider_name: Optional[str] = None
    protocol: Optional[str] = None
    use_legacy_chat_completions: bool = False
    endpoint_kind: str = 'auto'
    auth_mode: str = 'api_key'
    reasoning_effort: str | None = None
    thinking_budget_tokens: int | None = None
    top_p: float | None = None
    stop_sequences: list[str] = field(default_factory=list)
    #: Explicit capabilities for custom/model IDs absent from the registry.
    max_output_tokens_limit: Optional[int] = None
    context_window_limit: Optional[int] = None
    profile_id: str = ''
    currency: str = 'USD'
    input_price_per_million: str | None = None
    output_price_per_million: str | None = None

    def __post_init__(self):
        """验证配置参数"""
        if (isinstance(self.default_temperature, bool) or not isinstance(self.default_temperature, (int, float))
                or not math.isfinite(self.default_temperature) or not (0.0 <= self.default_temperature <= 2.0)):
            raise ValueError("Temperature must be between 0.0 and 2.0")

        if (isinstance(self.default_max_tokens, bool) or not isinstance(self.default_max_tokens, (int, float))
                or not math.isfinite(self.default_max_tokens) or int(self.default_max_tokens) != self.default_max_tokens
                or self.default_max_tokens <= 0):
            raise ValueError("Max tokens must be positive")
        self.default_max_tokens = int(self.default_max_tokens)
        # Keep direct Settings callers subject to the same typed model options.
        GenerationConfig(reasoning_effort=self.reasoning_effort, thinking_budget_tokens=self.thinking_budget_tokens,
                         top_p=self.top_p, stop_sequences=self.stop_sequences)
        for name in ('max_output_tokens_limit', 'context_window_limit'):
            limit = getattr(self, name)
            if limit is not None and (isinstance(limit, bool) or not isinstance(limit, int) or limit <= 0):
                raise ValueError(f'{name} must be a positive integer')

        if self.timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be positive")
        if self.connect_timeout <= 0:
            raise ValueError("connect_timeout must be positive")
        if self.read_timeout <= 0:
            raise ValueError("read_timeout must be positive")
        if self.write_timeout <= 0:
            raise ValueError("write_timeout must be positive")
        if self.pool_timeout <= 0:
            raise ValueError("pool_timeout must be positive")

    @property
    def http_timeout_settings(self) -> HttpTimeoutSettings:
        return HttpTimeoutSettings(
            timeout_seconds=self.timeout_seconds,
            connect_timeout=self.connect_timeout,
            read_timeout=self.read_timeout,
            write_timeout=self.write_timeout,
            pool_timeout=self.pool_timeout,
        )
