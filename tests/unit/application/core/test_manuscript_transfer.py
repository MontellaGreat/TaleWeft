# Modified for TaleWeft on 2026-10-22: product branding; compatibility identifiers retained.
import json
from concurrent.futures import ThreadPoolExecutor

from fastapi import FastAPI
from fastapi.testclient import TestClient
import pytest

from tests.unit.engine.test_daemon_rewrite_candidate_integrity import daemon_writer, RAW
from application.core.services.export_service import ExportService
from application.core.services.manuscript_transfer import ManuscriptTransfer, ManuscriptValidationError, digest, text_hash, validate_document
from infrastructure.persistence.database.sqlite_novel_repository import SqliteNovelRepository
from infrastructure.persistence.database.sqlite_chapter_repository import SqliteChapterRepository
from interfaces.api.v1.core import export


@pytest.fixture
def transfer(daemon_writer):
    db = daemon_writer[0]
    return ManuscriptTransfer(db), ExportService(SqliteNovelRepository(db), SqliteChapterRepository(db))


def document(transfer):
    content, mime, filename = transfer[1].export_novel('novel', 'json')
    assert 'json' in mime and filename.endswith('.taleweft.json')
    return json.loads(content)


def seal(item):
    for chapter in item['chapters']:
        chapter['content_sha256'] = text_hash(chapter['content'])
    item['payload_sha256'] = digest({key: value for key, value in item.items() if key != 'payload_sha256'})
    return json.dumps(item, ensure_ascii=False)


def test_export_preview_confirm_roundtrip_preserves_body_and_original_book(transfer):
    service = transfer[0]
    item = document(transfer)
    # Unicode, whitespace, CRLF and author's layout are never normalized.
    item['chapters'][0]['content'] = '  雨停了。\r\n\n🙂作者新段\t  '
    raw = seal(item)
    preview = service.preview(raw)
    assert preview['validation']['status'] == 'valid' and preview['validation']['chapter_count'] == 1
    assert service.db.fetch_one('SELECT COUNT(*) count FROM novels')['count'] == 1
    receipt = service.confirm(preview['preview_id'], preview['payload_sha256'])
    assert receipt['status'] == 'imported' and receipt['original_novels_changed'] is False
    assert service.db.fetch_one("SELECT content FROM chapters WHERE novel_id=?", (receipt['novel_id'],))['content'] == item['chapters'][0]['content']
    assert service.db.fetch_one("SELECT content FROM chapters WHERE novel_id='novel'")['content'] == RAW
    assert service.db.fetch_one('SELECT autopilot_status,current_stage FROM novels WHERE id=?', (receipt['novel_id'],)) == {'autopilot_status': 'stopped', 'current_stage': 'planning'}
    reexport = transfer[1].export_novel(receipt['novel_id'], 'json')[0].decode('utf-8')
    assert validate_document(reexport)[0] == item
    assert service.get(preview['preview_id'])['receipt'] == receipt


def test_two_concurrent_confirmations_create_exactly_one_new_book(transfer):
    service = transfer[0]
    preview = service.preview(seal(document(transfer)))
    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(lambda _: service.confirm(preview['preview_id'], preview['payload_sha256']), range(2)))
    assert results[0] == results[1]
    assert service.db.fetch_one('SELECT COUNT(*) count FROM novels')['count'] == 2
    assert service.db.fetch_one('SELECT COUNT(*) count FROM chapters')['count'] == 2


@pytest.mark.parametrize('mutation', ['duplicate_number', 'wrong_content_hash', 'wrong_payload_hash', 'unknown_field', 'bad_status', 'boolean_number', 'target_too_small'])
def test_invalid_manuscripts_report_errors_and_do_not_create_previews(transfer, mutation):
    service = transfer[0]
    item = document(transfer)
    if mutation == 'duplicate_number': item['chapters'].append(dict(item['chapters'][0]))
    if mutation == 'wrong_content_hash': item['chapters'][0]['content_sha256'] = '0' * 64
    if mutation == 'wrong_payload_hash': item['payload_sha256'] = '0' * 64
    if mutation == 'unknown_field': item['novel']['endpoint_key'] = 'not-a-manuscript-field'
    if mutation == 'bad_status': item['chapters'][0]['status'] = []
    if mutation == 'boolean_number': item['chapters'][0]['number'] = True
    if mutation == 'target_too_small': item['novel']['target_chapters'] = 0
    with pytest.raises(ManuscriptValidationError) as caught:
        service.preview(json.dumps(item))
    assert caught.value.errors
    assert service.db.fetch_one('SELECT COUNT(*) count FROM manuscript_imports')['count'] == 0
    assert service.db.fetch_one('SELECT COUNT(*) count FROM novels')['count'] == 1


def test_json_duplicate_keys_and_invalid_numbers_are_rejected():
    for raw in ['{"format":1,"format":2}', '{"value":NaN}', '[[[invalid']:
        with pytest.raises(ManuscriptValidationError): validate_document(raw)


def test_wrong_confirmation_hash_cannot_create_a_book(transfer):
    service = transfer[0]
    preview = service.preview(seal(document(transfer)))
    with pytest.raises(ValueError, match='预览不同'):
        service.confirm(preview['preview_id'], '0'*64)
    assert service.db.fetch_one('SELECT COUNT(*) count FROM novels')['count'] == 1
    assert service.get(preview['preview_id'])['status'] == 'pending'
    with pytest.raises(Exception, match='immutable'):
        service.db.execute('UPDATE manuscript_imports SET document_json=? WHERE preview_id=?', ('{}', preview['preview_id']))


def test_partial_import_failure_rolls_back_book_chapters_and_receipt(transfer):
    service = transfer[0]
    item = document(transfer)
    item['chapters'].append({**item['chapters'][0], 'number': 2})
    item['novel']['target_chapters'] = 2
    preview = service.preview(seal(item))
    service.db.execute("CREATE TRIGGER reject_import_second BEFORE INSERT ON chapters WHEN NEW.number=2 AND NEW.version_source='manuscript_import' BEGIN SELECT RAISE(ABORT,'injected failure'); END")
    with pytest.raises(ValueError, match='injected failure'):
        service.confirm(preview['preview_id'], preview['payload_sha256'])
    assert service.db.fetch_one('SELECT COUNT(*) count FROM novels')['count'] == 1
    assert service.db.fetch_one('SELECT COUNT(*) count FROM chapters')['count'] == 1
    assert service.get(preview['preview_id'])['receipt'] is None


def test_real_api_preserves_export_errors_and_requires_displayed_import_hash(transfer):
    app = FastAPI(); app.include_router(export.router, prefix='/api/v1')
    app.dependency_overrides[export.get_export_service] = lambda: transfer[1]
    app.dependency_overrides[export.get_manuscript_transfer] = lambda: transfer[0]
    with TestClient(app) as client:
        assert client.get('/api/v1/export/novel/novel?format=invalid').status_code == 400
        assert client.get('/api/v1/export/chapter/missing?format=invalid').status_code == 400
        assert client.get('/api/v1/export/novel/missing?format=json').status_code == 404
        exported = client.get('/api/v1/export/novel/novel?format=json')
        assert exported.status_code == 200 and 'filename*=' in exported.headers['content-disposition']
        preview = client.post('/api/v1/export/manuscript/preview', json={'document_json': exported.text}).json()
        endpoint = f"/api/v1/export/manuscript/previews/{preview['preview_id']}"
        assert client.post(endpoint+'/confirm', json={}).status_code == 422
        assert client.get(endpoint).json()['status'] == 'pending'
        assert client.post(endpoint+'/confirm', json={'expected_payload_sha256': preview['payload_sha256']}).json()['status'] == 'imported'
