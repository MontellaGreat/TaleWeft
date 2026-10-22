"""Edited prose must replace its evidence in persistent and cached T0 memory."""
import asyncio
import json
import sqlite3
from pathlib import Path
from types import SimpleNamespace

import pytest

from application.core.services.chapter_version_service import ChapterVersionService, content_hash
from application.engine.services.memory_engine import (
    MemoryEngine,
    MemoryState,
    invalidate_memory_engine_from_chapter,
)
from application.engine.services.memory_engine_settings import MemoryEngineRuntimeSettings
from infrastructure.persistence.database.chapter_write_fence import chapter_write_fence
from infrastructure.persistence.database.write_dispatch import TxnCollectingConnection


def _engine(db=None, llm=None):
    return MemoryEngine(
        llm_service=llm or SimpleNamespace(),
        bible_repository=SimpleNamespace(get_by_novel_id=lambda _: None),
        db_connection=db,
        runtime_settings=MemoryEngineRuntimeSettings(state_cache_ttl_seconds=3600, state_cache_max_size=2),
    )


def _state():
    return MemoryState(
        novel_id="book", last_updated_chapter=2,
        completed_beats=[{"beat_id": "b1", "summary": "OLD-A", "chapter": 1},
                         {"beat_id": "b2", "summary": "OLD-B", "source_chapter": 2, "chapter": 99},
                         {"beat_id": "legacy", "summary": "unattributed"}],
        revealed_clues=[{"clue_id": "c1", "content": "OLD-A", "revealed_at_chapter": 1},
                        {"clue_id": "c2", "content": "OLD-B", "source_chapter": 2}],
        fact_violations_history=[{"description": "v1", "chapter": 1},
                                 {"description": "v2", "source_chapter": 2}],
    )


def _payload(label="NEW-B"):
    return json.dumps({
        "completed_beats": [{"beat_id": "new-beat", "summary": label, "chapter": 99}],
        "revealed_clues": [{"clue_id": "new-clue", "content": label, "revealed_at_chapter": 99}],
        "fact_violations": [{"violation_type": "timeline_contradiction", "description": label}],
    })


class _LLM:
    def __init__(self, *responses):
        self.responses = list(responses)
        self.prompts = []

    async def generate(self, prompt, _config):
        self.prompts.append(str(prompt))
        response = self.responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return SimpleNamespace(content=response)


@pytest.fixture
def sqlite_db():
    connection = sqlite3.connect(":memory:")
    try:
        yield connection
    finally:
        connection.close()


@pytest.mark.parametrize("chapter,remaining", [(1, []), (2, ["b1"]), (3, ["b1", "b2"])])
def test_save_invalidation_discards_suffix_and_unattributed_legacy_evidence(sqlite_db, chapter, remaining):
    engine = _engine(sqlite_db)
    engine._persist_state("book", _state())
    invalidate_memory_engine_from_chapter(sqlite_db, "book", chapter)
    state = engine._get_or_load_state("book")
    assert [b["beat_id"] for b in state.completed_beats] == remaining
    assert len(state.revealed_clues) == len(remaining)
    assert len(state.fact_violations_history) == len(remaining)
    assert state.last_updated_chapter == min(2, chapter - 1)


def test_staged_invalidation_obeys_winning_job_gate_without_reading_or_committing(sqlite_db):
    engine = _engine(sqlite_db)
    engine._persist_state("book", _state())
    sqlite_db.execute("CREATE TABLE chapter_derivation_jobs(job_id TEXT,novel_id TEXT)")
    collector = TxnCollectingConnection()
    invalidate_memory_engine_from_chapter(collector, "book", 1, job_id="winner")
    for sql, parameters in collector.operations:
        sqlite_db.execute(sql, parameters)
    assert len(engine._load_from_db("book").completed_beats) == 3
    sqlite_db.execute("INSERT INTO chapter_derivation_jobs VALUES ('winner','other-book')")
    for sql, parameters in collector.operations:
        sqlite_db.execute(sql, parameters)
    assert len(engine._load_from_db("book").completed_beats) == 3
    sqlite_db.execute("INSERT INTO chapter_derivation_jobs VALUES ('winner','book')")
    for sql, parameters in collector.operations:
        sqlite_db.execute(sql, parameters)
    assert engine._load_from_db("book").completed_beats == []


def test_two_long_lived_instances_observe_same_second_edits_and_reset(sqlite_db):
    first, second = _engine(sqlite_db), _engine(sqlite_db)
    first._persist_state("book", _state())
    assert "OLD-A" in first.get_completed_beats_section("book")
    assert "OLD-A" in second.get_completed_beats_section("book")
    invalidate_memory_engine_from_chapter(sqlite_db, "book", 1)
    assert first.get_completed_beats_section("book") == second.get_completed_beats_section("book") == ""
    changed = MemoryState(novel_id="book", last_updated_chapter=1,
                          completed_beats=[{"beat_id": "new", "summary": "NEW-C", "chapter": 1}])
    second._persist_state("book", changed)
    assert "NEW-C" in first.get_completed_beats_section("book")
    assert "NEW-C" in second.get_completed_beats_section("book")
    first.reset("book")
    assert second.get_completed_beats_section("book") == ""


def test_cached_evidence_observes_an_edit_from_another_connection(tmp_path):
    path = str(tmp_path / "shared-memory.sqlite")
    with sqlite3.connect(path) as first_db, sqlite3.connect(path) as second_db:
        first, second = _engine(first_db), _engine(second_db)
        first._persist_state("book", _state())
        assert "OLD-B" in second.get_completed_beats_section("book")
        invalidate_memory_engine_from_chapter(first_db, "book", 2)
        first_db.commit()
        assert "OLD-B" not in second.get_completed_beats_section("book")
        assert "OLD-A" in second.get_completed_beats_section("book")


def test_rebuilding_earlier_chapter_excludes_future_machine_timeline_but_keeps_author_notes(sqlite_db):
    from application.world.services.chapter_narrative_sync import _stable_artifact_id
    from domain.bible.entities.timeline_note import TimelineNote
    sqlite_db.execute('CREATE TABLE chapter_extraction_snapshots(novel_id TEXT,chapter_number INTEGER,bundle_json TEXT)')
    notes = []
    for chapter in (1, 2, 3):
        item = {'time_point': f'day {chapter}', 'event': f'machine-event-{chapter}', 'description': 'model note'}
        note_id = _stable_artifact_id('tl', 'book', chapter, item['time_point'], item['event'], item['description'])
        notes.append(TimelineNote(id=note_id, **item))
        sqlite_db.execute('INSERT INTO chapter_extraction_snapshots VALUES(?,?,?)', ('book', chapter, json.dumps({'timeline_events': [item]})))
    notes.append(TimelineNote('manual', 'author chronology', 'future', 'author plan'))
    bible = SimpleNamespace(characters=[], timeline_notes=notes)
    engine = _engine(sqlite_db)
    engine.bible_repository.get_by_novel_id = lambda _: bible
    first = engine.build_fact_lock_section('book', 1)
    assert 'machine-event-' not in first
    assert 'author chronology' in first
    second = engine.build_fact_lock_section('book', 2)
    assert 'machine-event-1' in second
    assert 'machine-event-2' not in second and 'machine-event-3' not in second
    notes[2].event = 'author corrected chronology'
    assert 'author corrected chronology' in engine.build_fact_lock_section('book', 2)


@pytest.mark.asyncio
async def test_reconciliation_replaces_own_evidence_and_host_owns_source_attribution(sqlite_db):
    llm = _LLM(_payload())
    engine = _engine(sqlite_db, llm)
    engine._persist_state("book", _state())
    with chapter_write_fence("memory-provenance-test.sqlite", "source-chapter", 7, content_hash("revised chapter")):
        result = await engine.reconcile_from_chapter("book", 2, "revised chapter")
    assert result["errors"] == []
    state = engine._get_or_load_state("book")
    assert [b["summary"] for b in state.completed_beats] == ["OLD-A", "NEW-B"]
    assert "OLD-B" not in llm.prompts[0]
    assert state.completed_beats[-1]["chapter"] == 2
    assert state.revealed_clues[-1]["revealed_at_chapter"] == 2
    for item in (state.completed_beats[-1], state.revealed_clues[-1], state.fact_violations_history[-1]):
        assert item["source"] == "chapter_extract"
        assert item["source_chapter"] == 2
        assert item["source_revision"] == 7
        assert item["source_content_hash"] == content_hash("revised chapter")


@pytest.mark.asyncio
async def test_retry_is_replacement_and_empty_body_clears_without_model(sqlite_db):
    llm = _LLM(_payload("version-1"), _payload("version-2"))
    engine = _engine(sqlite_db, llm)
    assert (await engine.reconcile_from_chapter("book", 1, "body-v1"))["errors"] == []
    assert (await engine.reconcile_from_chapter("book", 1, "body-v2"))["errors"] == []
    assert "version-1" not in engine.get_completed_beats_section("book")
    assert "version-2" in engine.get_completed_beats_section("book")
    result = await engine.reconcile_from_chapter("book", 1, "  ")
    assert result["errors"] == []
    assert engine.get_completed_beats_section("book") == ""
    assert engine.get_revealed_clues_section("book") == ""
    assert len(llm.prompts) == 2


@pytest.mark.asyncio
@pytest.mark.parametrize("response", [RuntimeError("model unavailable"), "not-json", '{"completed_beats": [{}]}'])
async def test_failed_rebuild_reports_error_and_never_restores_old_edited_facts(sqlite_db, response):
    engine = _engine(sqlite_db, _LLM(response))
    engine._persist_state("book", _state())
    result = await engine.reconcile_from_chapter("book", 2, "revised")
    assert result["errors"]
    assert "OLD-A" in engine.get_completed_beats_section("book")
    assert "OLD-B" not in engine.get_completed_beats_section("book")


@pytest.mark.asyncio
async def test_write_failure_does_not_publish_uncommitted_evidence_to_cache(sqlite_db, monkeypatch):
    engine = _engine(sqlite_db, _LLM(_payload()))
    engine._persist_state("book", _state())
    assert "OLD-A" in engine.get_completed_beats_section("book")

    def reject(*_):
        raise RuntimeError("writer denied")

    monkeypatch.setattr(engine, "_upsert_state_row", reject)
    result = await engine.update_from_chapter("book", 3, "new body", "")
    assert any("writer denied" in error for error in result["errors"])
    assert "NEW-B" not in engine.get_completed_beats_section("book")
    assert "NEW-B" not in sqlite_db.execute("SELECT state_json FROM memory_engine_state").fetchone()[0]


@pytest.mark.asyncio
async def test_commit_failure_rolls_back_evidence_even_on_the_same_connection(sqlite_db):
    class FailingCommit:
        fail = False

        def execute(self, *args):
            return sqlite_db.execute(*args)

        def commit(self):
            if self.fail:
                raise RuntimeError("disk full during commit")
            sqlite_db.commit()

        def rollback(self):
            sqlite_db.rollback()

    database = FailingCommit()
    engine = _engine(database, _LLM(_payload()))
    engine._persist_state("book", _state())
    engine.get_completed_beats_section("book")
    database.fail = True
    result = await engine.update_from_chapter("book", 3, "new body", "")
    assert any("disk full" in error for error in result["errors"])
    assert "NEW-B" not in engine.get_completed_beats_section("book")
    assert "NEW-B" not in sqlite_db.execute("SELECT state_json FROM memory_engine_state").fetchone()[0]


@pytest.mark.asyncio
async def test_failed_snapshot_read_cannot_overwrite_existing_prefix(sqlite_db, monkeypatch):
    engine = _engine(sqlite_db, _LLM(_payload()))
    engine._persist_state("book", _state())
    sqlite_db.execute("UPDATE memory_engine_state SET state_json='broken-json'")
    result = await engine.update_from_chapter("book", 3, "new body", "")
    assert result["errors"]
    assert engine.llm_service.prompts == []
    assert sqlite_db.execute("SELECT state_json FROM memory_engine_state").fetchone()[0] == "broken-json"


@pytest.fixture
def writer_db(tmp_path, monkeypatch):
    from application.engine.services import persistence_queue as queue_module
    from application.engine.services.persistence_queue import PersistenceQueue, register_persistence_handlers
    from infrastructure.persistence.database import connection as connection_module
    from infrastructure.persistence.database.connection import DatabaseConnection
    monkeypatch.delenv("PLOTPILOT_ALLOW_DIRECT_SQLITE_WRITES", raising=False)
    monkeypatch.delenv("AITEXT_ALLOW_DIRECT_SQLITE_WRITES", raising=False)
    db = DatabaseConnection(str(tmp_path / "memory-edits.sqlite"))
    monkeypatch.setattr(connection_module, "_db_instance", db)
    monkeypatch.setattr(connection_module, "_db_instances_by_path", {str(Path(db.db_path).resolve()): db})
    queue = PersistenceQueue()
    queue.initialize()
    monkeypatch.setattr(queue_module, "_persistence_queue", queue)
    register_persistence_handlers()
    queue.start_consumer()
    db.execute("INSERT INTO novels(id,title,slug) VALUES ('book','Book','book')")
    db.execute("INSERT INTO chapters(id,novel_id,number,content) VALUES ('c','book',1,'old body')")
    try:
        yield db
    finally:
        queue.stop_consumer()
        db.close_all(skip_checkpoint=True)
        queue.get_queue().close()
        queue.get_queue().join_thread()


def test_winning_chapter_save_invalidates_memory_in_its_real_writer_transaction(writer_db):
    engine = _engine(writer_db)
    engine._persist_state("book", _state())
    engine.get_completed_beats_section("book")
    saved = ChapterVersionService(writer_db).save_if_revision(
        "c", 0, "revised body", pending_job={"job_id": "winning-job", "payload": {}})
    assert saved["derivation_job_id"] == "winning-job"
    assert engine.get_completed_beats_section("book") == ""
    assert engine.get_revealed_clues_section("book") == ""
    assert engine.get_state_summary("book")["last_updated_chapter"] == 0


def test_losing_chapter_cas_cannot_invalidate_another_saves_memory(writer_db, monkeypatch):
    from application.core.services.chapter_version_service import RevisionConflictError
    engine = _engine(writer_db)
    engine._persist_state("book", _state())
    versions = ChapterVersionService(writer_db)
    prepare = versions.prepare_save

    def lose_after_preflight(*args, **kwargs):
        statement = prepare(*args, **kwargs)
        ChapterVersionService(writer_db).save_if_revision("c", 0, "winning body")
        return statement

    monkeypatch.setattr(versions, "prepare_save", lose_after_preflight)
    with pytest.raises(RevisionConflictError):
        versions.save_if_revision("c", 0, "losing body", pending_job={"job_id": "losing-job", "payload": {}})
    assert "OLD-A" in engine.get_completed_beats_section("book")
    assert writer_db.fetch_one("SELECT job_id FROM chapter_derivation_jobs WHERE job_id='losing-job'") is None


@pytest.mark.asyncio
async def test_writer_rejects_late_memory_after_edit_while_model_is_running(writer_db):
    entered, release = asyncio.Event(), asyncio.Event()

    class WaitingLLM(_LLM):
        async def generate(self, prompt, config):
            entered.set()
            await release.wait()
            return await super().generate(prompt, config)

    engine = _engine(writer_db, WaitingLLM(_payload("LATE-OLD-FACT")))
    engine._persist_state("book", _state())

    async def run():
        with chapter_write_fence(writer_db.db_path, "c", 0, content_hash("old body")):
            return await engine.reconcile_from_chapter("book", 1, "old body")

    task = asyncio.create_task(run())
    await asyncio.wait_for(entered.wait(), timeout=10)
    await asyncio.to_thread(ChapterVersionService(writer_db).save_if_revision, "c", 0, "new body")
    release.set()
    result = await task
    assert any("superseded" in error for error in result["errors"])
    assert "LATE-OLD-FACT" not in engine.get_completed_beats_section("book")
    assert "LATE-OLD-FACT" not in writer_db.fetch_one("SELECT state_json FROM memory_engine_state WHERE novel_id='book'")["state_json"]

