"""张力评分：LLM JSON 契约、校验与 OpenAI-style tool 定义。

设计要点（与 knowledge_llm_contract / chapter_state_llm_contract 同源）：
- 保留 Pydantic 解析层兼容性；输出 schema 单独满足严格结构化输出要求
- 日后 provider 支持 function calling 时，可直接把
  tension_scoring_openai_function_tool() 交给网关
"""
from __future__ import annotations

import re
from copy import deepcopy
from typing import Any, Dict, List, Optional, Tuple

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

from application.ai.llm_json_extract import parse_llm_json_to_dict
from domain.novel.value_objects.tension_dimensions import TensionDimensions


# ---------------------------------------------------------------------------
# 与 LLM 约定的响应形状
# ---------------------------------------------------------------------------


class TensionScoringLlmPayload(BaseModel):
    """张力评分 LLM 响应根对象。

    仅允许六个字段：三个维度分数 + 三条依据。
    composite_score 由服务端加权计算，不由模型输出。
    """

    model_config = ConfigDict(extra="ignore")

    plot_tension: float = Field(ge=0, le=100, description="情节张力 0-100")
    emotional_tension: float = Field(ge=0, le=100, description="情绪张力 0-100")
    pacing_tension: float = Field(ge=0, le=100, description="节奏张力 0-100")
    plot_justification: str = Field(default="", max_length=500)
    emotional_justification: str = Field(default="", max_length=500)
    pacing_justification: str = Field(default="", max_length=500)

    @field_validator("plot_tension", "emotional_tension", "pacing_tension", mode="before")
    @classmethod
    def coerce_to_float(cls, v):
        if isinstance(v, (int, float)):
            return float(v)
        if isinstance(v, str):
            s = v.strip()
            if not s:
                return 0.0
            try:
                return float(s)
            except ValueError:
                pass
            # 模型常把长评语误写入数值字段：取首个数字，否则中性分
            m = re.search(r"(\d+(?:\.\d+)?)", s)
            if m:
                x = float(m.group(1))
                return min(100.0, max(0.0, x))
            return 50.0
        return v


# ---------------------------------------------------------------------------
# 解析 + 校验
# ---------------------------------------------------------------------------


def parse_tension_scoring_llm_response(
    raw: str,
) -> Tuple[Optional[TensionScoringLlmPayload], List[str]]:
    """解析并校验 LLM 返回文本。

    Returns:
        (payload, []) 成功；  (None, [错误…]) 失败。
    """
    data, errs = parse_llm_json_to_dict(raw)
    if data is None:
        return None, errs
    try:
        payload = TensionScoringLlmPayload.model_validate(data)
        return payload, []
    except ValidationError as e:
        err_list = e.errors()
        msg = "; ".join(
            f"{'/'.join(str(x) for x in err.get('loc', ()))}: {err.get('msg', '')}"
            for err in err_list[:12]
        )
        return None, [msg or str(e)]


def tension_scoring_payload_to_domain(
    payload: TensionScoringLlmPayload,
) -> TensionDimensions:
    """将校验后的 payload 转为领域值对象（自动计算综合分）。"""
    return TensionDimensions.from_raw_scores(
        plot=payload.plot_tension,
        emotional=payload.emotional_tension,
        pacing=payload.pacing_tension,
    )


# ---------------------------------------------------------------------------
# response_format 构建器（供 GenerationConfig 使用）
# ---------------------------------------------------------------------------


def _tension_scoring_wire_schema() -> Dict[str, Any]:
    """Close every wire object and require every property for OpenAI strict mode.

    The parser intentionally tolerates legacy extras and missing justifications.
    Those parser defaults must not make properties optional in the wire schema.
    Work on a copy so building a request cannot change local validation behavior.
    """
    schema = deepcopy(TensionScoringLlmPayload.model_json_schema(mode="validation"))

    def close(node: Any) -> None:
        if not isinstance(node, dict):
            return
        node.pop("default", None)
        properties = node.get("properties")
        if node.get("type") == "object" or isinstance(properties, dict):
            node["additionalProperties"] = False
            node["required"] = list(properties or {})
        # Visit schema nodes, not arbitrary dictionaries: a property actually
        # named "default" or "type" must remain a property of its parent.
        for key in ("properties", "$defs", "definitions"):
            for child in (node.get(key) or {}).values():
                close(child)
        for key in ("items", "not"):
            close(node.get(key))
        for key in ("anyOf", "oneOf", "allOf", "prefixItems"):
            for child in node.get(key) or []:
                close(child)

    close(schema)
    return schema


def tension_scoring_response_format() -> Dict[str, Any]:
    """构建统一 GenerationConfig 的结构化输出契约。

    具体 provider 负责把契约适配为自身支持的形式：OpenAI 可传
    response_format，Anthropic 则转为 prompt 约束后由解析层校验。

    用法::

        config = GenerationConfig(
            max_tokens=512,
            temperature=0.3,
            response_format=tension_scoring_response_format(),
        )
    """
    return {
        "type": "json_schema",
        "json_schema": {
            "name": "tension_scoring",
            "description": (
                "章节多维张力评分：情节张力、情绪张力、节奏张力各 0-100，"
                "附带一句话依据。composite_score 由服务端计算，模型不要输出。"
            ),
            "schema": _tension_scoring_wire_schema(),
            "strict": True,
        },
    }


# ---------------------------------------------------------------------------
# OpenAI function tool 定义（预留）
# ---------------------------------------------------------------------------


def tension_scoring_openai_function_tool() -> Dict[str, Any]:
    """可选：接入 function calling 时使用。"""
    schema = _tension_scoring_wire_schema()
    return {
        "type": "function",
        "function": {
            "name": "submit_tension_scoring",
            "description": (
                "提交章节多维张力评分：情节张力、情绪张力、节奏张力各 0-100，"
                "附带一句话依据。composite_score 由服务端计算，模型不要输出。"
            ),
            "parameters": schema,
        },
    }
