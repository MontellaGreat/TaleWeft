"""Exercise trace settings admission and real SQLite persistence through HTTP."""
import sqlite3

import httpx
import pytest
from fastapi import FastAPI

from engine.infrastructure.persistence.trace_store import SqliteTraceStore
from interfaces.api.middleware.session_auth import SessionAuthority, SessionAuthMiddleware
from interfaces.api.v1.core.settings import privacy_router
from interfaces.api.v1.engine import trace_routes

TOKEN = "trace-privacy-session-0123456789abcdef"
AUTH = {"Authorization": f"Bearer {TOKEN}"}
PRIVACY_PATH = "/api/v1/settings/trace-privacy"


class _Pool:
    def __init__(self, path):
        self.path = path

    def get_connection(self):
        return sqlite3.connect(self.path)


@pytest.fixture
def configured(tmp_path, monkeypatch):
    pool = _Pool(tmp_path / "trace-http.db")
    store = SqliteTraceStore(pool)
    monkeypatch.setattr(trace_routes, "_get_trace_store", lambda: store)
    app = FastAPI()
    app.include_router(privacy_router, prefix="/api/v1")
    app.add_middleware(SessionAuthMiddleware, authority=SessionAuthority(TOKEN))
    return app, store, pool


@pytest.mark.asyncio
async def test_trace_privacy_round_trip_controls_persistence_and_clear(configured):
    app, store, pool = configured
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://127.0.0.1") as client:
        defaults = await client.get(PRIVACY_PATH, headers=AUTH)
        assert defaults.status_code == 200
        assert defaults.json()["capture_fulltext"] is False
        store.record_ai_span({"trace_id": "trace", "span_id": "first", "novel_id": "novel",
                              "response_full": "private draft", "response_preview": "short preview"})
        store.record_ai_artifact({"trace_id": "trace", "artifact_id": "artifact", "novel_id": "novel",
                                  "content_text": "private draft", "content_preview": "short preview"})
        assert store.get_ai_timeline("trace")[0]["response_full"] is None
        with pool.get_connection() as conn:
            assert conn.execute("SELECT content_text FROM ai_trace_artifacts").fetchone()[0] is None

        settings = {**defaults.json(), "capture_fulltext": True}
        saved = await client.put(PRIVACY_PATH, headers=AUTH, json=settings)
        assert saved.status_code == 200 and saved.json() == settings
        assert SqliteTraceStore(pool).get_privacy_settings().capture_fulltext is True
        store.record_ai_span({"trace_id": "trace", "span_id": "second", "novel_id": "novel",
                              "prompt_full": {"user": "author opted in", "api_key": "never-store"}})
        assert store.get_ai_timeline("trace")[1]["prompt_full"] == {
            "user": "author opted in", "api_key": "[redacted]",
        }

        disabled = await client.put(PRIVACY_PATH, headers=AUTH, json={**settings, "enabled": False})
        assert disabled.status_code == 200
        store.record_ai_span({"trace_id": "trace", "span_id": "blocked"})
        assert len(store.get_ai_timeline("trace")) == 2
        cleared = await client.delete(PRIVACY_PATH + "/records", headers=AUTH)
        assert cleared.status_code == 200
        assert cleared.json() == {"deleted": {"ai_trace_artifacts": 1, "ai_trace_spans": 2}}
        assert store.list_ai_traces("novel") == []
        assert SqliteTraceStore(pool).get_privacy_settings().enabled is False


@pytest.mark.parametrize("method,path,payload", [
    ("GET", PRIVACY_PATH, None),
    ("PUT", PRIVACY_PATH, {"capture_fulltext": True}),
    ("DELETE", PRIVACY_PATH + "/records", None),
])
@pytest.mark.asyncio
async def test_trace_privacy_requires_session_without_changing_settings(configured, method, path, payload):
    app, store, _pool = configured
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://127.0.0.1") as client:
        response = await client.request(method, path, json=payload)
    assert response.status_code == 401
    assert store.get_privacy_settings().capture_fulltext is False


@pytest.mark.parametrize("invalid", [{"retention_days": 0}, {"retention_days": 366}, {"max_spans": 0}])
@pytest.mark.asyncio
async def test_trace_privacy_rejects_invalid_retention_without_saving(configured, invalid):
    app, store, _pool = configured
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://127.0.0.1") as client:
        response = await client.put(PRIVACY_PATH, headers=AUTH, json=invalid)
    assert response.status_code == 422
    assert store.get_privacy_settings().retention_days == 14
    assert store.get_privacy_settings().max_spans == 10000


@pytest.mark.asyncio
async def test_manuscript_policy_export_and_clear_are_isolated(configured, monkeypatch):
    app, store, pool = configured
    monkeypatch.setattr(trace_routes, '_novel_exists', lambda value: value in {'a', 'b'})
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://127.0.0.1') as client:
        scoped = PRIVACY_PATH + '?novel_id=a'
        saved = await client.put(scoped, headers=AUTH, json={'capture_fulltext': True, 'retention_days': 30})
        assert saved.status_code == 200
        assert SqliteTraceStore(pool).get_privacy_settings('a').capture_fulltext
        assert not store.get_privacy_settings('b').capture_fulltext
        for novel in ('a', 'b'):
            store.record_ai_span({'trace_id': 'shared', 'span_id': novel, 'novel_id': novel,
                                  'prompt_full': {'user': 'draft', 'token': 'never-export'}})
            store.record_ai_artifact({'artifact_id': novel, 'trace_id': 'shared', 'novel_id': novel,
                                      'content_text': 'draft'})
        assert store.get_ai_timeline('shared', 'a')[0]['prompt_full']['user'] == 'draft'
        assert store.get_ai_timeline('shared', 'b')[0]['prompt_full'] is None
        exported = await client.get(PRIVACY_PATH + '/records/export?novel_id=a', headers=AUTH)
        assert exported.status_code == 200
        assert {row['novel_id'] for row in exported.json()['ai_trace_spans']} == {'a'}
        assert 'never-export' not in exported.text
        assert len(exported.json()['ai_trace_artifacts']) == 1
        reset = await client.delete(scoped, headers=AUTH)
        assert reset.status_code == 200 and not reset.json()['capture_fulltext']
        cleared = await client.delete(PRIVACY_PATH + '/records?novel_id=a', headers=AUTH)
        assert cleared.json()['deleted'] == {'ai_trace_artifacts': 1, 'ai_trace_spans': 1}
        assert len(store.get_ai_timeline('shared', 'b')) == 1
        assert (await client.put(PRIVACY_PATH + '?novel_id=missing', headers=AUTH,
                                 json={'capture_fulltext': True})).status_code == 404
        assert (await client.get(PRIVACY_PATH + '/records/export?novel_id=b')).status_code == 401


def test_retention_and_orphan_cleanup_do_not_cross_manuscripts(configured):
    from datetime import datetime, timezone, timedelta
    from infrastructure.ai.trace_privacy import TracePrivacySettings
    _, store, pool = configured
    old = (datetime.now(timezone.utc) - timedelta(days=20)).isoformat()
    store.save_privacy_settings(TracePrivacySettings(retention_days=30), 'b')
    for novel in ('a', 'b'):
        store.record_ai_span({'trace_id': 'shared', 'span_id': novel, 'novel_id': novel, 'created_at': old})
        store.record_ai_artifact({'artifact_id': novel, 'trace_id': 'shared', 'novel_id': novel, 'created_at': old})
    assert not store.get_ai_timeline('shared', 'a')
    assert len(store.get_ai_timeline('shared', 'b')) == 1
    # Saving global defaults must preserve an independently configured project.
    store.save_privacy_settings(TracePrivacySettings(retention_days=1))
    assert len(store.get_ai_timeline('shared', 'b')) == 1
    with pool.get_connection() as conn:
        assert conn.execute('SELECT novel_id FROM ai_trace_artifacts').fetchall() == [('b',)]
