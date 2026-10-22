import json
import hashlib
import sqlite3
from pathlib import Path

from fastapi import FastAPI
from fastapi.testclient import TestClient
import pytest

from tests.unit.engine.test_daemon_rewrite_candidate_integrity import daemon_writer, RAW, CLEAN
from application.snapshot.services.database_backup_service import DatabaseBackupService, DatabaseBackupError
from interfaces.api.v1 import system
from infrastructure.persistence.database.recovery_ownership import DatabaseOwnership


def test_online_backup_recovers_latest_wal_commit_and_keeps_active_database(daemon_writer, tmp_path):
    db = daemon_writer[0]
    db.execute("UPDATE chapters SET content=? WHERE id='chapter'", (CLEAN,))
    assert Path(db.db_path + '-wal').is_file()
    service = DatabaseBackupService(db.db_path, tmp_path / 'backups')
    backup = service.create()
    assert backup['status'] == 'verified'
    assert backup['validation']['integrity_check'] == backup['validation']['foreign_key_check'] == 'ok'
    db.execute("UPDATE chapters SET content='备份后的作者编辑' WHERE id='chapter'")
    recovered = service.restore_copy(backup['backup_id'])
    assert recovered['active_database_changed'] is False and recovered['status'] == 'restored_verified'
    with sqlite3.connect(recovered['restored_database']) as conn:
        assert conn.execute("SELECT content FROM chapters WHERE id='chapter'").fetchone()[0] == CLEAN
        assert conn.execute('PRAGMA integrity_check').fetchone()[0] == 'ok'
        assert not conn.execute('PRAGMA foreign_key_check').fetchall()
    assert db.fetch_one("SELECT content FROM chapters WHERE id='chapter'")['content'] == '备份后的作者编辑'
    assert Path(recovered['restored_database']).with_suffix('.restore.json').is_file()


def test_corrupt_backup_cannot_verify_or_restore(daemon_writer, tmp_path):
    service = DatabaseBackupService(daemon_writer[0].db_path, tmp_path / 'backups')
    backup = service.create()
    database = Path(backup['directory']) / 'plotpilot.db'
    with database.open('r+b') as stream:
        stream.seek(-1, 2); original = stream.read(1); stream.seek(-1, 2); stream.write(bytes([original[0] ^ 255]))
    for action in (service.verify, service.restore_copy):
        with pytest.raises(DatabaseBackupError, match='SHA-256'):
            action(backup['backup_id'])
    assert daemon_writer[0].fetch_one("SELECT content FROM chapters WHERE id='chapter'")['content'] == RAW


def test_invalid_manifest_and_path_traversal_fail_closed(daemon_writer, tmp_path):
    service = DatabaseBackupService(daemon_writer[0].db_path, tmp_path / 'backups')
    backup = service.create()
    with pytest.raises(DatabaseBackupError, match='编号无效'):
        service.verify('../plotpilot.db')
    manifest = Path(backup['directory']) / 'manifest.json'
    parsed = json.loads(manifest.read_text(encoding='utf-8'))
    parsed['database']['filename'] = '../plotpilot.db'
    manifest.write_text(json.dumps(parsed), encoding='utf-8')
    with pytest.raises(DatabaseBackupError):
        service.restore_copy(backup['backup_id'])


def test_foreign_key_invalid_source_is_not_published_as_valid_backup(tmp_path):
    source = tmp_path / 'invalid.db'
    with sqlite3.connect(source) as conn:
        conn.executescript('CREATE TABLE parent(id INTEGER PRIMARY KEY); CREATE TABLE child(id INTEGER PRIMARY KEY,parent_id INTEGER REFERENCES parent(id)); INSERT INTO child VALUES (1,999);')
    service = DatabaseBackupService(source, tmp_path / 'backups')
    with pytest.raises(DatabaseBackupError, match='外键'):
        service.create()
    assert service.list() == []


def test_backup_api_requires_localhost_and_returns_verified_restoration(daemon_writer, tmp_path, monkeypatch):
    service = DatabaseBackupService(daemon_writer[0].db_path, tmp_path / 'backups')
    monkeypatch.setattr(system, '_get_database_backup_service', lambda: service)
    app = FastAPI(); app.include_router(system.router, prefix='/api/v1')
    with TestClient(app, client=('127.0.0.1', 50000)) as client:
        backup = client.post('/api/v1/system/database-backups').json()
        assert backup['status'] == 'verified'
        assert client.post(f"/api/v1/system/database-backups/{backup['backup_id']}/verify").json()['status'] == 'verified'
        assert client.post(f"/api/v1/system/database-backups/{backup['backup_id']}/restore-copy").json()['active_database_changed'] is False
    with TestClient(app, client=('198.51.100.1', 50000)) as client:
        assert client.post('/api/v1/system/database-backups').status_code == 403


@pytest.mark.parametrize('value', [[], {'format': 'plotpilot-sqlite-online-backup', 'format_version': 1, 'database': []}])
def test_malformed_manifest_is_reported_and_excluded_from_list(daemon_writer, tmp_path, value):
    service = DatabaseBackupService(daemon_writer[0].db_path, tmp_path / 'backups')
    backup = service.create()
    (Path(backup['directory']) / 'manifest.json').write_text(json.dumps(value), encoding='utf-8')
    with pytest.raises(DatabaseBackupError, match='清单'): service.verify(backup['backup_id'])
    assert service.list() == []


def test_database_corruption_is_checked_even_if_manifest_hash_matches(daemon_writer, tmp_path):
    service = DatabaseBackupService(daemon_writer[0].db_path, tmp_path / 'backups')
    backup = service.create()
    folder = Path(backup['directory'])
    data = b'not a sqlite database'
    (folder / 'plotpilot.db').write_bytes(data)
    manifest = json.loads((folder / 'manifest.json').read_text(encoding='utf-8'))
    manifest['database'].update(size=len(data), sha256=hashlib.sha256(data).hexdigest())
    (folder / 'manifest.json').write_text(json.dumps(manifest), encoding='utf-8')
    with pytest.raises(DatabaseBackupError, match='完整性'): service.restore_copy(backup['backup_id'])
    assert daemon_writer[0].fetch_one("SELECT content FROM chapters WHERE id='chapter'")['content'] == RAW


def test_controlled_restore_switches_database_and_local_vectors_only_after_restart(tmp_path):
    source = tmp_path / 'plotpilot.db'
    vectors = tmp_path / 'chromadb'
    vectors.mkdir()
    (vectors / 'index.json').write_text('{"v": 1}', encoding='utf-8')
    conn = sqlite3.connect(source)
    try:
        conn.execute('PRAGMA journal_mode=WAL')
        conn.execute('CREATE TABLE notes(id INTEGER PRIMARY KEY, body TEXT)')
        conn.execute("INSERT INTO notes(body) VALUES ('before backup')")
        conn.commit()
    finally:
        conn.close()
    service = DatabaseBackupService(source, tmp_path / 'backups', vector_path=vectors)
    backup = service.create()
    conn = sqlite3.connect(source)
    try:
        conn.execute("UPDATE notes SET body='after backup'")
        conn.commit()
    finally:
        conn.close()
    (vectors / 'index.json').write_text('{"v": 2}', encoding='utf-8')
    owner = DatabaseOwnership(source).acquire()
    try:
        request = service.request_restore(backup['backup_id'], expected_sha256=backup['database']['sha256'])
        assert request['status'] == 'pending_restart' and request['active_database_changed'] is False
        status = service.restore_status()
        assert status['request_id'] == request['request_id']
        receipt = service.activate_pending(owner, quiescent=True)
    finally:
        owner.close()
    assert receipt['status'] == 'activated_verified' and receipt['active_database_changed'] is True
    with sqlite3.connect(source) as conn:
        assert conn.execute('SELECT body FROM notes').fetchone()[0] == 'before backup'
    assert (vectors / 'index.json').read_text(encoding='utf-8') == '{"v": 1}'
    assert service.restore_status()['status'] == 'activated_verified'
