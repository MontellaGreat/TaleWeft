from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from pydantic import ValidationError

from application.ai.llm_control_service import LLMControlConfig, LLMControlService, LLMProfile
from domain.ai.services.llm_service import GenerationConfig, GenerationResult
from infrastructure.ai.generation_profiles import generation_config_from_profile
from infrastructure.ai.llm_client import LLMClient
from infrastructure.persistence.database.connection import DatabaseConnection
from interfaces.api.v1.engine.ai_invocation_routes import _config_from_dict


@pytest.fixture
def control_db(tmp_path, monkeypatch):
    import application.ai.llm_control_service as control
    db = DatabaseConnection(str(tmp_path / 'control.sqlite'))
    monkeypatch.setattr(control, 'get_database', lambda: db)
    yield db
    db.close_all(skip_checkpoint=True)


def test_model_options_persist_through_service_recreation(control_db):
    profile = LLMProfile(id='p', name='本地模型', base_url='http://localhost:11434/v1', auth_mode='none',
        model='local', endpoint_kind='chat', top_p=0, stop_sequences=['结束', '\n作者注'])
    LLMControlService().save_config(LLMControlConfig(active_profile_id='p', profiles=[profile]))
    reloaded = LLMControlService().get_config().profiles[0]
    assert reloaded.top_p == 0
    assert reloaded.stop_sequences == ['结束', '\n作者注']
    assert reloaded.auth_mode == 'none' and reloaded.endpoint_kind == 'chat'


@pytest.mark.parametrize('endpoint', ['chat', 'responses', 'auto'])
def test_protocol_choice_persists_and_is_visible_in_runtime_summary(control_db, endpoint):
    profile = LLMProfile(id='p', name='协议测试', base_url='http://localhost:11434/v1', auth_mode='none',
                         model='custom-model', endpoint_kind=endpoint)
    LLMControlService().save_config(LLMControlConfig(active_profile_id='p', profiles=[profile]))
    service = LLMControlService()
    reloaded = service.get_config()
    assert reloaded.profiles[0].protocol == 'openai'
    assert reloaded.profiles[0].endpoint_kind == endpoint
    assert service.get_runtime_summary(reloaded).endpoint_kind == endpoint


def test_legacy_chat_configuration_is_reported_as_chat_without_rewriting_storage(control_db):
    profile = LLMProfile(id='p', name='旧聊天配置', model='custom-model', api_key='test-key',
                         endpoint_kind='auto', use_legacy_chat_completions=True)
    config = LLMControlConfig(active_profile_id='p', profiles=[profile])
    assert LLMControlService().get_runtime_summary(config).endpoint_kind == 'chat'
    assert config.profiles[0].endpoint_kind == 'auto'


def test_openai_presets_declare_explicit_protocol_choices():
    presets = LLMControlService().get_preset_map()
    assert presets['custom-openai-compatible'].endpoint_kind == 'chat'
    assert presets['custom-openai-responses'].endpoint_kind == 'responses'
    assert presets['openai-official'].endpoint_kind == 'responses'
    for key in ['deepseek', 'qwen-dashscope', 'glm-openai', 'doubao-ark']:
        assert presets[key].endpoint_kind == 'chat'


@pytest.mark.asyncio
async def test_protocol_switch_preserves_endpoint_through_preset_capability_test_and_runtime(control_db, monkeypatch):
    from infrastructure.ai.provider_factory import DynamicLLMService, LLMProviderFactory
    from domain.ai.value_objects.prompt import Prompt
    from interfaces.api.v1.workbench import llm_control as routes

    service = LLMControlService()
    factory = LLMProviderFactory(service)
    monkeypatch.setattr(routes, '_service', service)
    monkeypatch.setattr(routes, '_factory', factory)
    dynamic = DynamicLLMService(factory)
    prior = None
    try:
        # Keep a contradictory preset to prove that it never overwrites the
        # user's explicit endpoint, including when the same profile switches.
        for endpoint, preset in [('chat', 'custom-openai-responses'), ('responses', 'custom-openai-compatible'), ('chat', 'openai-official')]:
            profile = LLMProfile(id='p', name='协议切换', api_key='test-key', model='custom-model',
                base_url='https://gateway.example/v1', endpoint_kind=endpoint, preset_key=preset)
            service.save_config(LLMControlConfig(active_profile_id='p', profiles=[profile]))
            saved = service.get_config().profiles[0]
            assert service.resolve_profile(saved).endpoint_kind == endpoint
            assert service.get_runtime_summary().endpoint_kind == endpoint
            capability = await routes.get_model_capabilities(saved)
            assert capability['protocol_id'] == f'openai.{endpoint}'

            def stub_provider(candidate):
                provider = factory.create_from_profile(candidate)
                provider.async_client.responses.create = AsyncMock(return_value=SimpleNamespace(status='completed',
                    output=[SimpleNamespace(type='message', content=[SimpleNamespace(type='output_text', text='连接成功')])], usage=None))
                provider.async_client.chat.completions.create = AsyncMock(return_value=SimpleNamespace(
                    choices=[SimpleNamespace(message=SimpleNamespace(content='连接成功'), finish_reason='stop')], usage=None))
                return provider

            tested = await service.test_profile_model(saved, stub_provider)
            assert tested.ok
            assert tested.requested_parameters['endpoint_kind'] == endpoint
            assert tested.submitted_parameters['endpoint'] == f'openai.{endpoint}'

            provider = await dynamic._resolve_provider()
            assert provider is not prior
            if prior is not None:
                assert prior._http_client.is_closed
            assert provider.settings.endpoint_kind == endpoint
            provider.async_client.responses.create = AsyncMock(return_value=SimpleNamespace(status='completed',
                output=[SimpleNamespace(type='message', content=[SimpleNamespace(type='output_text', text='正文')])], usage=None))
            provider.async_client.chat.completions.create = AsyncMock(return_value=SimpleNamespace(
                choices=[SimpleNamespace(message=SimpleNamespace(content='正文'), finish_reason='stop')], usage=None))
            generated = await provider.generate(Prompt(system='写作', user='继续'), GenerationConfig())
            assert generated.content == '正文'
            assert generated.request_receipt['endpoint'] == f'openai.{endpoint}'
            (provider.async_client.chat.completions.create if endpoint == 'chat' else provider.async_client.responses.create).assert_awaited_once()
            (provider.async_client.responses.create if endpoint == 'chat' else provider.async_client.chat.completions.create).assert_not_called()
            prior = provider
    finally:
        await dynamic.aclose()


@pytest.mark.asyncio
async def test_profile_test_passes_parameters_and_reports_actual_receipt(control_db):
    profile = LLMProfile(id='p', name='模型', api_key='test-key', model='model', endpoint_kind='chat',
                         top_p=0, stop_sequences=['停止'])
    response = GenerationResult('连接成功', None)
    response.request_receipt = {'endpoint': 'openai.chat', 'top_p': 0, 'stop_sequences': ['停止']}
    provider = SimpleNamespace(generate=AsyncMock(return_value=response))
    result = await LLMControlService().test_profile_model(profile, lambda p: provider)
    sent = provider.generate.await_args.args[1]
    assert sent.top_p == 0 and sent.stop_sequences == ['停止']
    assert result.requested_parameters['top_p'] == 0
    assert result.requested_parameters['stop_sequences'] == ['停止']
    assert result.submitted_parameters['endpoint'] == 'openai.chat'
    assert result.provider_confirmation_status == 'not_reported'
    assert result.provider_confirmed_parameters == {}


def test_http_client_profiles_and_engine_share_generation_config(monkeypatch):
    from engine.core.ports import GenerationConfig as EngineConfig
    assert EngineConfig is GenerationConfig
    expected = {'top_p': 0, 'stop_sequences': []}
    assert _config_from_dict(expected).top_p == 0
    assert _config_from_dict(expected).stop_sequences == []
    config = LLMClient(SimpleNamespace())._build_config(**expected)
    assert config.top_p == 0 and config.stop_sequences == []
    assert not config.temperature_explicit and not config.max_tokens_explicit
    import infrastructure.ai.generation_profiles as profiles
    monkeypatch.setattr(profiles, 'get_generation_profile', lambda name: {'top_p': 0.8, 'stop_sequences': ['旧']})
    built = generation_config_from_profile('test', **expected)
    assert built.top_p == 0 and built.stop_sequences == []


@pytest.mark.parametrize('values', [
    {'top_p': True}, {'top_p': -0.1}, {'top_p': 1.1}, {'top_p': float('nan')},
    {'stop_sequences': 'STOP'}, {'stop_sequences': ['']}, {'stop_sequences': [12]},
    {'stop_sequences': ['x'] * 17},
])
def test_profile_rejects_invalid_sampling_options(values):
    with pytest.raises(ValidationError):
        LLMProfile(id='p', name='模型', **values)


def test_profile_rejects_unknown_fields_instead_of_silently_dropping_them():
    with pytest.raises(ValidationError, match='reasoning_effor'):
        LLMProfile(id='p', name='模型', reasoning_effor='high')


@pytest.mark.parametrize('temperature', [True, False, '0.7', float('nan'), float('inf')])
def test_http_config_rejects_invalid_temperature_without_coercing(temperature):
    from fastapi import HTTPException
    with pytest.raises(HTTPException) as error:
        _config_from_dict({'temperature': temperature})
    assert error.value.status_code == 422


@pytest.mark.parametrize('values', [
    {'top_p': True}, {'top_p': float('nan')}, {'stop_sequences': ['']},
    {'stop_sequences': 'stop'}, {'default_temperature': True}, {'default_max_tokens': 1.5},
    {'default_max_tokens': True}, {'reasoning_effort': 'unsupported'},
    {'thinking_budget_tokens': -1},
])
def test_direct_settings_cannot_bypass_model_option_validation(values):
    from infrastructure.ai.config.settings import Settings
    with pytest.raises(ValueError):
        Settings(**values)


def test_stop_sequences_preserve_whitespace_and_long_text():
    from infrastructure.ai.config.settings import Settings
    stops = ['\n\n', ' ', 'x' * 1024]
    profile = LLMProfile(id='p', name='模型', stop_sequences=stops)
    assert profile.stop_sequences == stops
    assert Settings(stop_sequences=stops).stop_sequences == stops
