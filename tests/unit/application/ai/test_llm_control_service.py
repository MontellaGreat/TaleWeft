import pytest

from application.ai.llm_control_service import LLMControlConfig, LLMControlService, LLMProfile
from domain.ai.services.llm_service import DEFAULT_MAX_OUTPUT_TOKENS
from infrastructure.ai.llm_environment import ARK_DEFAULT_BASE_URL


LLM_ENV_NAMES = (
    "LLM_PROVIDER",
    "ANTHROPIC_API_KEY",
    "ANTHROPIC_AUTH_TOKEN",
    "ANTHROPIC_BASE_URL",
    "ANTHROPIC_MODEL",
    "OPENAI_API_KEY",
    "OPENAI_BASE_URL",
    "OPENAI_MODEL",
    "GEMINI_API_KEY",
    "GEMINI_BASE_URL",
    "GEMINI_MODEL",
    "ARK_API_KEY",
    "ARK_BASE_URL",
    "ARK_MODEL",
)


def _clear_llm_env(monkeypatch):
    for name in LLM_ENV_NAMES:
        monkeypatch.delenv(name, raising=False)


def test_initial_config_prefers_anthropic_when_provider_unset(monkeypatch):
    _clear_llm_env(monkeypatch)
    monkeypatch.setenv("ANTHROPIC_AUTH_TOKEN", "anthropic-token")
    monkeypatch.setenv("ANTHROPIC_MODEL", "claude-test")

    config = LLMControlService()._build_initial_config()

    assert config.active_profile_id == "claude-official-default"
    active = config.profiles[1]
    assert active.api_key == "anthropic-token"
    assert active.model == "claude-test"


def test_initial_config_uses_openai_official_without_base_url(monkeypatch):
    _clear_llm_env(monkeypatch)
    monkeypatch.setenv("LLM_PROVIDER", "openai")
    monkeypatch.setenv("OPENAI_API_KEY", "openai-key")
    monkeypatch.setenv("OPENAI_MODEL", "gpt-test")

    config = LLMControlService()._build_initial_config()

    active = config.profiles[0]
    assert config.active_profile_id == active.id
    assert active.preset_key == "openai-official"
    assert active.base_url == ""
    assert active.model == "gpt-test"


def test_initial_config_uses_custom_openai_when_base_url_present(monkeypatch):
    _clear_llm_env(monkeypatch)
    monkeypatch.setenv("OPENAI_API_KEY", "openai-key")
    monkeypatch.setenv("OPENAI_BASE_URL", "https://gateway.example/v1")

    config = LLMControlService()._build_initial_config()

    active = config.profiles[0]
    assert active.preset_key == "custom-openai-compatible"
    assert active.base_url == "https://gateway.example/v1"


def test_initial_config_keeps_ark_default_base_url(monkeypatch):
    _clear_llm_env(monkeypatch)
    monkeypatch.setenv("ARK_API_KEY", "ark-key")
    monkeypatch.setenv("ARK_MODEL", "ark-model")

    config = LLMControlService()._build_initial_config()

    active = config.profiles[0]
    assert active.name == "豆包 / Ark"
    assert active.base_url == ARK_DEFAULT_BASE_URL
    assert active.model == "ark-model"


def test_profile_preserves_small_requested_max_tokens():
    profile = LLMProfile(id="p", name="Profile", max_tokens=512)

    assert profile.max_tokens == 512


def test_row_to_profile_preserves_max_tokens_above_global_floor():
    row = {
        "id": "p",
        "name": "Profile",
        "preset_key": "custom-openai-compatible",
        "protocol": "openai",
        "base_url": "",
        "api_key": "",
        "model": "",
        "temperature": 0.7,
        "max_tokens": DEFAULT_MAX_OUTPUT_TOKENS + 1000,
        "timeout_seconds": 300,
        "extra_headers": "{}",
        "extra_query": "{}",
        "extra_body": "{}",
        "notes": "",
        "use_legacy_chat_completions": 0,
    }

    profile = LLMControlService()._row_to_profile(row)

    assert profile.max_tokens == DEFAULT_MAX_OUTPUT_TOKENS + 1000


def test_runtime_summary_without_profile_reports_configuration_missing():
    runtime = LLMControlService().get_runtime_summary(LLMControlConfig(profiles=[]))

    assert runtime.source == 'unavailable'
    assert runtime.status == 'unavailable'
    assert runtime.using_mock is False
    assert runtime.active_profile_id is None
    assert '模型档案' in runtime.reason


@pytest.mark.parametrize(('api_key', 'model', 'missing'), [
    ('key', '', ['模型 ID']),
    ('', 'model-a', ['API Key']),
    ('   ', '   ', ['模型 ID', 'API Key']),
])
def test_runtime_summary_for_incomplete_profile_never_advertises_mock(api_key, model, missing):
    config = LLMControlConfig(profiles=[LLMProfile(id='p', name='Profile', api_key=api_key, model=model)])

    runtime = LLMControlService().get_runtime_summary(config)

    assert runtime.source == 'unavailable'
    assert runtime.status == 'unavailable'
    assert runtime.using_mock is False
    assert runtime.active_profile_id == 'p'
    assert all(field in runtime.reason for field in missing)
    assert 'Mock' not in runtime.reason


@pytest.mark.parametrize('auth_mode', ['api_key', 'none'])
def test_runtime_summary_configured_does_not_claim_a_live_connection(auth_mode):
    config = LLMControlConfig(profiles=[LLMProfile(
        id='p', name='Profile', model='model-a',
        api_key='key' if auth_mode == 'api_key' else '',
        base_url='https://gateway.example/v1', auth_mode=auth_mode,
    )])

    runtime = LLMControlService().get_runtime_summary(config)

    assert runtime.source == 'profile'
    assert runtime.status == 'configured'
    assert runtime.using_mock is False
    assert runtime.model == 'model-a'
    assert runtime.reason is None
