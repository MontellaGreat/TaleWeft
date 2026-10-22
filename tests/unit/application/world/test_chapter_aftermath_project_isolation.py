"""Same-id projects keep every chapter side effect in the injected database."""
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from application.engine.services.chapter_aftermath_pipeline import ChapterAftermathPipeline
from application.world.services import chapter_narrative_sync as narrative
from application.world.services.knowledge_service import KnowledgeService
from domain.novel.value_objects.tension_dimensions import TensionDimensions
from infrastructure.persistence.database.connection import DatabaseConnection
from infrastructure.persistence.database.sqlite_chapter_repository import SqliteChapterRepository
from infrastructure.persistence.database.sqlite_knowledge_repository import SqliteKnowledgeRepository
from infrastructure.persistence.database.sqlite_storyline_repository import SqliteStorylineRepository


@pytest.fixture
def projects(tmp_path, monkeypatch):
    databases = [DatabaseConnection(str(tmp_path / f"project-{n}.sqlite")) for n in range(2)]
    for n, db in enumerate(databases):
        db.execute("INSERT INTO novels(id,title,slug) VALUES('book',?,'book')", (f"Project {n}",))
        db.execute("INSERT INTO chapters(id,novel_id,number,title,content) VALUES('chapter','book',1,'Title','正文')")
        db.execute("INSERT INTO story_nodes(id,novel_id,node_type,number,title,order_index,outline) "
                   "VALUES('node','book','chapter',1,'Title',1,?)", (f"项目{n}专属节拍",))
        db.commit()

    def scoped_only(db_path=None):
        assert db_path is not None, "Default project database was used"
        for db in databases:
            if str(db_path) == str(db.db_path):
                return db
        raise AssertionError(f"Unexpected project path: {db_path}")

    monkeypatch.setattr("infrastructure.persistence.database.connection.get_database", scoped_only)
    yield databases
    for db in databases:
        db.close_all()


def test_bundle_tension_timeline_snapshot_memory_and_storyline_stay_in_project(projects, monkeypatch):
    left, right = projects
    bundle = {
        "summary": "林澈发现铜铃的线索",
        "tension_score": 72,
        "timeline_events": [{"time_point": "夜", "event": "铜铃响起", "description": "雨后"}],
        "character_states": [{"character_name": "林澈", "mental_state": "警觉"}],
        "storyline_progress": [{"type": "main_plot", "description": "铜铃线索推进", "arc_label": "铜铃"}],
    }
    monkeypatch.setattr("application.engine.services.persistence_queue.get_persistence_queue",
                        lambda: (_ for _ in ()).throw(AssertionError("Default writer queue used")))
    for _ in range(2):
        narrative.persist_bundle_extras("book", 1, bundle,
            storyline_repository=SqliteStorylineRepository(left), chapter_repository=SqliteChapterRepository(left))
        assert narrative.persist_bundle_memory_atoms("book", 1, bundle, database=left) == 1
    assert left.fetch_one("SELECT tension_score FROM chapters WHERE id='chapter'")["tension_score"] == 72
    assert right.fetch_one("SELECT tension_score FROM chapters WHERE id='chapter'")["tension_score"] != 72
    for table in ("bible_timeline_notes", "novel_snapshots", "memory_atoms", "storylines"):
        assert len(left.fetch_all(f"SELECT * FROM {table}")) == 1
        assert right.fetch_all(f"SELECT * FROM {table}") == []


@pytest.mark.asyncio
async def test_sync_resolves_project_from_knowledge_repository_for_outline_and_memory(projects, monkeypatch):
    left, right = projects
    monkeypatch.setattr(narrative, "llm_chapter_extract_bundle", AsyncMock(return_value={
        "summary": "项目0章末摘要", "character_states": [{"character_name": "林澈", "mental_state": "警觉"}]}))
    monkeypatch.setattr("application.analyst.services.tension_scoring_service.TensionScoringService.score_chapter",
                        AsyncMock(return_value=TensionDimensions(50, 50, 50, 50)))
    service = KnowledgeService(SqliteKnowledgeRepository(left))
    result = await narrative.sync_chapter_narrative_after_save("book", 1, "正文", service, None, None)
    assert result["narrative_sync_ok"] and result["memory_atoms_stored"]
    summary = service.get_knowledge("book").get_chapter(1)
    assert summary.beat_sections == ["项目0专属节拍"]
    assert len(left.fetch_all("SELECT * FROM memory_atoms")) == 1
    assert right.fetch_all("SELECT * FROM memory_atoms") == []
    assert right.fetch_all("SELECT * FROM chapter_summaries") == []


@pytest.mark.asyncio
async def test_bridge_guardrail_trace_governance_and_kg_use_chapter_project(projects, monkeypatch):
    from application.engine.services.chapter_bridge_service import ChapterBridgeService
    from infrastructure.persistence.database.sqlite_governance_repository import SqliteGovernanceRepository
    from infrastructure.persistence.database.chapter_guardrail_snapshot_repository import ChapterGuardrailSnapshotRepository
    from engine.infrastructure.persistence.trace_store import SqliteTraceStore

    left, right = projects
    # Ensure comparison tables exist before checking that project 1 stayed empty.
    SqliteGovernanceRepository(right)
    ChapterGuardrailSnapshotRepository(right)
    SqliteTraceStore(right)
    monkeypatch.setattr(ChapterBridgeService, "_invoke_helper_text", AsyncMock(return_value='{"suspense_hook":"铜铃未解"}'))
    # A mismatched knowledge dependency must not override the chapter repository.
    pipeline = ChapterAftermathPipeline(KnowledgeService(SqliteKnowledgeRepository(right)), None,
        SimpleNamespace(), chapter_repository=SqliteChapterRepository(left))
    assert pipeline._database() is left
    await pipeline._extract_chapter_bridge("book", 1, "正文")
    evidence = {}
    await pipeline._run_auxiliary_stages("book", 1, "林澈推开窗，铜铃响起。", evidence)
    assert "guardrail_score" in evidence and "governance_report" in evidence
    assert len(left.fetch_all("SELECT * FROM chapter_bridges")) == 1
    for table in ("chapter_guardrail_snapshots", "engine_traces", "narrative_contracts", "governance_reports"):
        assert len(left.fetch_all(f"SELECT * FROM {table}")) >= 1
        assert right.fetch_all(f"SELECT * FROM {table}") == []
