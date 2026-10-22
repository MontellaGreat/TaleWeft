"""Exercise real configuration routers and SQLite; only model calls are fake."""
import asyncio
from types import SimpleNamespace

import httpx
import pytest
from fastapi import FastAPI

from application.ai.embedding_config_service import EmbeddingConfigService
from application.ai.llm_control_service import LLMControlConfig, LLMControlService, LLMProfile
from interfaces.api.middleware.session_auth import SessionAuthority, SessionAuthMiddleware
from interfaces.api.secret_fields import MASKED_SECRET
from interfaces.api.v1.core import settings as settings_routes
from interfaces.api.v1.workbench import llm_control as llm_routes
from tests.unit.application.engine.test_persistence_commit_ack import writer

RealAsyncClient = httpx.AsyncClient
TOKEN = "http-config-session-0123456789abcdef"


@pytest.fixture
def configured(writer, monkeypatch):
    _queue, db = writer
    service = LLMControlService()
    monkeypatch.setattr(service, "_db", lambda: db)
    service.save_config(LLMControlConfig(active_profile_id="first", profiles=[
        LLMProfile(id="first", name="First", api_key="first-private-key", model="test-model",
                   extra_headers={"Authorization": "Bearer header-private-key"}),
        LLMProfile(id="second", name="Second", api_key="second-private-key", model="test-model"),
    ]))
    embedding = EmbeddingConfigService(db)
    embedding.update_config(mode="openai", api_key="embedding-private-key", base_url="https://provider.invalid")
    monkeypatch.setattr(settings_routes, "_service", service)
    monkeypatch.setattr(llm_routes, "_service", service)
    monkeypatch.setattr("application.ai.embedding_config_service.get_embedding_config_service", lambda: embedding)
    llm_routes._invalidate_llm_panel_cache()
    app = FastAPI()
    app.include_router(settings_routes.router, prefix="/api/v1")
    app.include_router(settings_routes.embedding_router, prefix="/api/v1")
    app.include_router(llm_routes.router, prefix="/api/v1")
    app.add_middleware(SessionAuthMiddleware, authority=SessionAuthority(TOKEN))
    yield app, service, embedding, db
    llm_routes._invalidate_llm_panel_cache()


def request(app, method, path, **kwargs):
    async def run():
        async with RealAsyncClient(transport=httpx.ASGITransport(app=app),
                                   base_url="http://127.0.0.1:8005") as client:
            return await client.request(method, path, headers={"Authorization": f"Bearer {TOKEN}"}, **kwargs)
    return asyncio.run(run())


def test_all_configuration_responses_hide_keys_without_mutating_internal_profiles(configured):
    app, service, _embedding, _db = configured
    for path in ("/api/v1/llm-control", "/api/v1/settings/llm-configs/", "/api/v1/settings/embedding/"):
        response = request(app, "GET", path)
        assert response.status_code == 200
        for secret in ("first-private-key", "second-private-key", "header-private-key", "embedding-private-key"):
            assert secret not in response.text
        assert MASKED_SECRET in response.text
    assert service.get_config().profiles[0].api_key == "first-private-key"


@pytest.mark.parametrize("incoming", ["", MASKED_SECRET])
def test_panel_round_trip_preserves_real_keys_and_sensitive_headers(configured, incoming):
    app, service, _embedding, _db = configured
    panel = request(app, "GET", "/api/v1/llm-control").json()
    panel["config"]["profiles"][0]["api_key"] = incoming
    panel["config"]["profiles"][0]["name"] = "Edited"
    response = request(app, "PUT", "/api/v1/llm-control", json=panel["config"])
    assert response.status_code == 200, response.text
    assert "private-key" not in response.text
    profile = service.get_config().profiles[0]
    assert profile.name == "Edited" and profile.api_key == "first-private-key"
    assert profile.extra_headers["Authorization"] == "Bearer header-private-key"


def test_legacy_update_and_embedding_update_preserve_keys(configured):
    app, service, embedding, _db = configured
    response = request(app, "PUT", "/api/v1/settings/llm-configs/second",
                       json={"name": "changed", "api_key": MASKED_SECRET})
    assert response.status_code == 200 and "second-private-key" not in response.text
    assert service.get_config().profiles[1].api_key == "second-private-key"
    data = request(app, "GET", "/api/v1/settings/embedding/").json()
    data["api_key"] = ""
    data["model"] = "updated-embedding"
    response = request(app, "PUT", "/api/v1/settings/embedding/", json=data)
    assert response.status_code == 200, response.text
    assert "embedding-private-key" not in response.text
    assert embedding.get_config().api_key == "embedding-private-key"


def test_explicit_new_key_is_saved_and_masked_and_unknown_sentinel_is_rejected(configured):
    app, service, _embedding, _db = configured
    response = request(app, "PUT", "/api/v1/settings/llm-configs/first", json={"api_key": "new-private-key"})
    assert response.status_code == 200 and "new-private-key" not in response.text
    assert service.get_config().profiles[0].api_key == "new-private-key"
    response = request(app, "POST", "/api/v1/settings/llm-configs/", json={
        "name": "new", "provider": "openai", "api_key": MASKED_SECRET,
    })
    assert response.status_code == 400


def test_test_endpoint_uses_saved_selected_profile_key(configured, monkeypatch):
    app, _service, _embedding, _db = configured
    captured = []

    async def test_profile(profile, _factory):
        captured.append(profile)
        return {"ok": True, "provider_label": profile.name, "model": profile.model,
                "latency_ms": 1, "preview": "connected"}

    monkeypatch.setattr(llm_routes._service, "test_profile_model", test_profile)
    panel = request(app, "GET", "/api/v1/llm-control").json()
    response = request(app, "POST", "/api/v1/llm-control/test", json=panel["config"]["profiles"][1])
    assert response.status_code == 200 and response.json()["ok"]
    assert captured[0].api_key == "second-private-key"


def test_models_and_embedding_models_resolve_correct_saved_key(configured, monkeypatch):
    app, _service, _embedding, _db = configured
    captured = []

    class FakeProviderClient:
        def __init__(self, **_kwargs): pass
        async def __aenter__(self): return self
        async def __aexit__(self, *_args): pass
        async def get(self, url, headers):
            captured.append(headers)
            return httpx.Response(200, json={"data": [{"id": "model-one"}]}, request=httpx.Request("GET", url))

    monkeypatch.setattr(llm_routes.httpx, "AsyncClient", FakeProviderClient)
    response = request(app, "POST", "/api/v1/llm-control/models", json={
        "profile_id": "second", "protocol": "openai", "api_key": MASKED_SECRET,
        "base_url": "https://provider.invalid/v1",
    })
    assert response.status_code == 200, response.text
    assert captured[-1]["Authorization"] == "Bearer second-private-key"
    response = request(app, "POST", "/api/v1/settings/embedding/fetch-models", json={
        "provider": "openai", "api_key": MASKED_SECRET, "base_url": "https://provider.invalid/v1",
    })
    assert response.status_code == 200, response.text
    assert captured[-1]["Authorization"] == "Bearer embedding-private-key"
