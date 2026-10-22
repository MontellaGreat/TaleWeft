from __future__ import annotations

import pytest

from application.ai.llm_control_service import LLMControlConfig, LLMProfile
from domain.ai.services.llm_service import GenerationConfig
from domain.ai.value_objects.prompt import Prompt
from infrastructure.ai.provider_factory import DynamicLLMService, LLMProviderFactory


class _FakeControlService:
    def __init__(self, profiles: list[LLMProfile]):
        self.profiles = profiles
        self.index = 0

    def resolve_active_profile(self):
        return self.profiles[self.index] if self.profiles else None

    def get_config(self):
        return LLMControlConfig(profiles=self.profiles)


class _FakeProvider:
    def __init__(self, name: str):
        self.name = name
        self.closed = False
        self.close_count = 0

    async def aclose(self):
        self.closed = True
        self.close_count += 1


class _FakeSyncClient:
    def __init__(self):
        self.is_closed = False

    def close(self):
        self.is_closed = True


class _FakeAsyncClient:
    def __init__(self):
        self.closed = False

    async def aclose(self):
        self.closed = True


class _ProviderWithoutAclose:
    def __init__(self):
        self._http_client_sync = _FakeSyncClient()
        self._http_client = _FakeAsyncClient()


class _FakeFactory:
    def __init__(self, profiles: list[LLMProfile]):
        self.control_service = _FakeControlService(profiles)
        self.providers = {profile.id: _FakeProvider(profile.id) for profile in profiles}

    def create_from_profile(self, profile):
        return self.providers[profile.id]


def _profile(profile_id: str, *, model: str) -> LLMProfile:
    return LLMProfile(
        id=profile_id,
        name=profile_id,
        api_key="key",
        model=model,
        protocol="openai",
    )


@pytest.mark.asyncio
async def test_dynamic_llm_service_closes_old_provider_when_cache_key_changes():
    profiles = [
        _profile("p1", model="model-a"),
        _profile("p2", model="model-b"),
    ]
    factory = _FakeFactory(profiles)
    service = DynamicLLMService(factory)

    first = await service._resolve_provider()
    factory.control_service.index = 1
    second = await service._resolve_provider()

    assert first is factory.providers["p1"]
    assert second is factory.providers["p2"]
    assert factory.providers["p1"].closed is True
    assert factory.providers["p1"].close_count == 1
    assert factory.providers["p2"].closed is False


@pytest.mark.asyncio
async def test_dynamic_llm_service_reuses_provider_when_cache_key_is_stable():
    profiles = [_profile("p1", model="model-a")]
    factory = _FakeFactory(profiles)
    service = DynamicLLMService(factory)

    first = await service._resolve_provider()
    second = await service._resolve_provider()

    assert first is second
    assert factory.providers["p1"].closed is False


@pytest.mark.asyncio
async def test_dynamic_llm_service_aclose_closes_cached_provider_and_clears_cache():
    profiles = [_profile("p1", model="model-a")]
    factory = _FakeFactory(profiles)
    service = DynamicLLMService(factory)

    provider = await service._resolve_provider()
    await service.aclose()

    assert provider.closed is True
    assert service._cached_provider is None
    assert service._cached_key is None


@pytest.mark.asyncio
async def test_dynamic_llm_service_close_fallback_handles_provider_clients_without_aclose():
    service = DynamicLLMService(_FakeFactory([_profile("p1", model="model-a")]))
    provider = _ProviderWithoutAclose()
    service._cached_provider = provider
    service._cached_key = "manual"

    await service.aclose()

    assert provider._http_client_sync.is_closed is True
    assert provider._http_client.closed is True
    assert service._cached_provider is None


def test_factory_requires_explicit_profile_instead_of_mock_fallback():
    factory = LLMProviderFactory(_FakeControlService([]))

    with pytest.raises(ValueError, match='模型档案'):
        factory.create_from_profile(None)
    with pytest.raises(ValueError, match='模型档案'):
        factory.create_active_provider()


@pytest.mark.asyncio
@pytest.mark.parametrize('stream', [False, True])
async def test_dynamic_llm_service_missing_profile_fails_before_generation(stream):
    service = DynamicLLMService(LLMProviderFactory(_FakeControlService([])))
    prompt = Prompt(system='system', user='user')

    with pytest.raises(ValueError, match='模型档案'):
        if stream:
            async for _ in service.stream_generate(prompt, GenerationConfig()):
                pytest.fail('Missing configuration must not yield generated content')
        else:
            await service.generate(prompt, GenerationConfig())
    assert service._cached_provider is None
    assert service._cached_key is None


@pytest.mark.asyncio
@pytest.mark.parametrize('profile_id', [None, 'p1'])
async def test_removed_profile_closes_cached_provider_and_cannot_reuse_it(profile_id):
    factory = _FakeFactory([_profile('p1', model='model-a')])
    service = DynamicLLMService(factory, profile_id=profile_id)
    provider = await service._resolve_provider()
    factory.control_service.profiles = []

    with pytest.raises(ValueError, match='模型档案|LLM profile not found'):
        await service._resolve_provider()

    assert provider.closed is True
    assert provider.close_count == 1
    assert service._cached_provider is None
    assert service._cached_key is None
