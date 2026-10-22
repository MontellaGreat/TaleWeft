"""Revision/ETag/CAS tests for chapter content persistence."""

from pathlib import Path

import pytest

from infrastructure.persistence.database.connection import DatabaseConnection
from application.core.services.chapter_version_service import (
    ChapterVersionService,
    RevisionConflictError,
    content_hash,
)


@pytest.fixture
def chapter_db(tmp_path: Path):
    db = DatabaseConnection(str(tmp_path / "chapters.sqlite3"))
    db.execute(
        "INSERT INTO novels (id,title,slug) VALUES (?,?,?)",
        ("novel-1", "Test", "novel-1"),
    )
    db.execute(
        "INSERT INTO chapters (id,novel_id,number,title,content,status) VALUES (?,?,?,?,?,?)",
        ("chapter-1", "novel-1", 1, "第一章", "原文", "draft"),
    )
    db.get_connection().commit()
    try:
        yield db
    finally:
        db.close()


def test_migration_creates_immutable_initial_version_and_snapshot(chapter_db):
    service = ChapterVersionService(chapter_db)

    current = service.read("chapter-1")
    assert current["revision"] == 0
    assert current["etag"].startswith('"chapter:chapter-1:0:')
    assert service.list_versions("chapter-1")[0]["revision"] == 0

    saved = service.save_if_revision("chapter-1", 0, "新正文")
    assert saved["revision"] == 1
    assert saved["content"] == "新正文"
    assert saved["content_hash"] == content_hash("新正文")
    assert saved["etag"] == '"chapter:chapter-1:1:%s"' % content_hash("新正文")
    assert [v["revision"] for v in service.list_versions("chapter-1")] == [1, 0]


def test_stale_revision_returns_server_snapshot_and_unified_diff(chapter_db):
    service = ChapterVersionService(chapter_db)
    service.save_if_revision("chapter-1", 0, "服务器正文")

    with pytest.raises(RevisionConflictError) as exc_info:
        service.save_if_revision("chapter-1", 0, "本地正文")

    exc = exc_info.value
    assert exc.current["revision"] == 1
    assert exc.current["content"] == "服务器正文"
    assert "--- server" in exc.diff
    assert "+++ local" in exc.diff
    assert exc.detail()["code"] == "chapter_revision_conflict"


def test_hash_guard_rejects_aba_or_unversioned_stale_writer(chapter_db):
    service = ChapterVersionService(chapter_db)
    current = service.read("chapter-1")
    with pytest.raises(RevisionConflictError):
        service.save_if_revision(
            "chapter-1",
            current["revision"],
            "另一份正文",
            expected_hash="0" * 64,
        )


def test_content_versions_cannot_be_mutated_or_deleted(chapter_db):
    db = chapter_db
    with pytest.raises(Exception, match="immutable"):
        db.execute(
            "UPDATE content_versions SET content=? WHERE chapter_id=? AND revision=0",
            ("篡改", "chapter-1"),
        )
    with pytest.raises(Exception, match="immutable"):
        db.execute(
            "DELETE FROM content_versions WHERE chapter_id=? AND revision=0",
            ("chapter-1",),
        )
