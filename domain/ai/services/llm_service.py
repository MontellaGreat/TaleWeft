from abc import ABC, abstractmethod
from typing import AsyncIterator, Dict, List, Optional
import math
from domain.ai.value_objects.prompt import Prompt
from domain.ai.value_objects.token_usage import TokenUsage


DEFAULT_MAX_OUTPUT_TOKENS = 4096


class GenerationConfig:
    """生成配置"""
    def __init__(
        self,
        model: str = "",
        max_tokens: Optional[int] = None,
        temperature: Optional[float] = None,
        response_format: Optional[Dict] = None,
        reasoning_effort: Optional[str] = None,
        thinking_budget_tokens: Optional[int] = None,
        *,
        temperature_explicit: Optional[bool] = None,
        top_p: Optional[float] = None,
        stop_sequences: Optional[List[str]] = None,
    ):
        self.model = model
        self.max_tokens_explicit = max_tokens is not None
        self.max_tokens = DEFAULT_MAX_OUTPUT_TOKENS if max_tokens is None else max_tokens
        self.temperature_explicit = temperature is not None if temperature_explicit is None else temperature_explicit
        self.temperature = 1.0 if temperature is None else temperature
        self.response_format = response_format
        self.reasoning_effort = reasoning_effort.strip().lower() if isinstance(reasoning_effort, str) else reasoning_effort
        self.thinking_budget_tokens = thinking_budget_tokens
        self.top_p = top_p
        self.stop_sequences = list(stop_sequences) if isinstance(stop_sequences, list) else stop_sequences
        self.__post_init__()

    def __post_init__(self):
        """验证配置参数"""
        if isinstance(self.temperature, bool) or not isinstance(self.temperature, (int, float)) or not math.isfinite(self.temperature) or not (0.0 <= self.temperature <= 2.0):
            raise ValueError("Temperature must be between 0.0 and 2.0")
        if isinstance(self.max_tokens, bool) or not isinstance(self.max_tokens, (int, float)) or not math.isfinite(self.max_tokens) or int(self.max_tokens) != self.max_tokens or self.max_tokens <= 0:
            raise ValueError("max_tokens must be greater than 0 and a positive integer")
        self.max_tokens = int(self.max_tokens)
        if self.response_format is not None:
            if not isinstance(self.response_format, dict) or self.response_format.get('type') not in {'text', 'json_object', 'json_schema'}:
                raise ValueError('response_format must specify text, json_object, or json_schema')
            if self.response_format['type'] == 'json_schema':
                schema = self.response_format.get('json_schema')
                if not isinstance(schema, dict) or not isinstance(schema.get('schema'), dict):
                    raise ValueError('json_schema requires a schema object')
        if self.top_p is not None and (isinstance(self.top_p, bool) or not isinstance(self.top_p, (int, float))
                                     or not math.isfinite(self.top_p) or not 0.0 <= self.top_p <= 1.0):
            raise ValueError("top_p must be a finite number between 0.0 and 1.0")
        if self.stop_sequences is not None:
            if (not isinstance(self.stop_sequences, list) or len(self.stop_sequences) > 16
                    or any(not isinstance(item, str) or not item or len(item) > 1024 for item in self.stop_sequences)):
                raise ValueError("stop_sequences must contain at most 16 non-empty strings of at most 1024 characters")
        supported_efforts = {"none", "minimal", "low", "medium", "high", "xhigh", "max"}
        if self.reasoning_effort is not None:
            if not isinstance(self.reasoning_effort, str) or self.reasoning_effort not in supported_efforts:
                choices = ", ".join(sorted(supported_efforts))
                raise ValueError(f"reasoning_effort must be one of: {choices}")
        if self.thinking_budget_tokens is not None:
            if (
                isinstance(self.thinking_budget_tokens, bool)
                or not isinstance(self.thinking_budget_tokens, int)
                or self.thinking_budget_tokens < 0
            ):
                raise ValueError("thinking_budget_tokens must be a non-negative integer")
        if self.reasoning_effort is not None and self.thinking_budget_tokens is not None:
            raise ValueError("Set either reasoning_effort or thinking_budget_tokens, not both")


class GenerationResult:
    """生成结果"""
    def __init__(self, content: str, token_usage: TokenUsage | None):
        self.content = content
        self.token_usage = token_usage
        self.__post_init__()

    def __post_init__(self):
        """验证结果参数"""
        if not self.content or not self.content.strip():
            raise ValueError("Content cannot be empty")


class LLMService(ABC):
    """LLM 服务接口（领域服务）"""

    @abstractmethod
    async def generate(
        self,
        prompt: Prompt,
        config: GenerationConfig
    ) -> GenerationResult:
        """生成内容"""
        pass

    @abstractmethod
    async def stream_generate(
        self,
        prompt: Prompt,
        config: GenerationConfig
    ) -> AsyncIterator[str]:
        """流式生成内容"""
        pass
