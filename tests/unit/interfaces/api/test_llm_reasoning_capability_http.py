"""HTTP boundary for the model/reasoning capability gate."""
import asyncio

import httpx
import pytest
from fastapi import FastAPI

from application.ai.llm_control_service import LLMControlConfig, LLMControlService, LLMProfile
from interfaces.api.middleware.session_auth import SessionAuthMiddleware, SessionAuthority
from interfaces.api.v1.workbench import llm_control as llm_routes
from tests.unit.application.engine.test_persistence_commit_ack import writer


TOKEN = 'reasoning-capability-http-session-0123456789'


@pytest.fixture
def app_with_service(writer, monkeypatch):
    _queue, db = writer
    service = LLMControlService()
    monkeypatch.setattr(service, '_db', lambda: db)
    service.save_config(LLMControlConfig(active_profile_id='p', profiles=[
        LLMProfile(id='p', name='profile', api_key='key', model='custom-model'),
    ]))
    monkeypatch.setattr(llm_routes, '_service', service)
    llm_routes._invalidate_llm_panel_cache()
    app = FastAPI()
    app.include_router(llm_routes.router, prefix='/api/v1')
    app.add_middleware(SessionAuthMiddleware, authority=SessionAuthority(TOKEN))
    yield app
    llm_routes._invalidate_llm_panel_cache()


def _request(app, method, path, **kwargs):
    async def run():
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url='http://127.0.0.1:8005'
        ) as client:
            return await client.request(
                method, path,
                headers={'Authorization': f'Bearer {TOKEN}'},
                **kwargs,
            )
    return asyncio.run(run())


def test_save_rejects_known_non_reasoning_model_with_422(app_with_service):
    panel = _request(app_with_service, 'GET', '/api/v1/llm-control').json()
    profile = panel['config']['profiles'][0]
    profile.update({'model': 'gpt-4o', 'reasoning_effort': 'high'})
    panel['config']['profiles'][0] = profile
    response = _request(app_with_service, 'PUT', '/api/v1/llm-control', json=panel['config'])
    assert response.status_code == 422
    assert '不支持 reasoning_effort' in response.text
