"""Independent adversarial review of recovery ordering and approval races."""
import json

import httpx
import pytest

from application.engine.services.query_service import _hydrate_pending_invocation_from_db
from interfaces.api.v1.engine import ai_invocation_routes as routes
from tests.unit.interfaces.api.v1.test_macro_legacy_recovery import recovery, writer  # noqa: F401


def test_newer_completed_request_wins_over_late_update_to_older_failed_session(recovery):
    db, _, _ = recovery
    db.execute("""UPDATE ai_invocation_sessions SET status='failed',created_at='2026-01-01 00:00:00',
        updated_at='2026-01-01 00:02:00' WHERE id='legacy-macro'""")
    db.execute("""INSERT INTO ai_invocation_sessions
        (id,operation,node_key,policy,status,context_json,created_at,updated_at)
        VALUES('new-completed','autopilot.macro.plan','planning-quick-macro','DIRECT','completed',?,
               '2026-01-01 00:01:00','2026-01-01 00:01:30')""", (json.dumps({'novel_id': 'recover-book'}),))
    status = _hydrate_pending_invocation_from_db({
        'novel_id': 'recover-book', 'autopilot_status': 'stopped', 'current_stage': 'paused_for_review'})
    assert not status.get('active_invocation_session_id'), 'A late update must not resurrect a superseded request'


@pytest.mark.asyncio
async def test_resume_rechecks_durable_running_attempt_after_refresh_before_dispatch(recovery, monkeypatch):
    db, app, _ = recovery
    db.execute("UPDATE ai_invocation_sessions SET status='awaiting_pre_call_review' WHERE id='legacy-macro'")

    def competing_approval(*args, **kwargs):
        db.execute("""INSERT INTO ai_invocation_attempts(id,session_id,status,content)
            VALUES('already-running','legacy-macro','running','')""")
        db.execute("""UPDATE ai_invocation_sessions SET status='generating',attempts_json=?
            WHERE id='legacy-macro'""", (json.dumps(['already-running']),))

    monkeypatch.setattr(routes, '_refresh_session_variables_from_hub', competing_approval)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://test') as client:
        response = await client.post('/ai-invocations/legacy-macro/resume', json={})
    assert response.status_code == 409, response.text
    assert db.fetch_one("SELECT COUNT(*) n FROM ai_invocation_attempts WHERE session_id='legacy-macro'")['n'] == 1
    assert json.loads(db.fetch_one("SELECT attempts_json FROM ai_invocation_sessions WHERE id='legacy-macro'")['attempts_json']) == ['already-running']


@pytest.mark.asyncio
async def test_retry_basis_must_match_the_loaded_session_not_only_its_status(recovery, monkeypatch):
    db, app, _ = recovery
    original = routes._retry_claim_basis

    def update_before_basis(repos, session, **kwargs):
        db.execute("UPDATE ai_invocation_sessions SET metadata_json=? WHERE id='legacy-macro'",
                   (json.dumps({'author_review_note': 'Newer reviewed context'}),))
        return original(repos, session, **kwargs)

    monkeypatch.setattr(routes, '_retry_claim_basis', update_before_basis)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://test') as client:
        response = await client.post('/ai-invocations/legacy-macro/retry', json={})
    assert response.status_code == 409, response.text
    assert db.fetch_all("SELECT id FROM ai_invocation_attempts WHERE session_id='legacy-macro'") == []
    assert json.loads(db.fetch_one("SELECT metadata_json FROM ai_invocation_sessions WHERE id='legacy-macro'")['metadata_json']) == {'author_review_note': 'Newer reviewed context'}
