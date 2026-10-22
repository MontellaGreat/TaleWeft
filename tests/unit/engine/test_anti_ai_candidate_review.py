"""Real writer/API evidence for author review and recovery, including concurrent resume."""
import asyncio
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from tests.unit.engine.test_daemon_rewrite_candidate_integrity import daemon_writer, RAW, CLEAN
from application.audit.services.anti_ai_candidate_review import AntiAICandidateReview, CandidateReviewError
from application.audit.services.anti_ai_rewrite_workflow import AntiAIRewriteWorkflow
from application.engine.services.chapter_rewrite_candidates import ChapterRewriteCandidates, CandidateConflictError
from interfaces.api.v1 import anti_ai


def reviewer(fixture, **kwargs):
    db = fixture[0]
    db.execute("UPDATE novels SET current_stage='paused_for_review' WHERE id='novel'")
    async def score(*args, **kw):
        assert kw["persist"] is False
        return {"similarity_score": 0.9, "drift_alert": False}
    voice = kwargs.pop("voice_service", SimpleNamespace(use_llm_mode=True, score_chapter_async=score))
    return AntiAICandidateReview(db, voice_service=voice, **kwargs)


async def candidate(fixture):
    db, _, host, novel, chapter, _ = fixture
    result = await AntiAIRewriteWorkflow(db, score_voice=lambda body: host._score_voice_only("novel", 1, body)).execute(
        "novel", 1, mode="review", execution_guard=host._execution_identities["novel"], force_rewrite=True,
        rewrite=lambda body, attempt: host._rewrite_chapter_for_voice(novel, chapter, body, .9, attempt), max_attempts=1)
    return result["candidate"]


def tokens(item):
    return {"expected_candidate_hash": item["content_hash"], "expected_base_hash": item["base_hash"],
            "expected_base_revision": item["base_revision"], "expected_run_epoch": 1}


@pytest.mark.asyncio
async def test_author_confirm_revalidates_commits_archives_and_stays_paused(daemon_writer):
    item = await candidate(daemon_writer)
    events = []
    service = reviewer(daemon_writer, on_event=lambda *args: events.append(args))
    result = await service.confirm("novel", item["candidate_id"], **tokens(item))
    saved = result["candidate"]
    assert saved["status"] == "committed" and saved["audit"]["final_audit"]["persistence_status"] == "committed"
    assert saved["audit"]["final_audit"]["content_hash"] == saved["content_hash"]
    assert daemon_writer[0].fetch_one("SELECT content,revision FROM chapters WHERE id='chapter'") == {"content": CLEAN, "revision": 1}
    assert daemon_writer[0].fetch_one("SELECT current_stage FROM novels WHERE id='novel'")["current_stage"] == "paused_for_review"
    assert result["requires_resume"] is True and events[-1][1] == "audit_rewrite_committed"


@pytest.mark.asyncio
async def test_confirmation_is_idempotent_and_does_not_rewrite_revision(daemon_writer):
    item = await candidate(daemon_writer)
    service = reviewer(daemon_writer)
    await service.confirm("novel", item["candidate_id"], **tokens(item))
    result = await service.confirm("novel", item["candidate_id"], **tokens(item))
    assert result["candidate"]["audit"]["committed_revision"] == 1
    assert daemon_writer[0].fetch_one("SELECT revision FROM chapters WHERE id='chapter'")["revision"] == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("field,value", [("expected_base_hash", "0"*64), ("expected_base_revision", 99), ("expected_candidate_hash", "0"*64), ("expected_run_epoch", 99)])
async def test_mismatched_review_tokens_do_not_change_body(daemon_writer, field, value):
    item = await candidate(daemon_writer)
    service = reviewer(daemon_writer)
    request = {**tokens(item), field: value}
    with pytest.raises(CandidateConflictError):
        await service.confirm("novel", item["candidate_id"], **request)
    assert daemon_writer[0].fetch_one("SELECT content FROM chapters WHERE id='chapter'")["content"] == RAW


@pytest.mark.asyncio
async def test_resume_during_candidate_voice_check_rejects_at_writer(daemon_writer):
    item = await candidate(daemon_writer)
    db = daemon_writer[0]
    async def score(novel_id, chapter_number, content, **kwargs):
        if content == CLEAN:
            db.execute("UPDATE novels SET autopilot_run_epoch=2,current_stage='writing' WHERE id='novel'")
        return {"similarity_score": .9, "drift_alert": False}
    service = reviewer(daemon_writer, voice_service=SimpleNamespace(use_llm_mode=True, score_chapter_async=score))
    with pytest.raises(CandidateConflictError):
        await service.confirm("novel", item["candidate_id"], **tokens(item))
    assert db.fetch_one("SELECT content FROM chapters WHERE id='chapter'")["content"] == RAW
    assert service.candidates.get(item["candidate_id"])["status"] == "conflict"


@pytest.mark.asyncio
async def test_no_voice_baseline_cannot_be_treated_as_review_success(daemon_writer):
    item = await candidate(daemon_writer)
    service = reviewer(daemon_writer, voice_service=None)
    with pytest.raises(CandidateReviewError):
        await service.confirm("novel", item["candidate_id"], **tokens(item))
    assert service.candidates.get(item["candidate_id"])["audit"]["voice"]["status"] == "unavailable"
    assert daemon_writer[0].fetch_one("SELECT content FROM chapters WHERE id='chapter'")["content"] == RAW


@pytest.mark.asyncio
async def test_author_rejection_cannot_be_reopened_by_an_inflight_revalidation(daemon_writer):
    item = await candidate(daemon_writer)
    ready, release = asyncio.Event(), asyncio.Event()
    async def score(novel_id, chapter_number, content, **kwargs):
        if content == CLEAN:
            ready.set()
            await release.wait()
        return {"similarity_score": .9, "drift_alert": False}
    service = reviewer(daemon_writer, voice_service=SimpleNamespace(use_llm_mode=True, score_chapter_async=score))
    task = asyncio.create_task(service.confirm("novel", item["candidate_id"], **tokens(item)))
    await ready.wait()
    await service.reject("novel", item["candidate_id"], item["content_hash"])
    release.set()
    with pytest.raises(CandidateReviewError):
        await task
    assert service.candidates.get(item["candidate_id"])["status"] == "rejected"
    assert daemon_writer[0].fetch_one("SELECT content FROM chapters WHERE id='chapter'")["content"] == RAW


@pytest.mark.asyncio
async def test_archive_failure_preserves_commit_and_can_recover(daemon_writer, monkeypatch):
    item = await candidate(daemon_writer)
    events = []
    service = reviewer(daemon_writer, on_event=lambda *args: events.append(args))
    from infrastructure.persistence.database.sqlite_anti_ai_audit_repository import SqliteAntiAiAuditRepository
    original = SqliteAntiAiAuditRepository.save_report
    monkeypatch.setattr(SqliteAntiAiAuditRepository, "save_report", lambda *args: (_ for _ in ()).throw(RuntimeError("archive unavailable")))
    with pytest.raises(RuntimeError, match="archive unavailable"):
        await service.confirm("novel", item["candidate_id"], **tokens(item))
    saved = service.detail("novel", item["candidate_id"])
    assert saved["status"] == "committed" and not saved["audit"].get("final_audit") and not events
    monkeypatch.setattr(SqliteAntiAiAuditRepository, "save_report", original)
    result = await service.confirm("novel", item["candidate_id"], **tokens(item))
    assert result["candidate"]["audit"]["final_audit"] and result["candidate"]["audit"]["committed_revision"] == 1


@pytest.mark.asyncio
async def test_candidate_api_enforces_scope_and_mandatory_confirmation_tokens(daemon_writer, monkeypatch):
    item = await candidate(daemon_writer)
    service = reviewer(daemon_writer)
    monkeypatch.setattr(anti_ai, "_get_candidate_review", lambda **kwargs: service)
    app = FastAPI()
    app.include_router(anti_ai.router, prefix="/api/v1")
    with TestClient(app) as client:
        endpoint = f"/api/v1/anti-ai/novels/novel/candidates/{item['candidate_id']}"
        assert client.get(endpoint).json()["can_confirm"] is True
        assert client.get(endpoint.replace("/novels/novel/", "/novels/other/")).status_code == 404
        assert client.post(endpoint+"/confirm", json={"expected_candidate_hash": item["content_hash"]}).status_code == 422
        assert client.post(endpoint+"/confirm", json=tokens(item)).status_code == 200


@pytest.mark.asyncio
async def test_author_confirmation_derives_only_the_committed_revision_and_hash(daemon_writer):
    item = await candidate(daemon_writer)
    calls = []
    async def aftermath(*args, **kwargs):
        calls.append((args, kwargs))
        return {'narrative_sync_ok': True, 'derivation_status': 'completed'}
    service = reviewer(daemon_writer, aftermath=SimpleNamespace(run_after_chapter_saved=aftermath))
    result = await service.confirm('novel', item['candidate_id'], **tokens(item))
    args, kwargs = calls[0]
    assert args == ('novel', 1, CLEAN)
    assert kwargs['expected_revision'] == result['candidate']['audit']['committed_revision'] == 1
    assert kwargs['expected_content_hash'] == result['candidate']['content_hash']
    assert result['candidate']['audit']['aftermath']['derivation_status'] == 'completed'


@pytest.mark.asyncio
async def test_failed_aftermath_preserves_commit_and_retry_does_not_rewrite_body(daemon_writer):
    item = await candidate(daemon_writer)
    events, calls = [], []
    async def aftermath(*args, **kwargs):
        calls.append(kwargs)
        if len(calls) == 1:
            raise RuntimeError('derived state unavailable')
        return {'narrative_sync_ok': True, 'derivation_status': 'completed'}
    service = reviewer(daemon_writer, aftermath=SimpleNamespace(run_after_chapter_saved=aftermath), on_event=lambda *args: events.append(args))
    with pytest.raises(CandidateReviewError, match='派生尚未完成'):
        await service.confirm('novel', item['candidate_id'], **tokens(item))
    saved = service.detail('novel', item['candidate_id'])
    assert saved['status'] == 'committed' and saved['audit']['aftermath']['derivation_status'] == 'failed'
    assert not events and daemon_writer[0].fetch_one("SELECT revision FROM chapters WHERE id='chapter'")['revision'] == 1
    result = await service.confirm('novel', item['candidate_id'], **tokens(item))
    assert result['candidate']['audit']['committed_revision'] == 1
    assert calls[0]['expected_content_hash'] == calls[1]['expected_content_hash']
    assert events[-1][1] == 'audit_rewrite_committed'


@pytest.mark.asyncio
async def test_provider_error_details_are_not_persisted_in_candidate_audit(daemon_writer):
    item = await candidate(daemon_writer)
    secret = "https://provider.example/v1/chat/completions?api_key=secret-token"
    async def aftermath(*args, **kwargs):
        raise RuntimeError(secret)
    service = reviewer(daemon_writer, aftermath=SimpleNamespace(run_after_chapter_saved=aftermath))
    with pytest.raises(CandidateReviewError):
        await service.confirm("novel", item["candidate_id"], **tokens(item))
    saved = service.candidates.get(item["candidate_id"])
    assert secret not in str(saved["audit"])
    assert saved["audit"]["aftermath"]["error"].startswith("章后关联分析不可用（RuntimeError）")
