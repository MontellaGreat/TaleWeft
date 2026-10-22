"""Receipt identity is checked inside the real winning SQLite CAS."""
import pytest

from tests.unit.application.engine.test_persistence_commit_ack import writer
from application.core.services.chapter_version_service import ChapterVersionService, RevisionConflictError, content_hash
from application.core.services.chapter_save_receipts import ChapterSaveReceipts


@pytest.fixture
def saved(writer):
    _, db = writer
    db.execute("INSERT INTO novels(id,title,slug) VALUES('book','Book','book')")
    db.execute("INSERT INTO chapters(id,novel_id,number,title,content) VALUES('chapter','book',1,'第一章','原文')")
    _, receipt = ChapterSaveReceipts(db).begin('guarded-save', 'book', 1, {'content': 'new'})
    return db, receipt


@pytest.mark.parametrize('fault', ['novel', 'chapter', 'fingerprint', 'failed', 'missing'])
def test_invalidated_registration_blocks_body_version_and_derivation_on_writer(saved, monkeypatch, fault):
    db, receipt = saved
    versions = ChapterVersionService(db)
    prepare = versions.prepare_save
    def mutate_after_precheck(*args, **kwargs):
        prepared = prepare(*args, **kwargs)
        updates = {
            'novel': "UPDATE chapter_save_receipts SET novel_id='other' WHERE request_id='guarded-save'",
            'chapter': "UPDATE chapter_save_receipts SET chapter_number=2 WHERE request_id='guarded-save'",
            'fingerprint': "UPDATE chapter_save_receipts SET payload_hash='changed' WHERE request_id='guarded-save'",
            'failed': "UPDATE chapter_save_receipts SET status='failed' WHERE request_id='guarded-save'",
            'missing': "DELETE FROM chapter_save_receipts WHERE request_id='guarded-save'",
        }
        db.execute(updates[fault])
        return prepared
    monkeypatch.setattr(versions, 'prepare_save', mutate_after_precheck)
    with pytest.raises(RevisionConflictError):
        versions.save_if_revision('chapter', 0, 'new', save_receipt={
            'request_id': receipt['request_id'], 'payload_hash': receipt['payload_hash']},
            pending_job={'job_id': 'phantom', 'payload': {'content': 'new'}})
    assert db.fetch_one("SELECT content,revision FROM chapters WHERE id='chapter'") == {'content': '原文', 'revision': 0}
    assert db.fetch_one("SELECT COUNT(*) AS n FROM content_versions WHERE chapter_id='chapter'")['n'] == 1
    assert db.fetch_one("SELECT COUNT(*) AS n FROM chapter_derivation_jobs")['n'] == 0


def test_valid_receipt_body_version_and_derivation_commit_together(saved):
    db, registered = saved
    result = ChapterVersionService(db).save_if_revision('chapter', 0, 'new',
        save_receipt={'request_id': registered['request_id'], 'payload_hash': registered['payload_hash']},
        pending_job={'job_id': 'derivation', 'payload': {'content': 'new'}})
    receipt = ChapterSaveReceipts(db).get('guarded-save', 'book', 1)
    assert result['content'] == receipt['chapter']['content'] == 'new'
    assert result['revision'] == receipt['revision'] == 1
    assert receipt['status'] == 'committed' and receipt['content_hash'] == content_hash('new')
    assert db.fetch_one("SELECT revision,chapter_id FROM chapter_derivation_jobs WHERE job_id='derivation'") == {'revision': 1, 'chapter_id': 'chapter'}


def test_corrupted_receipt_cannot_expose_matching_hash_from_another_novel(saved):
    db, _ = saved
    db.execute("INSERT INTO novels(id,title,slug) VALUES('other','Other','other')")
    db.execute("INSERT INTO chapters(id,novel_id,number,title,content) VALUES('other-chapter','other',1,'秘密','同文')")
    db.execute("UPDATE chapter_save_receipts SET status='committed',chapter_id='other-chapter',revision=0,content_hash=? WHERE request_id='guarded-save'", (content_hash('同文'),))
    receipt = ChapterSaveReceipts(db).get('guarded-save', 'book', 1)
    assert receipt['status'] == 'unavailable' and 'chapter' not in receipt
