from __future__ import annotations

import json
import logging
from decimal import Decimal, InvalidOperation
from time import perf_counter
from typing import Any, Callable, Dict, List, Literal, Optional

from pydantic import BaseModel, Field, ConfigDict, ValidationError, field_validator, model_validator

from domain.ai.services.llm_service import DEFAULT_MAX_OUTPUT_TOKENS
from infrastructure.ai.llm_environment import LLMEnvironmentSettings
from infrastructure.persistence.database.connection import get_database
from infrastructure.security.credential_store import PREFIX, credentials_for_database
from application.ai.credential_management_service import credential_operation_lock
from infrastructure.ai.url_utils import (
    normalize_anthropic_base_url,
    normalize_gemini_base_url,
    normalize_openai_base_url,
)
from infrastructure.ai.providers.generation_constraints import (
    anthropic_thinking, validate_anthropic_thinking_budget, validate_generation_constraints,
)

logger = logging.getLogger(__name__)

LLMProtocol = Literal['openai', 'anthropic', 'gemini']
LLMEndpointMode = Literal['unified', 'independent']
PROFILE_OPTION_FIELDS = ('max_output_tokens_limit', 'context_window_limit', 'currency',
                         'input_price_per_million', 'output_price_per_million',
                         'reasoning_effort', 'thinking_budget_tokens', 'endpoint_kind', 'auth_mode',
                         'top_p', 'stop_sequences')


class LLMPreset(BaseModel):
    key: str
    label: str
    protocol: LLMProtocol
    endpoint_kind: Literal['auto', 'responses', 'chat'] = 'auto'
    default_base_url: str = ''
    default_model: str = ''
    description: str = ''
    tags: List[str] = Field(default_factory=list)


class LLMProfile(BaseModel):
    model_config = ConfigDict(extra='forbid')

    id: str
    name: str
    preset_key: str = 'custom-openai-compatible'
    protocol: LLMProtocol = 'openai'
    base_url: str = ''
    api_key: str = ''
    model: str = ''
    temperature: float = 0.7
    max_tokens: int = DEFAULT_MAX_OUTPUT_TOKENS
    timeout_seconds: int = 300
    extra_headers: Dict[str, str] = Field(default_factory=dict)
    extra_query: Dict[str, Any] = Field(default_factory=dict)
    extra_body: Dict[str, Any] = Field(default_factory=dict)
    notes: str = ''
    use_legacy_chat_completions: bool = False
    endpoint_kind: Literal['auto', 'responses', 'chat'] = 'auto'
    auth_mode: Literal['api_key', 'none'] = 'api_key'
    reasoning_effort: Literal['none', 'minimal', 'low', 'medium', 'high', 'xhigh', 'max'] | None = None
    thinking_budget_tokens: int | None = Field(default=None, ge=0, strict=True)
    top_p: float | None = Field(default=None, ge=0, le=1, strict=True)
    stop_sequences: List[str] = Field(default_factory=list, max_length=16)
    max_output_tokens_limit: int | None = Field(default=None, gt=0)
    context_window_limit: int | None = Field(default=None, gt=0)
    currency: str = 'USD'
    input_price_per_million: str | None = None
    output_price_per_million: str | None = None

    @model_validator(mode='after')
    def _validate_generation_options(self):
        if self.reasoning_effort is not None and self.thinking_budget_tokens is not None:
            raise ValueError('只能设置思考程度或思考预算中的一项')
        if self.protocol != 'openai' and self.endpoint_kind != 'auto':
            raise ValueError('Responses / Chat 端点仅用于 OpenAI 协议')
        if self.auth_mode == 'none' and (self.protocol != 'openai' or not self.base_url.strip()):
            raise ValueError('免密钥模式需要明确的 OpenAI 兼容服务地址')
        if self.protocol == 'openai' and self.thinking_budget_tokens is not None:
            raise ValueError('OpenAI 协议请使用 reasoning_effort')
        if self.protocol != 'openai' and self.reasoning_effort is not None:
            raise ValueError('此适配器请使用 thinking_budget_tokens')
        if self.protocol == 'anthropic' and self.thinking_budget_tokens not in (None, 0):
            validate_anthropic_thinking_budget(self.thinking_budget_tokens, self.max_tokens)
        return self

    @field_validator('currency')
    @classmethod
    def _currency(cls, value):
        value = value.strip().upper()
        if len(value) != 3 or not value.isascii() or not value.isalpha():
            raise ValueError('currency must be a three-letter code')
        return value

    @field_validator('stop_sequences', mode='before')
    @classmethod
    def _validate_stops(cls, value):
        if not isinstance(value, list) or len(value) > 16:
            raise ValueError('stop_sequences must be a list with at most 16 items')
        if any(not isinstance(item, str) or not item or len(item) > 1024 for item in value):
            raise ValueError('stop_sequences must contain non-empty strings of at most 1024 characters')
        return value

    @field_validator('input_price_per_million', 'output_price_per_million', mode='before')
    @classmethod
    def _price(cls, value):
        if value is None or value == '':
            return None
        try:
            amount = Decimal(str(value))
        except InvalidOperation as exc:
            raise ValueError('price must be a valid decimal') from exc
        if not amount.is_finite() or amount < 0 or amount > 1_000_000_000:
            raise ValueError('price must be finite and non-negative')
        return format(amount, 'f')

    @field_validator('temperature')
    @classmethod
    def _validate_temperature(cls, value: float) -> float:
        if not 0 <= value <= 2:
            raise ValueError('temperature must be between 0 and 2')
        return value

    @field_validator('max_tokens')
    @classmethod
    def _validate_max_tokens(cls, value: int) -> int:
        if value <= 0:
            raise ValueError('value must be positive')
        return value

    @field_validator('timeout_seconds')
    @classmethod
    def _validate_positive_int(cls, value: int) -> int:
        if value <= 0:
            raise ValueError('value must be positive')
        return value

    @field_validator('extra_headers')
    @classmethod
    def _normalize_headers(cls, value: Dict[str, str]) -> Dict[str, str]:
        return {
            str(k).strip(): str(v).strip()
            for k, v in (value or {}).items()
            if str(k).strip() and str(v).strip()
        }


class LLMControlConfig(BaseModel):
    version: int = 1
    active_profile_id: Optional[str] = None
    #: 统一 = 单档案主力端点；独立 = 主力 / 经济 / 知识图谱 等分档案（持久化在 llm_config_meta）
    endpoint_mode: LLMEndpointMode = 'unified'
    profiles: List[LLMProfile] = Field(default_factory=list)

    @model_validator(mode='after')
    def _validate_active_profile(self) -> 'LLMControlConfig':
        if not self.profiles:
            return self
        ids = [profile.id for profile in self.profiles]
        if not self.active_profile_id or self.active_profile_id not in ids:
            self.active_profile_id = ids[0]
        if self.endpoint_mode not in ('unified', 'independent'):
            self.endpoint_mode = 'unified'
        return self


class LLMRuntimeSummary(BaseModel):
    source: Literal['profile', 'unavailable']
    status: Literal['configured', 'unavailable']
    active_profile_id: Optional[str] = None
    active_profile_name: Optional[str] = None
    protocol: Optional[LLMProtocol] = None
    endpoint_kind: Optional[Literal['auto', 'responses', 'chat']] = None
    model: Optional[str] = None
    base_url: Optional[str] = None
    using_mock: bool = False
    reason: Optional[str] = None


class LLMControlPanelData(BaseModel):
    config: LLMControlConfig
    presets: List[LLMPreset]
    runtime: LLMRuntimeSummary


class LLMTestResult(BaseModel):
    ok: bool
    provider_label: str
    model: str
    latency_ms: int
    preview: str = ''
    error: Optional[str] = None
    effective_max_tokens: int | None = None
    usage_source: str = 'unknown'
    token_usage: Dict[str, int] | None = None
    requested_parameters: Dict[str, Any] = Field(default_factory=dict)
    submitted_parameters: Dict[str, Any] = Field(default_factory=dict)
    provider_confirmation_status: Literal['not_reported'] = 'not_reported'
    provider_confirmed_parameters: Dict[str, Any] = Field(default_factory=dict)


class LLMControlService:
    """LLM 控制面板服务 —— 配置全部持久化到 SQLite（llm_profiles + llm_config_meta 表）。"""

    # ---- 内部 DB 辅助 ------------------------------------------------

    def _db(self):
        return get_database()

    def _get_meta(self, key: str, default: str = '') -> str:
        row = self._db().fetch_one(
            "SELECT value FROM llm_config_meta WHERE key = ?", (key,)
        )
        return row['value'] if row else default

    def _set_meta(self, key: str, value: str) -> None:
        self._db().execute(
            "INSERT OR REPLACE INTO llm_config_meta (key, value) VALUES (?, ?)",
            (key, value),
        )

    def _row_to_profile(self, row: dict) -> LLMProfile:
        def reveal(value: str) -> str:
            return credentials_for_database(self._db()).resolve(value) if value.startswith(PREFIX) else value
        options = json.loads(self._get_meta('profile_options:' + row['id'], '{}'))
        options = {key: options[key] for key in PROFILE_OPTION_FIELDS if key in options}
        data = dict(
            id=row['id'],
            name=row['name'],
            preset_key=row['preset_key'],
            protocol=row['protocol'],
            base_url=row['base_url'] or '',
            api_key=reveal(row['api_key'] or ''),
            model=row['model'] or '',
            temperature=row['temperature'],
            max_tokens=int(row.get('max_tokens') or DEFAULT_MAX_OUTPUT_TOKENS),
            timeout_seconds=row['timeout_seconds'],
            extra_headers=json.loads(reveal(row.get('extra_headers') or '{}')),
            extra_query=json.loads(reveal(row.get('extra_query') or '{}')),
            extra_body=json.loads(reveal(row.get('extra_body') or '{}')),
            notes=row['notes'] or '',
            use_legacy_chat_completions=bool(row.get('use_legacy_chat_completions', 0)),
            **options,
        )
        try:
            return LLMProfile(**data)
        except ValidationError as exc:
            # A profile written by an older release may contain a now-known
            # unsupported effort.  Keep it readable so the editor can clear or
            # replace the value; save/test/provider paths still revalidate it.
            if 'reasoning_effort' not in str(exc).lower():
                raise
            return LLMProfile.model_construct(**data)

    def _profile_to_row(self, p: LLMProfile) -> dict:
        db = self._db()
        previous = db.fetch_one("SELECT * FROM llm_profiles WHERE id = ?", (p.id,)) or {}
        def protect(field: str, value: str) -> str:
            if value in ('', '{}'):
                return value
            vault = credentials_for_database(db)
            old = previous.get(field) or ''
            if old.startswith(PREFIX) and vault.resolve(old) == value:
                return old
            return vault.store(value)
        return dict(
            id=p.id,
            name=p.name,
            preset_key=p.preset_key,
            protocol=p.protocol,
            base_url=p.base_url,
            api_key=protect('api_key', p.api_key),
            model=p.model,
            temperature=p.temperature,
            max_tokens=p.max_tokens,
            timeout_seconds=p.timeout_seconds,
            extra_headers=protect('extra_headers', json.dumps(p.extra_headers, ensure_ascii=False)),
            extra_query=protect('extra_query', json.dumps(p.extra_query, ensure_ascii=False)),
            extra_body=protect('extra_body', json.dumps(p.extra_body, ensure_ascii=False)),
            notes=p.notes,
            use_legacy_chat_completions=int(p.use_legacy_chat_completions),
        )

    def _migrate_profile_credentials(self, row: dict) -> dict:
        fields = ('api_key', 'extra_headers', 'extra_query', 'extra_body')
        pending = {key: value for key in fields
                   if (value := row.get(key)) and value != '{}' and not value.startswith(PREFIX)}
        if not pending:
            return row
        db = self._db()
        with credential_operation_lock(db):
            vault = credentials_for_database(db)
            protected = {key: vault.store(value) for key, value in pending.items()}
            assignments = ', '.join(f'{key} = ?' for key in protected)
            conditions = ' AND '.join(f'{key} = ?' for key in pending)
            # A concurrent settings edit must not be overwritten by legacy migration.
            result = db.execute(
                f'UPDATE llm_profiles SET {assignments} WHERE id = ? AND {conditions}',
                (*protected.values(), row['id'], *pending.values()),
            )
            db.commit()
            if result.rowcount == 1:
                return dict(row, **protected)
            return db.fetch_one('SELECT * FROM llm_profiles WHERE id = ?', (row['id'],)) or row

    # ---- 公共接口（保持与原文件版一致）----------------------------------

    def get_presets(self) -> List[LLMPreset]:
        return [
            LLMPreset(
                key='custom-openai-compatible',
                label='OpenAI C 兼容 / 国产通用',
                protocol='openai',
                endpoint_kind='chat',
                default_base_url='',
                default_model='',
                description='固定使用 C（Chat Completions）协议。适用于支持 /chat/completions 的 OpenAI 兼容网关。',
                tags=['custom', 'openai-compatible', 'domestic'],
            ),
            LLMPreset(
                key='custom-openai-responses',
                label='OpenAI R 兼容 / Responses',
                protocol='openai',
                endpoint_kind='responses',
                description='固定使用 R（Responses）协议。请填写支持 /responses 的服务地址；不自动降级到 C。',
                tags=['custom', 'openai-compatible', 'responses'],
            ),
            LLMPreset(
                key='openai-official',
                label='OpenAI 官方 R / Responses',
                protocol='openai',
                endpoint_kind='responses',
                default_base_url='https://api.openai.com/v1',
                default_model='',
                description='OpenAI 官方 R（Responses）接口。导入后请填写模型 ID；也可在协议中改选 C。',
                tags=['official'],
            ),
            LLMPreset(
                key='claude-official',
                label='Claude / Anthropic 官方',
                protocol='anthropic',
                default_base_url='https://api.anthropic.com',
                default_model='',
                description='Anthropic Messages 接口；也可接入 Claude-compatible 网关。请填写网关文档中的模型 ID。',
                tags=['official'],
            ),
            LLMPreset(
                key='gemini-official',
                label='Gemini / Google 官方',
                protocol='gemini',
                default_base_url='https://generativelanguage.googleapis.com/v1beta',
                default_model='',
                description='Gemini generateContent / streamGenerateContent 接口。请填写 Google 文档中的模型 ID。',
                tags=['official'],
            ),
            LLMPreset(
                key='deepseek',
                label='DeepSeek',
                protocol='openai',
                endpoint_kind='chat',
                default_base_url='https://api.deepseek.com/v1',
                default_model='',
                description='DeepSeek 官方 OpenAI-compatible 接口。模型名以官方文档为准。',
                tags=['domestic', 'preset'],
            ),
            LLMPreset(
                key='qwen-dashscope',
                label='Qwen / DashScope',
                protocol='openai',
                endpoint_kind='chat',
                default_base_url='https://dashscope.aliyuncs.com/compatible-mode/v1',
                default_model='',
                description='阿里云百炼 / DashScope OpenAI-compatible 接口。模型名以控制台为准。',
                tags=['domestic', 'preset'],
            ),
            LLMPreset(
                key='glm-openai',
                label='智谱 GLM（OpenAI 兼容）',
                protocol='openai',
                endpoint_kind='chat',
                default_base_url='https://open.bigmodel.cn/api/paas/v4',
                default_model='',
                description='智谱 OpenAI-compatible 接口。模型名以智谱文档为准。',
                tags=['domestic', 'preset'],
            ),
            LLMPreset(
                key='glm-anthropic',
                label='智谱 GLM（Claude 兼容）',
                protocol='anthropic',
                default_base_url='https://open.bigmodel.cn/api/anthropic',
                default_model='',
                description='智谱 Anthropic-compatible 接口。模型名以智谱文档为准。',
                tags=['domestic', 'preset'],
            ),
            LLMPreset(
                key='doubao-ark',
                label='豆包 / 火山方舟 Ark',
                protocol='openai',
                endpoint_kind='chat',
                default_base_url='https://ark.cn-beijing.volces.com/api/v3',
                default_model='',
                description='方舟 OpenAI-compatible 接口；模型名以方舟控制台 Endpoint 为准。',
                tags=['domestic', 'preset'],
            ),
        ]

    def get_preset_map(self) -> Dict[str, LLMPreset]:
        return {preset.key: preset for preset in self.get_presets()}

    def get_control_panel_data(self) -> LLMControlPanelData:
        config = self.get_config()
        return LLMControlPanelData(
            config=config,
            presets=self.get_presets(),
            runtime=self.get_runtime_summary(config),
        )

    def get_config(self) -> LLMControlConfig:
        """从数据库读取完整配置；空库时自动写入初始默认值。"""
        try:
            rows = self._db().fetch_all(
                "SELECT * FROM llm_profiles ORDER BY sort_order, created_at"
            )
        except Exception as exc:
            logger.warning('读取 LLM profiles 失败，回退默认配置: %s', exc)
            rows = []

        profiles = [self._row_to_profile(self._migrate_profile_credentials(r)) for r in rows]

        if not profiles:
            config = self._build_initial_config()
            self.save_config(config)
            return config

        active_id = self._get_meta('active_profile_id')
        # 校验 active_id 是否在当前 profiles 中
        valid_ids = {p.id for p in profiles}
        if not active_id or active_id not in valid_ids:
            active_id = profiles[0].id

        endpoint_mode = self._get_meta('endpoint_mode', '')
        if endpoint_mode not in ('unified', 'independent'):
            endpoint_mode = self._infer_endpoint_mode_from_profiles(profiles)
        return LLMControlConfig(
            version=1,
            active_profile_id=active_id,
            endpoint_mode=endpoint_mode,  # type: ignore[arg-type]
            profiles=profiles,
        )

    def get_config_readonly(self) -> LLMControlConfig:
        """Read the effective configuration without creating rows or migrating secrets."""
        rows = self._db().fetch_all("SELECT * FROM llm_profiles ORDER BY sort_order, created_at")
        profiles = [self._row_to_profile(row) for row in rows]
        if not profiles:
            return self._build_initial_config()
        endpoint_mode = self._get_meta('endpoint_mode', '')
        if endpoint_mode not in ('unified', 'independent'):
            endpoint_mode = self._infer_endpoint_mode_from_profiles(profiles)
        return LLMControlConfig(
            active_profile_id=self._get_meta('active_profile_id') or None,
            endpoint_mode=endpoint_mode,
            profiles=profiles,
        )

    @staticmethod
    def _infer_endpoint_mode_from_profiles(profiles: List[LLMProfile]) -> LLMEndpointMode:
        """无 meta 时的向后兼容：存在「经济模型」「知识图谱模型」档案则视为独立端点 UI。"""
        for p in profiles:
            n = p.name or ''
            if '经济模型' in n or '知识图谱' in n or '知识图谱模型' in n:
                return 'independent'
        return 'unified'

    def save_config(self, config: LLMControlConfig) -> LLMControlConfig:
        """先验证安全凭据，再将完整配置原子提交。"""
        sanitized = self._sanitize_config(config)
        db = self._db()

        with credential_operation_lock(db):
            return self._save_config_locked(sanitized, db)

    @staticmethod
    def _validate_profile_capabilities(profile: LLMProfile) -> None:
        """Reject deterministic offline option conflicts before persistence/use.

        This deliberately runs outside ``LLMProfile``'s Pydantic validator so
        an older, already persisted profile can still be read and edited.  A
        save or a test revalidates it, and the provider repeats the same gate
        immediately before serializing a request.
        """
        validate_generation_constraints(
            profile.protocol,
            profile,
            model=profile.model,
            base_url=profile.base_url,
            endpoint_kind=profile.endpoint_kind,
            legacy_chat=profile.use_legacy_chat_completions,
            extra_body=profile.extra_body,
            token_limit_settings=profile,
        )

    def _save_config_locked(self, sanitized: LLMControlConfig, db) -> LLMControlConfig:

        # 批量写入 profiles
        params_list = []
        for idx, profile in enumerate(sanitized.profiles):
            row = self._profile_to_row(profile)
            row['sort_order'] = idx
            params_list.append((
                row['id'], row['name'], row['preset_key'], row['protocol'],
                row['base_url'], row['api_key'], row['model'],
                row['temperature'], row['max_tokens'], row['timeout_seconds'],
                row['extra_headers'], row['extra_query'], row['extra_body'],
                row['notes'], row['use_legacy_chat_completions'], row['sort_order'],
            ))

        with db.transaction() as connection:
            connection.execute("DELETE FROM llm_profiles")
            connection.execute("DELETE FROM llm_config_meta")
            connection.execute("INSERT INTO llm_config_meta (key, value) VALUES (?, ?)",
                               ('active_profile_id', sanitized.active_profile_id or ''))
            connection.execute("INSERT INTO llm_config_meta (key, value) VALUES (?, ?)",
                               ('endpoint_mode', sanitized.endpoint_mode))
            for profile in sanitized.profiles:
                connection.execute("INSERT INTO llm_config_meta (key, value) VALUES (?, ?)",
                    ('profile_options:' + profile.id, json.dumps({key: getattr(profile, key) for key in PROFILE_OPTION_FIELDS})))
            for parameters in params_list:
                connection.execute("""INSERT INTO llm_profiles (
                id, name, preset_key, protocol, base_url, api_key, model,
                temperature, max_tokens, timeout_seconds,
                extra_headers, extra_query, extra_body, notes,
                use_legacy_chat_completions, sort_order
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    parameters,
                )

        logger.info("LLM 配置已保存到数据库 (%d 个 profiles)", len(sanitized.profiles))
        return sanitized

    def get_active_profile(self, config: Optional[LLMControlConfig] = None) -> Optional[LLMProfile]:
        cfg = config or self.get_config()
        if not cfg.profiles:
            return None
        target_id = cfg.active_profile_id
        for profile in cfg.profiles:
            if profile.id == target_id:
                return profile
        return cfg.profiles[0]

    def resolve_profile(self, profile: LLMProfile) -> LLMProfile:
        preset = self.get_preset_map().get(profile.preset_key)
        protocol = profile.protocol or (preset.protocol if preset else 'openai')
        base_url = self._normalize_base_url(
            protocol,
            profile.base_url.strip() or (preset.default_base_url if preset else ''),
        )
        model = profile.model.strip() or (preset.default_model if preset else '')
        data = {
            **profile.model_dump(),
            'protocol': protocol,
            'base_url': base_url,
            'model': model,
        }
        try:
            return LLMProfile(**data)
        except ValidationError as exc:
            if 'reasoning_effort' not in str(exc).lower():
                raise
            return LLMProfile.model_construct(**data)

    def resolve_active_profile(self, config: Optional[LLMControlConfig] = None) -> Optional[LLMProfile]:
        active = self.get_active_profile(config)
        if active is None:
            return None
        return self.resolve_profile(active)

    def get_runtime_summary(self, config: Optional[LLMControlConfig] = None) -> LLMRuntimeSummary:
        profile = self.resolve_active_profile(config)
        if profile is None:
            return LLMRuntimeSummary(
                source='unavailable',
                status='unavailable',
                reason='未配置可用的模型档案；请先在模型控制台配置并激活模型',
            )

        try:
            self._validate_profile_capabilities(profile)
        except ValueError as exc:
            return LLMRuntimeSummary(
                source='unavailable',
                status='unavailable',
                active_profile_id=profile.id,
                active_profile_name=profile.name,
                protocol=profile.protocol,
                endpoint_kind='chat' if profile.endpoint_kind == 'auto' and profile.use_legacy_chat_completions else profile.endpoint_kind,
                model=profile.model or None,
                base_url=profile.base_url or None,
                reason=str(exc),
            )

        missing = []
        if not profile.model.strip():
            missing.append('模型 ID')
        if profile.auth_mode != 'none' and not profile.api_key.strip():
            missing.append('API Key')
        if missing:
            return LLMRuntimeSummary(
                source='unavailable',
                status='unavailable',
                active_profile_id=profile.id,
                active_profile_name=profile.name,
                protocol=profile.protocol,
                endpoint_kind='chat' if profile.endpoint_kind == 'auto' and profile.use_legacy_chat_completions else profile.endpoint_kind,
                model=profile.model or None,
                base_url=profile.base_url or None,
                reason='当前激活配置缺少' + '、'.join(missing) + '；请补全后再生成',
            )

        return LLMRuntimeSummary(
            source='profile',
            status='configured',
            active_profile_id=profile.id,
            active_profile_name=profile.name,
            protocol=profile.protocol,
            endpoint_kind='chat' if profile.endpoint_kind == 'auto' and profile.use_legacy_chat_completions else profile.endpoint_kind,
            model=profile.model,
            base_url=profile.base_url,
            using_mock=False,
        )

    async def test_profile_model(
        self,
        profile: LLMProfile,
        llm_service_factory: Callable[[LLMProfile], LLMService],
    ) -> LLMTestResult:
        resolved = self.resolve_profile(profile)
        try:
            self._validate_profile_capabilities(resolved)
        except ValueError as exc:
            return LLMTestResult(
                ok=False,
                provider_label=resolved.name,
                model=resolved.model or '',
                latency_ms=0,
                error=str(exc),
            )
        if (resolved.auth_mode != 'none' and not resolved.api_key.strip()) or not resolved.model.strip():
            return LLMTestResult(
                ok=False,
                provider_label=resolved.name,
                model=resolved.model or '',
                latency_ms=0,
                error='请先填写 API Key 与模型名后再测试',
            )
        started = perf_counter()
        llm_service = None
        try:
            llm_service = llm_service_factory(resolved)
            from domain.ai.value_objects.prompt import Prompt
            from domain.ai.services.llm_service import GenerationConfig
            prompt = Prompt(
                system='你是连通性测试助手。',
                user='请只回复"连接成功"。',
            )
            enabled_thinking = bool(resolved.thinking_budget_tokens) or (
                resolved.protocol == 'anthropic'
                and anthropic_thinking(resolved, resolved.extra_body).get('type') == 'enabled'
            )
            generation_extension = resolved.extra_body.get('generationConfig')
            fixed_extension_budget = any(key in resolved.extra_body for key in (
                'max_tokens', 'max_output_tokens', 'max_completion_tokens',
            )) or (isinstance(generation_extension, dict) and 'maxOutputTokens' in generation_extension)
            config = GenerationConfig(
                model=resolved.model,
                max_tokens=min(resolved.max_tokens, resolved.max_output_tokens_limit or resolved.max_tokens)
                    if enabled_thinking or fixed_extension_budget else min(resolved.max_tokens, 256, resolved.max_output_tokens_limit or 256),
                temperature=resolved.temperature,
                reasoning_effort=resolved.reasoning_effort,
                thinking_budget_tokens=resolved.thinking_budget_tokens,
                top_p=resolved.top_p,
                stop_sequences=resolved.stop_sequences,
            )
            result = await llm_service.generate(prompt, config)
            latency_ms = int((perf_counter() - started) * 1000)
            body = (result.content or '').strip()
            if not body:
                return LLMTestResult(
                    ok=False,
                    provider_label=resolved.name,
                    model=resolved.model or '',
                    latency_ms=latency_ms,
                    error=(
                        "模型返回了空内容。请核对：API Key 是否有效、模型名与协议是否匹配、"
                        "base_url 是否正确；若使用代理或网关，也可能是超时截断或限流。"
                    ),
                )
            preview = body.replace('\r', ' ').replace('\n', ' ')
            return LLMTestResult(
                ok=True,
                provider_label=resolved.name,
                model=resolved.model,
                latency_ms=latency_ms,
                preview=preview[:120],
                effective_max_tokens=config.max_tokens,
                usage_source='provider' if result.token_usage is not None else 'unknown',
                token_usage=None if result.token_usage is None else {
                    'input_tokens': result.token_usage.input_tokens, 'output_tokens': result.token_usage.output_tokens},
                requested_parameters={'reasoning_effort': resolved.reasoning_effort,
                                      'thinking_budget_tokens': resolved.thinking_budget_tokens,
                                      'endpoint_kind': resolved.endpoint_kind,
                                      'top_p': resolved.top_p, 'stop_sequences': resolved.stop_sequences},
                submitted_parameters=getattr(result, 'request_receipt', {}),
            )
        except Exception as exc:
            latency_ms = int((perf_counter() - started) * 1000)
            err_text = str(exc).strip()
            if not err_text:
                err_text = "未知错误（异常无消息），请查看服务端日志。"
            return LLMTestResult(
                ok=False,
                provider_label=resolved.name,
                model=resolved.model or '',
                latency_ms=latency_ms,
                error=err_text,
            )
        finally:
            if llm_service is not None and hasattr(llm_service, 'aclose'):
                await llm_service.aclose()

    def _sanitize_config(self, config: LLMControlConfig) -> LLMControlConfig:
        profiles: List[LLMProfile] = []
        seen_ids: set = set()
        for index, profile in enumerate(config.profiles):
            candidate_id = profile.id.strip() or f'profile-{index + 1}'
            if candidate_id in seen_ids:
                candidate_id = f'{candidate_id}-{index + 1}'
            seen_ids.add(candidate_id)
            profiles.append(
                LLMProfile(
                    **{
                        **profile.model_dump(),
                        'id': candidate_id,
                        'name': profile.name.strip() or f'配置 {index + 1}',
                        'base_url': profile.base_url.strip(),
                        'api_key': profile.api_key.strip(),
                        'model': profile.model.strip(),
                    }
                )
            )
            self._validate_profile_capabilities(self.resolve_profile(profiles[-1]))

        if not profiles:
            profiles = self._build_initial_config().profiles

        active_profile_id = config.active_profile_id if config.active_profile_id in {p.id for p in profiles} else profiles[0].id
        mode = config.endpoint_mode if config.endpoint_mode in ('unified', 'independent') else 'unified'
        return LLMControlConfig(
            version=1,
            active_profile_id=active_profile_id,
            endpoint_mode=mode,  # type: ignore[arg-type]
            profiles=profiles,
        )

    @staticmethod
    def _normalize_base_url(protocol: LLMProtocol, base_url: str) -> str:
        if protocol == 'anthropic':
            return normalize_anthropic_base_url(base_url) or ''
        if protocol == 'gemini':
            return normalize_gemini_base_url(base_url) or ''
        return normalize_openai_base_url(base_url) or ''

    def _build_initial_config(self) -> LLMControlConfig:
        profiles = [
            LLMProfile(
                id='openai-compatible-default',
                name='OpenAI 兼容 / 国产通用',
                preset_key='custom-openai-compatible',
                protocol='openai',
                base_url='',
                model='',
            ),
            LLMProfile(
                id='claude-official-default',
                name='Claude / Anthropic',
                preset_key='claude-official',
                protocol='anthropic',
                base_url='https://api.anthropic.com',
                model='',
            ),
            LLMProfile(
                id='gemini-official-default',
                name='Gemini / Google',
                preset_key='gemini-official',
                protocol='gemini',
                base_url='https://generativelanguage.googleapis.com/v1beta',
                model='',
            ),
        ]
        active_profile_id = profiles[0].id

        env = LLMEnvironmentSettings.from_env()

        anthropic_key = env.anthropic_api_key_with_token_fallback
        openai_key = env.openai_api_key
        gemini_key = env.gemini_api_key
        ark_key = env.ark_api_key

        if anthropic_key and (env.provider == 'anthropic' or not env.provider):
            profiles[1] = profiles[1].model_copy(update={
                'api_key': anthropic_key,
                'base_url': env.anthropic_base_url or profiles[1].base_url,
                'model': env.anthropic_model or profiles[1].model,
            })
            active_profile_id = profiles[1].id
        elif openai_key and (env.provider == 'openai' or not env.provider):
            profiles[0] = profiles[0].model_copy(update={
                'name': 'OpenAI / 兼容网关',
                'preset_key': env.openai_preset_key,
                'api_key': openai_key,
                'base_url': env.openai_base_url,
                'model': env.openai_model,
            })
            active_profile_id = profiles[0].id
        elif gemini_key:
            profiles[2] = profiles[2].model_copy(update={
                'api_key': gemini_key,
                'base_url': env.gemini_base_url or profiles[2].base_url,
                'model': env.gemini_model or profiles[2].model,
            })
            active_profile_id = profiles[2].id
        elif ark_key:
            profiles[0] = profiles[0].model_copy(update={
                'name': '豆包 / Ark',
                'preset_key': 'doubao-ark',
                'api_key': ark_key,
                'base_url': env.ark_base_url_or_default,
                'model': env.ark_model,
            })
            active_profile_id = profiles[0].id

        return LLMControlConfig(
            version=1,
            active_profile_id=active_profile_id,
            endpoint_mode='unified',
            profiles=profiles,
        )
