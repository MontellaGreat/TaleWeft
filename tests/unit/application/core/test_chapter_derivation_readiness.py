import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from application.core.services.chapter_derivation_readiness import (
    ChapterDerivationsNotReady, assert_previous_derivations_ready,
)
from infrastructure.persistence.database.connection import DatabaseConnection


@pytest.fixture
def db(tmp_path):
    database = DatabaseConnection(str(tmp_path / "readiness.sqlite"))
    database.execute("INSERT INTO novels(id,title,author,premise,slug) VALUES('book','测试','作者','测试','readiness')")
    database.execute("INSERT INTO chapters(id,novel_id,number,title,content,revision) VALUES('one','book',1,'第一章','新正文',2)")
    database.commit()
    yield database
    database.close_all(skip_checkpoint=True)


def add_job(db, revision=2, status="pending"):
    db.execute("INSERT INTO chapter_derivation_jobs(job_id,chapter_id,novel_id,chapter_number,revision,payload_json,status) "
               "VALUES(?,?,?,?,?,?,?)", (f"job-{revision}", "one", "book", 1, revision, json.dumps({"content": "新正文"}), status))
    db.commit()


@pytest.mark.parametrize("status", ["pending", "running", "unavailable", "superseded"])
def test_unfinished_current_revision_blocks_later_chapter(db, status):
    add_job(db, status=status)
    with pytest.raises(ChapterDerivationsNotReady, match="重跑关联工作流"):
        assert_previous_derivations_ready(db, "book", 2)
    assert_previous_derivations_ready(db, "other-book", 2)
    assert_previous_derivations_ready(db, "book", 1)


def test_old_revisions_and_books_without_reconciliation_are_compatible(db):
    assert_previous_derivations_ready(db, "book", 2)
    add_job(db, revision=1, status="unavailable")
    add_job(db, status="completed")
    assert_previous_derivations_ready(db, "book", 2)


@pytest.mark.parametrize("cascade_status", ["running", "unavailable"])
def test_parent_completed_does_not_acknowledge_unfinished_cascade(db, cascade_status):
    add_job(db, status="completed")
    db.execute("UPDATE chapter_derivation_jobs SET cascade_status=?", (cascade_status,))
    db.commit()
    with pytest.raises(ChapterDerivationsNotReady):
        assert_previous_derivations_ready(db, "book", 2)


def test_completed_index_job_does_not_unblock_invalidated_summary(db):
    add_job(db, status="completed")
    db.execute("INSERT INTO knowledge(id,novel_id) VALUES('knowledge','book')")
    db.execute("INSERT INTO chapter_summaries(knowledge_id,chapter_number,summary,sync_status) VALUES('knowledge',1,'旧摘要','stale')")
    db.commit()
    with pytest.raises(ChapterDerivationsNotReady, match="stale"):
        assert_previous_derivations_ready(db, "book", 2)
    db.execute("UPDATE chapter_summaries SET sync_status='synced'")
    db.commit()
    assert_previous_derivations_ready(db, "book", 2)


def test_withdrawn_empty_chapter_keeps_override_history_without_blocking_forever(db):
    add_job(db, status="completed")
    db.execute("UPDATE chapters SET content='' WHERE id='one'")
    db.execute("INSERT INTO chapter_evolution_snapshots(snapshot_id,novel_id,chapter_number,status) "
               "VALUES('old-override','book',1,'stale')")
    db.commit()
    assert_previous_derivations_ready(db, "book", 2)


@pytest.mark.asyncio
@pytest.mark.parametrize("stream", [False, True])
async def test_final_prose_boundary_blocks_before_provider_or_prompt_assembly(db, stream):
    from application.engine.rules.prose_prompt_hooks import generate_direct_prose
    from application.engine.rules.anti_ai_stream_guard import prepare_direct_prose_stream
    from domain.ai.value_objects.prompt import Prompt
    from domain.ai.services.llm_service import GenerationConfig

    add_job(db)
    provider = SimpleNamespace(generate=AsyncMock(), stream_generate=AsyncMock())
    with pytest.raises(ChapterDerivationsNotReady):
        if stream:
            prepare_direct_prose_stream(provider, Prompt(system="规则", user="正文"), GenerationConfig(),
                context={"novel_id": "book", "chapter_number": 2}, db=db)
        else:
            await generate_direct_prose(provider, Prompt(system="规则", user="正文"), GenerationConfig(),
                context={"novel_id": "book", "chapter_number": 2}, db=db)
    provider.generate.assert_not_called()
    provider.stream_generate.assert_not_called()


def test_allocator_and_workflow_fallback_cannot_swallow_readiness_failure(db):
    from application.engine.services.context_budget_allocator import ContextBudgetAllocator
    from application.workflows.auto_novel_generation_workflow import AutoNovelGenerationWorkflow
    from infrastructure.persistence.database.sqlite_chapter_repository import SqliteChapterRepository

    add_job(db)
    repo = SqliteChapterRepository(db)
    allocator = ContextBudgetAllocator(chapter_repository=repo)
    with pytest.raises(ChapterDerivationsNotReady):
        allocator.allocate("book", 2, "下一章")
    workflow = object.__new__(AutoNovelGenerationWorkflow)
    workflow.context_builder = SimpleNamespace(chapter_repository=repo)
    for entry in (workflow.prepare_chapter_generation, workflow.build_fallback_chapter_bundle):
        with pytest.raises(ChapterDerivationsNotReady):
            entry("book", 2, "下一章")
