"""Real writer tests for body edits racing revision-bound derived work."""
import asyncio
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace

import pytest

from application.core.services.chapter_version_service import ChapterVersionService, content_hash
from application.engine.services import persistence_queue as queue_module
from application.engine.services.persistence_queue import PersistenceQueue, PersistenceCommitError, register_persistence_handlers
from infrastructure.persistence.database.connection import DatabaseConnection
from infrastructure.persistence.database.chapter_write_fence import chapter_write_fence, ChapterWriteSuperseded
from infrastructure.persistence.database.sqlite_knowledge_repository import SqliteKnowledgeRepository


@pytest.fixture
def fenced_db(tmp_path, monkeypatch):
    monkeypatch.delenv("PLOTPILOT_ALLOW_DIRECT_SQLITE_WRITES", raising=False)
    monkeypatch.delenv("AITEXT_ALLOW_DIRECT_SQLITE_WRITES", raising=False)
    from infrastructure.persistence.database import connection as connection_module
    db = DatabaseConnection(str(tmp_path / "fenced.sqlite"))
    monkeypatch.setattr(connection_module, "_db_instance", db)
    monkeypatch.setattr(connection_module, "_db_instances_by_path", {str(Path(db.db_path).resolve()): db})
    queue = PersistenceQueue()
    queue.initialize()
    monkeypatch.setattr(queue_module, "_persistence_queue", queue)
    register_persistence_handlers()
    queue.start_consumer()
    db.execute("INSERT INTO novels(id,title,slug) VALUES ('book','Book','book')")
    db.execute("INSERT INTO chapters(id,novel_id,number,content) VALUES ('c','book',1,'old body')")
    db.execute("CREATE TABLE derivation_probe(value TEXT)")
    try:
        yield db
    finally:
        queue.stop_consumer()
        db.close_all(skip_checkpoint=True)
        queue.get_queue().close()
        queue.get_queue().join_thread()


@pytest.mark.parametrize("path", ["execute", "cursor", "transaction", "many"])
def test_stale_sql_is_rejected_at_writer_for_every_connection_path(fenced_db, path):
    db = fenced_db
    ChapterVersionService(db).save_if_revision("c", 0, "new body")
    with chapter_write_fence(db.db_path, "c", 0, content_hash("old body")):
        with pytest.raises(PersistenceCommitError, match="superseded"):
            if path == "execute":
                db.execute("INSERT INTO derivation_probe VALUES ('stale')")
            elif path == "cursor":
                db.get_connection().cursor().execute("INSERT INTO derivation_probe VALUES ('stale')")
            elif path == "many":
                db.execute_many("INSERT INTO derivation_probe VALUES (?)", [("stale",), ("also stale",)])
            else:
                with db.transaction() as conn:
                    conn.execute("INSERT INTO derivation_probe VALUES ('stale')")
    assert db.fetch_all("SELECT * FROM derivation_probe") == []


def test_transaction_rolls_back_all_derived_writes_if_body_changes_inside_it(fenced_db):
    db = fenced_db
    with chapter_write_fence(db.db_path, "c", 0, content_hash("old body")):
        with pytest.raises(PersistenceCommitError, match="superseded"):
            with db.transaction() as conn:
                conn.execute("INSERT INTO derivation_probe VALUES ('must roll back')")
                conn.execute("UPDATE chapters SET content='wrong update' WHERE id='c'")
    assert db.fetch_all("SELECT * FROM derivation_probe") == []
    assert db.fetch_one("SELECT content,revision FROM chapters WHERE id='c'") == {"content": "old body", "revision": 0}


def test_direct_mode_commit_fence_keeps_executemany_in_the_same_transaction(fenced_db, monkeypatch):
    db = fenced_db
    monkeypatch.setenv("PLOTPILOT_ALLOW_DIRECT_SQLITE_WRITES", "1")
    with chapter_write_fence(db.db_path, "c", 0, content_hash("old body")):
        with pytest.raises(ChapterWriteSuperseded):
            with db.transaction() as conn:
                conn.executemany("INSERT INTO derivation_probe VALUES (?)", [("one",), ("two",)])
                conn.execute("UPDATE chapters SET content='wrong update' WHERE id='c'")
    assert db.fetch_all("SELECT * FROM derivation_probe") == []
    assert db.fetch_one("SELECT content FROM chapters WHERE id='c'")["content"] == "old body"


@pytest.mark.asyncio
async def test_to_thread_carries_fence_after_model_wait_and_rejects_new_body(fenced_db):
    db = fenced_db
    entered, release = asyncio.Event(), asyncio.Event()

    async def derived_work():
        with chapter_write_fence(db.db_path, "c", 0, content_hash("old body")):
            entered.set()
            await release.wait()
            await asyncio.to_thread(db.get_connection().cursor().execute, "INSERT INTO derivation_probe VALUES ('late')")

    task = asyncio.create_task(derived_work())
    await entered.wait()
    await asyncio.to_thread(ChapterVersionService(db).save_if_revision, "c", 0, "new body")
    release.set()
    with pytest.raises(PersistenceCommitError, match="superseded"):
        await task
    assert db.fetch_all("SELECT * FROM derivation_probe") == []


def test_microbeat_patch_cas_race_does_not_touch_new_summary_or_other_fields(fenced_db, monkeypatch):
    db = fenced_db
    repo = SqliteKnowledgeRepository(db)
    repo.save_knowledge("book")
    db.execute("INSERT INTO chapter_summaries(id,knowledge_id,chapter_number,summary,micro_beats) VALUES ('s','book-knowledge',1,'new summary','[]')")
    original_transaction = db.transaction

    @contextmanager
    def body_changes_before_writer_batch():
        db.execute("UPDATE chapters SET content='new body' WHERE id='c'")
        with original_transaction() as conn:
            yield conn

    monkeypatch.setattr(db, "transaction", body_changes_before_writer_batch)
    assert repo.patch_chapter_micro_beats_if_revision("book", 1, [{"description": "old beat"}], 0, content_hash("old body")) is False
    assert db.fetch_one("SELECT summary,micro_beats FROM chapter_summaries WHERE id='s'") == {"summary": "new summary", "micro_beats": "[]"}


@pytest.mark.asyncio
async def test_aftermath_entry_binds_fence_and_detects_body_changed_during_model(fenced_db, monkeypatch):
    from application.engine.services.chapter_aftermath_pipeline import ChapterAftermathPipeline
    db = fenced_db
    pipeline = ChapterAftermathPipeline(None, None, None, chapter_repository=SimpleNamespace(db=db))
    entered, release = asyncio.Event(), asyncio.Event()

    async def simulated_llm_stage(*_args):
        entered.set()
        await release.wait()
        with pytest.raises(PersistenceCommitError, match="superseded"):
            await asyncio.to_thread(db.execute, "INSERT INTO derivation_probe VALUES ('late summary')")
        return {"narrative_sync_status": "failed"}

    monkeypatch.setattr(pipeline, "_run_after_chapter_saved", simulated_llm_stage)
    task = asyncio.create_task(pipeline.run_after_chapter_saved("book", 1, "old body"))
    await entered.wait()
    await asyncio.to_thread(ChapterVersionService(db).save_if_revision, "c", 0, "new body")
    release.set()
    assert (await task)["narrative_sync_status"] == "superseded"
    assert db.fetch_all("SELECT * FROM derivation_probe") == []


@pytest.mark.asyncio
async def test_aftermath_explicit_version_rejects_aba_without_running_model(fenced_db):
    from application.engine.services.chapter_aftermath_pipeline import ChapterAftermathPipeline
    db = fenced_db
    versions = ChapterVersionService(db)
    await asyncio.to_thread(versions.save_if_revision, "c", 0, "intermediate")
    await asyncio.to_thread(versions.save_if_revision, "c", 1, "old body")
    pipeline = ChapterAftermathPipeline(None, None, None, chapter_repository=SimpleNamespace(db=db))
    assert (await pipeline.run_after_chapter_saved("book", 1, "old body", expected_revision=0, expected_content_hash=content_hash("old body")))["derivation_status"] == "superseded"


def test_close_all_invalidates_current_and_other_thread_connection_caches(fenced_db):
    db = fenced_db
    old_main = db.get_connection()
    with ThreadPoolExecutor(max_workers=1) as executor:
        old_worker = executor.submit(db.get_connection).result()
        db.close_all(skip_checkpoint=True)
        new_main = db.get_connection()
        new_worker = executor.submit(db.get_connection).result()
        assert new_main is not old_main
        assert new_worker is not old_worker
        assert executor.submit(db.fetch_one, "SELECT content FROM chapters WHERE id='c'").result()["content"] == "old body"
        # The writer's cache is also from the old generation.
        assert db.execute("INSERT INTO derivation_probe VALUES ('after reopen')").rowcount == 1


def test_upsert_repeated_saves_and_recreated_id_keep_distinct_immutable_versions(fenced_db):
    from infrastructure.persistence.database.sqlite_chapter_repository import SqliteChapterRepository
    from domain.novel.entities.chapter import Chapter
    from domain.novel.value_objects.novel_id import NovelId
    db = fenced_db
    repo = SqliteChapterRepository(db)
    chapter = repo.get_by_novel_and_number(NovelId("book"), 1)
    chapter.update_content("second body")
    repo.save(chapter)
    chapter.update_content("third body")
    repo.save(chapter)
    assert chapter.revision == 2
    db.execute("DELETE FROM chapters WHERE id='c'")
    recreated = Chapter("c", NovelId("book"), 1, "recreated", "fourth body")
    repo.save(recreated)
    assert recreated.revision == 3
    assert [row["content"] for row in db.fetch_all("SELECT content FROM content_versions WHERE chapter_id='c' ORDER BY revision")] == ["old body", "second body", "third body", "fourth body"]
