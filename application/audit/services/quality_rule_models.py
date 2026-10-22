"""Bounded declarative quality rules; user rules match literal text only."""
from __future__ import annotations

from typing import Literal
from pydantic import BaseModel, ConfigDict, Field, field_validator


class LiteralQualityRule(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True)
    name: str = Field(min_length=1, max_length=80)
    text: str = Field(min_length=1, max_length=200)
    severity: Literal['critical', 'warning', 'info'] = 'warning'
    category: str = Field(default='自定义', max_length=80)
    replacement_hint: str = Field(default='', max_length=500)

    @field_validator('name', 'text')
    @classmethod
    def meaningful(cls, value):
        if not value.strip():
            raise ValueError('规则名与匹配文本不能为空白')
        return value


class QualityRuleConfig(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True)
    disabled_builtin: list[str] = Field(default_factory=list, max_length=100)
    rules: list[LiteralQualityRule] = Field(default_factory=list, max_length=100)

    @field_validator('rules')
    @classmethod
    def unique_names(cls, value):
        if len({rule.name for rule in value}) != len(value):
            raise ValueError('自定义规则名不能重复')
        return value

