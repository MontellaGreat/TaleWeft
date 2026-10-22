"""HTTP contract tests for chapter revision/ETag saves."""

import asyncio
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest
from fastapi import BackgroundTasks, Response
from fastapi import HTTPException

from application.core.dtos.chapter_dto import ChapterDTO
from infrastructure.persistence.database.connection import DatabaseConnection
from interfaces.api.v1.core.chapters import (
    UpdateChapterContentRequest,
    get_chapter,
    update_chapter,
)


class _Service:
    def __init__(self, db):
        self.chapter_repository = type("Repo", (), {"db": db})()

    def get_chapter_by_novel_and_number(self, novel_id, chapter_number):
        row = self.chapter_repository.db.fetch_one(
            "SELECT * FROM chapters WHERE novel_id=? AND number=?",
            (novel_id, chapter_number),
        )
        if row is None:
            return None
        from application.core.services.chapter_version_service import ChapterVersionService

        snap = ChapterVersionService(self.chapter_repository.db).read(row["id"])
        return ChapterDTO(
            id=snap["id"], novel_id=snap["novel_id"], number=snap["number"],
            title=snap["title"], content=snap["content"], word_count=len(snap["content"]),
            status=snap["status"], revision=snap["revision"],
            content_hash=snap["content_hash"], etag=snap["etag"],
            created_at=str(snap.get("created_at") or ""),
            updated_at=str(snap.get("updated_at") or ""),
        )


class _Pipeline:
    async def run_after_chapter_saved(self, *args, **kwargs):
        return None


class _Knowledge:
    def patch_chapter_micro_beats(self, *args, **kwargs):
        return None


@pytest.fixture
def route_context(tmp_path: Path):
    db = DatabaseConnection(str(tmp_path / "routes.sqlite3"))
    db.execute("INSERT INTO novels (id,title,slug) VALUES (?,?,?)", ("novel-1", "Test", "novel-1"))
    db.execute(
        "INSERT INTO chapters (id,novel_id,number,title,content,status) VALUES (?,?,?,?,?,?)",
        ("chapter-1", "novel-1", 1, "第一章", "原文", "draft"),
    )
    db.get_connection().commit()
    try:
        yield db, _Service(db), _Pipeline(), _Knowledge()
    finally:
        db.close()


@pytest.mark.asyncio
async def test_get_chapter_exposes_etag_header_and_body(route_context):
    _db, service, _pipeline, _knowledge = route_context
    response = Response()
    result = await get_chapter("novel-1", response, 1, service)
    assert result.revision == 0
    assert result.etag
    assert response.headers["etag"] == result.etag


@pytest.mark.asyncio
async def test_update_requires_precondition(route_context):
    _db, service, pipeline, knowledge = route_context
    with pytest.raises(HTTPException) as exc_info:
        await update_chapter(
            "novel-1",
            UpdateChapterContentRequest(content="新正文"),
            BackgroundTasks(),
            Response(),
            None,
            1,
            service,
            pipeline,
            knowledge,
        )
    assert exc_info.value.status_code == 428


@pytest.mark.asyncio
async def test_update_returns_conflict_diff_and_persists_failed_receipt_across_workers(route_context, monkeypatch):
    _db, service, pipeline, knowledge = route_context

    # Force the save worker and conflict-receipt worker to use distinct thread
    # local SQLite connections, as can happen in the application's executor.
    worker_threads = []

    async def to_thread_in_fresh_worker(func, /, *args, **kwargs):
        loop = asyncio.get_running_loop()

        def invoke():
            worker_threads.append(threading.current_thread())
            return func(*args, **kwargs)

        with ThreadPoolExecutor(max_workers=1) as executor:
            return await loop.run_in_executor(executor, invoke)

    monkeypatch.setattr(asyncio, "to_thread", to_thread_in_fresh_worker)

    # First writer commits revision 1.
    await update_chapter(
        "novel-1",
        UpdateChapterContentRequest(content="服务器正文", expected_revision=0,
                                    save_request_id="first-writer-request-0001"),
        BackgroundTasks(), Response(), None, 1, service, pipeline, knowledge,
    )
    with pytest.raises(HTTPException) as exc_info:
        await update_chapter(
            "novel-1",
            UpdateChapterContentRequest(content="本地正文", expected_revision=0,
                                        save_request_id="stale-writer-request-0001"),
            BackgroundTasks(), Response(), None, 1, service, pipeline, knowledge,
        )
    assert exc_info.value.status_code == 409
    detail = exc_info.value.detail
    assert detail["code"] == "chapter_revision_conflict"
    assert detail["current"]["revision"] == 1
    assert "+++ local" in detail["diff"]
    assert len(worker_threads) == 3
    assert worker_threads[1] is not worker_threads[2]

    from application.core.services.chapter_save_receipts import ChapterSaveReceipts

    failed = ChapterSaveReceipts(_db).get("stale-writer-request-0001", "novel-1", 1)
    assert failed["status"] == "failed"
    assert failed["error_code"] == "chapter_revision_conflict"
    chapter = service.get_chapter_by_novel_and_number("novel-1", 1)
    assert chapter.content == "服务器正文"
    assert chapter.revision == 1


@pytest.mark.asyncio
async def test_update_sets_etag_and_updated_at_from_commit(route_context):
    _db, service, pipeline, knowledge = route_context
    response = Response()
    result = await update_chapter(
        "novel-1",
        UpdateChapterContentRequest(content="新正文", expected_revision=0),
        BackgroundTasks(), response, None, 1, service, pipeline, knowledge,
    )
    assert result.revision == 1
    assert result.etag == response.headers["etag"]
    assert result.updated_at

