"""AOF files are evidence, not disposable until the exact draft is committed."""
import pytest

from application.engine.services import draft_aof
from application.engine.services.persistence_queue import PersistenceCommitTimeout
from domain.novel.entities.chapter import Chapter, ChapterStatus
from domain.novel.value_objects.novel_id import NovelId
from infrastructure.persistence.database.sqlite_chapter_repository import SqliteChapterRepository

from tests.unit.application.engine.test_persistence_commit_ack import writer


@pytest.fixture
def drafts(writer, tmp_path, monkeypatch):
    queue, db = writer
    directory = tmp_path / "drafts"
    directory.mkdir()
    monkeypatch.setattr(draft_aof, "_draft_dir", lambda: directory)
    monkeypatch.setattr("application.paths.get_db_path", lambda: db.db_path)
    db.execute("INSERT INTO novels(id, slug, title) VALUES ('novel', 'novel', 'test')")
    return queue, db, directory


def _chapter(db, content, status=ChapterStatus.DRAFT):
    SqliteChapterRepository(db).save(Chapter(
        id="chapter", novel_id=NovelId("novel"), number=1,
        title="test", content=content, status=status,
    ))


@pytest.mark.parametrize("database_text,draft_text", [
    ("AAAA", "BBBB"), ("old text", "different and longer draft"),
    ("longer existing version", "shorter"),
])
def test_conflicting_text_is_preserved_regardless_of_length(drafts, database_text, draft_text):
    queue, db, directory = drafts
    _chapter(db, database_text)
    path = directory / "novel_ch1.draft"
    path.write_text(draft_text, encoding="utf-8")
    assert draft_aof.recover_all_drafts() == 0
    assert path.read_text(encoding="utf-8") == draft_text
    assert db.fetch_one("SELECT content FROM chapters")["content"] == database_text


def test_prefix_extension_commits_before_file_is_deleted(drafts):
    _queue, db, directory = drafts
    _chapter(db, "start")
    path = directory / "novel_ch1.draft"
    path.write_text("start plus recovery", encoding="utf-8")
    assert draft_aof.recover_all_drafts() == 1
    assert not path.exists()
    assert db.fetch_one("SELECT content FROM chapters")["content"] == "start plus recovery"
    assert db.fetch_one("SELECT current_beat_index FROM novels")["current_beat_index"] == 1


def test_completed_chapter_is_never_replaced_by_aof(drafts):
    _queue, db, directory = drafts
    _chapter(db, "start", ChapterStatus.COMPLETED)
    path = directory / "novel_ch1.draft"
    path.write_text("start but stale draft", encoding="utf-8")
    assert draft_aof.recover_all_drafts() == 0
    assert path.exists()
    assert db.fetch_one("SELECT status, content FROM chapters") == {
        "status": "completed", "content": "start"}


def test_completed_chapter_with_only_prefix_match_keeps_draft(drafts):
    _queue, db, directory = drafts
    _chapter(db, "start final", ChapterStatus.COMPLETED)
    path = directory / "novel_ch1.draft"
    path.write_text("start", encoding="utf-8")
    assert draft_aof.recover_all_drafts() == 0
    assert path.exists()
    assert db.fetch_one("SELECT current_beat_index FROM novels")["current_beat_index"] == 0


def test_completed_chapter_with_exact_match_cleans_draft(drafts):
    _queue, db, directory = drafts
    _chapter(db, "start final", ChapterStatus.COMPLETED)
    path = directory / "novel_ch1.draft"
    path.write_text("start final", encoding="utf-8")
    assert draft_aof.recover_all_drafts() == 1
    assert not path.exists()


def test_commit_timeout_preserves_draft(drafts, monkeypatch):
    queue, db, directory = drafts
    _chapter(db, "start")
    path = directory / "novel_ch1.draft"
    path.write_text("start draft", encoding="utf-8")

    def pending(*_args, **_kwargs):
        raise PersistenceCommitTimeout("still pending")

    monkeypatch.setattr(queue, "push_and_wait", pending)
    assert draft_aof.recover_all_drafts() == 0
    assert path.exists()


def test_foreign_key_failure_preserves_orphan_draft(drafts):
    _queue, _db, directory = drafts
    path = directory / "missing_ch1.draft"
    path.write_text("orphan data", encoding="utf-8")
    assert draft_aof.recover_all_drafts() == 0
    assert path.exists()


def test_changed_file_is_retained_after_recovery(drafts, monkeypatch):
    _queue, db, directory = drafts
    _chapter(db, "start")
    path = directory / "novel_ch1.draft"
    path.write_text("start snapshot", encoding="utf-8")
    monkeypatch.setattr(draft_aof, "_mark_recovered_chapter_state",
                        lambda _novel: draft_aof.append_chunk("novel", 1, " new data"))
    assert draft_aof.recover_all_drafts() == 0
    assert path.read_text(encoding="utf-8") == "start snapshot new data"
    assert db.fetch_one("SELECT content FROM chapters")["content"] == "start snapshot"


def test_delete_requires_committed_content(drafts):
    _queue, db, directory = drafts
    _chapter(db, "start")
    path = directory / "novel_ch1.draft"
    path.write_text("start draft", encoding="utf-8")
    draft_aof.delete_draft("novel", 1)
    assert path.exists()
    _chapter(db, "start draft")
    draft_aof.delete_draft("novel", 1)
    assert not path.exists()


def test_recovery_does_not_overwrite_concurrent_editor_change(drafts, monkeypatch):
    _queue, db, directory = drafts
    _chapter(db, "start")
    path = directory / "novel_ch1.draft"
    path.write_text("start draft", encoding="utf-8")
    execute = db.execute

    def concurrent_edit(sql, params=()):
        if "COALESCE(content, '') = ?" in sql:
            execute("UPDATE chapters SET content = 'editor version' WHERE id = 'chapter'")
        return execute(sql, params)

    monkeypatch.setattr(db, "execute", concurrent_edit)
    assert draft_aof.recover_all_drafts() == 0
    assert path.exists()
    assert db.fetch_one("SELECT content FROM chapters")["content"] == "editor version"


def test_recovery_does_not_overwrite_concurrent_new_chapter(drafts, monkeypatch):
    _queue, db, directory = drafts
    path = directory / "novel_ch1.draft"
    path.write_text("older draft", encoding="utf-8")
    execute = db.execute

    def concurrent_insert(sql, params=()):
        if "ON CONFLICT(novel_id, number) DO NOTHING" in sql:
            execute("""INSERT INTO chapters(id, novel_id, number, title, content)
                       VALUES ('editor', 'novel', 1, 'edited', 'editor version')""")
        return execute(sql, params)

    monkeypatch.setattr(db, "execute", concurrent_insert)
    assert draft_aof.recover_all_drafts() == 0
    assert path.exists()
    assert db.fetch_one("SELECT content FROM chapters")["content"] == "editor version"
