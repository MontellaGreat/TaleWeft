"""Extraction failures retain the committed summary and report failure upstream."""
import sqlite3
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from application.engine.services.chapter_aftermath_pipeline import ChapterAftermathPipeline
from application.world.services import chapter_narrative_sync as narrative
from application.world.services.knowledge_service import KnowledgeService
from domain.ai.value_objects.prompt import Prompt
from domain.novel.value_objects.tension_dimensions import TensionDimensions
from infrastructure.persistence.database.connection import DatabaseConnection
from infrastructure.persistence.database.sqlite_knowledge_repository import SqliteKnowledgeRepository
from tests.unit.application.engine.test_persistence_commit_ack import writer


@pytest.fixture
def knowledge(tmp_path, monkeypatch):
    db = DatabaseConnection(str(tmp_path / "summary-guards.db"))
    db.execute("INSERT INTO novels (id, title, slug) VALUES ('novel-summary', 'Summary', 'summary')")
    db.commit()
    service = KnowledgeService(SqliteKnowledgeRepository(db))
    service.upsert_chapter_summary("novel-summary", 2, summary="旧有效摘要", key_events="旧事件",
                                   open_threads="旧悬念", beat_sections=["旧节拍"], sync_status="synced")
    monkeypatch.setattr("infrastructure.ai.prompt_utils.render_required_prompt", lambda *args: Prompt(system="extract", user="text"))
    monkeypatch.setattr(narrative, "_resolve_beat_sections", lambda novel, number, existing, database=None: list(existing))
    monkeypatch.setattr(narrative, "persist_bundle_memory_atoms", lambda *args, **kwargs: 0)
    monkeypatch.setattr("application.analyst.services.tension_scoring_service.TensionScoringService.score_chapter",
                        AsyncMock(return_value=TensionDimensions(50, 50, 50, 50)))
    yield service, db
    db.close()


def _snapshot(db):
    return [dict(row) for row in db.fetch_all("SELECT * FROM chapter_summaries ORDER BY id")]


@pytest.mark.asyncio
@pytest.mark.parametrize("response", ["不是 JSON", "", "{}", '{"summary":null}', '{"summary":[]}', '{"summary":"  "}'])
async def test_invalid_llm_summary_retains_previous_sqlite_summary_and_skips_vector(knowledge, response):
    service, db = knowledge
    before = _snapshot(db)
    llm = SimpleNamespace(generate=AsyncMock(return_value=SimpleNamespace(content=response)))
    indexing = SimpleNamespace(ensure_collection=AsyncMock(), index_chapter_summary=AsyncMock())
    flags = await narrative.sync_chapter_narrative_after_save("novel-summary", 2, "新的章节正文", service, indexing, llm)
    assert flags["narrative_sync_ok"] is False and flags["sync_status"] == "failed"
    assert flags["sync_error"]
    assert _snapshot(db) == before
    indexing.ensure_collection.assert_not_called()
    indexing.index_chapter_summary.assert_not_called()


@pytest.mark.asyncio
async def test_provider_failure_does_not_replace_old_summary_with_empty_draft(knowledge):
    service, db = knowledge
    before = _snapshot(db)
    llm = SimpleNamespace(generate=AsyncMock(side_effect=RuntimeError("provider down")))
    flags = await narrative.sync_chapter_narrative_after_save("novel-summary", 2, "正文", service, None, llm)
    assert not flags["narrative_sync_ok"]
    assert "provider down" in flags["sync_error"]
    assert _snapshot(db) == before


@pytest.mark.asyncio
async def test_summary_commit_error_is_not_reported_as_synced(knowledge, monkeypatch):
    service, db = knowledge
    before = _snapshot(db)
    monkeypatch.setattr(service.knowledge_repository, "save", Mock(side_effect=sqlite3.OperationalError("write failed")))
    llm = SimpleNamespace(generate=AsyncMock(return_value=SimpleNamespace(content='{"summary":"新有效摘要"}')))
    flags = await narrative.sync_chapter_narrative_after_save("novel-summary", 2, "正文", service, None, llm)
    assert flags["narrative_sync_ok"] is False
    assert "write failed" in flags["sync_error"]
    assert _snapshot(db) == before


@pytest.mark.asyncio
async def test_valid_summary_commit_returns_explicit_success_and_indexes_same_summary(knowledge):
    service, db = knowledge
    llm = SimpleNamespace(generate=AsyncMock(return_value=SimpleNamespace(content='{"summary":"新有效摘要"}')))
    indexing = SimpleNamespace(ensure_collection=AsyncMock(), index_chapter_summary=AsyncMock())
    flags = await narrative.sync_chapter_narrative_after_save("novel-summary", 2, "正文", service, indexing, llm)
    assert flags["narrative_sync_ok"] is True and flags["sync_status"] == "synced"
    assert service.get_knowledge("novel-summary").get_chapter(2).summary == "新有效摘要"
    indexing.index_chapter_summary.assert_awaited_once_with("novel-summary", 2, "新有效摘要")


@pytest.mark.asyncio
@pytest.mark.parametrize("sync_flags,expected", [
    ({"narrative_sync_ok": False, "sync_status": "failed", "sync_error": "extraction failed"}, False),
    ({}, False),
    ({"narrative_sync_ok": True, "sync_status": "synced"}, True),
])
async def test_aftermath_success_requires_explicit_sync_result(monkeypatch, sync_flags, expected, writer):
    monkeypatch.setattr(narrative, "sync_chapter_narrative_after_save", AsyncMock(return_value=sync_flags))
    pipeline = ChapterAftermathPipeline(Mock(), None, Mock())
    pipeline._database = lambda: writer[1]
    pipeline._extract_chapter_bridge = AsyncMock()
    pipeline._schedule_auxiliary_stages = Mock()
    result = await pipeline.run_after_chapter_saved("novel-summary", 2, "正文")
    assert result["narrative_sync_ok"] is expected
    if sync_flags.get("sync_error"):
        assert result["narrative_sync_error"] == sync_flags["sync_error"]
