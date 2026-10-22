"""Production-shaped SQLite WAL recovery and startup fault combinations.

These tests keep the writer connection open with auto-checkpoint disabled so
the recovery service must consume a real, committed WAL rather than a copied
database that already contains all rows.
"""

import hashlib
import json
import sqlite3
from pathlib import Path

import pytest

from application.snapshot.services.database_backup_service import DatabaseBackupError
from application.snapshot.services.database_recovery_service import DatabaseRecoveryService
from infrastructure.persistence.database.recovery_ownership import DatabaseOwnership
from interfaces.runtime import BackendLifecycle


def _wal_database(tmp_path: Path):
    database = tmp_path / "plotpilot.db"
    connection = sqlite3.connect(database)
    connection.execute("PRAGMA journal_mode=WAL")
    connection.execute("PRAGMA wal_autocheckpoint=0")
    connection.execute("CREATE TABLE events(id INTEGER PRIMARY KEY, body TEXT NOT NULL)")
    connection.execute("INSERT INTO events(body) VALUES ('committed before recovery')")
    connection.commit()
    connection.execute("INSERT INTO events(body) VALUES ('committed only in WAL')")
    connection.commit()
    wal = database.with_name(database.name + "-wal")
    assert wal.is_file() and wal.stat().st_size > 32
    return database, connection, wal


def _inventory(folder: Path):
    return {
        item["filename"]: item
        for item in json.loads((folder / "manifest.json").read_text(encoding="utf-8"))["files"]
    }


def test_recover_copy_replays_valid_uncheckpointed_wal_and_records_manifest(tmp_path):
    database, writer, wal = _wal_database(tmp_path)
    owner = DatabaseOwnership(database).acquire()
    try:
        service = DatabaseRecoveryService(database, tmp_path / "recovery")
        before = {
            database.name: hashlib.sha256(database.read_bytes()).hexdigest(),
            wal.name: hashlib.sha256(wal.read_bytes()).hexdigest(),
        }
        receipt = service.recover_copy(owner)
        assert hashlib.sha256(database.read_bytes()).hexdigest() == before[database.name]
        assert hashlib.sha256(wal.read_bytes()).hexdigest() == before[wal.name]
    finally:
        owner.close()
        writer.close()

    assert receipt["status"] == "recovered_verified"
    assert receipt["active_database_changed"] is False
    assert receipt["wal_validation"]["frames"] > 0
    assert receipt["wal_validation"]["last_commit_frame"] > 0
    assert receipt["validation"]["integrity_check"] == "ok"
    with sqlite3.connect(receipt["recovered_database"]) as recovered:
        assert recovered.execute("SELECT body FROM events ORDER BY id").fetchall() == [
            ("committed before recovery",),
            ("committed only in WAL",),
        ]
    files = _inventory(Path(receipt["directory"]))
    assert files[database.name]["sha256"] == before[database.name]
    assert files[wal.name]["sha256"] == before[wal.name]
    assert receipt["manifest_path"] and Path(receipt["manifest_path"]).is_file()


def test_recover_copy_rebuilds_shm_in_working_copy_and_preserves_original_shm(tmp_path):
    database, writer, wal = _wal_database(tmp_path)
    shm = database.with_name(database.name + "-shm")
    assert shm.is_file()
    owner = DatabaseOwnership(database).acquire()
    try:
        service = DatabaseRecoveryService(database, tmp_path / "recovery")
        original_shm = hashlib.sha256(shm.read_bytes()).hexdigest()
        receipt = service.recover_copy(owner)
        assert hashlib.sha256(shm.read_bytes()).hexdigest() == original_shm
    finally:
        owner.close()
        writer.close()

    assert receipt["status"] == "recovered_verified"
    assert any(item["filename"] == shm.name for item in receipt["files"])
    # SQLite may unlink its transient working -shm file when the connection
    # closes; the immutable original remains in the recovery evidence set.
    assert (Path(receipt["directory"]) / "originals" / shm.name).is_file()


def test_corrupt_wal_blocks_recovery_and_keeps_originals_and_failure_receipt(tmp_path):
    database, writer, wal = _wal_database(tmp_path)
    original_db = database.read_bytes()
    original_wal = wal.read_bytes()
    # Keep the file shape intact but invalidate the final frame checksum.
    with wal.open("r+b") as stream:
        stream.seek(-1, 2)
        byte = stream.read(1)
        stream.seek(-1, 2)
        stream.write(bytes([byte[0] ^ 0xFF]))
    corrupted_wal = wal.read_bytes()
    owner = DatabaseOwnership(database).acquire()
    try:
        service = DatabaseRecoveryService(database, tmp_path / "recovery")
        with pytest.raises(DatabaseBackupError, match="原始副本"):
            service.recover_copy(owner)
        assert database.read_bytes() == original_db
        assert wal.read_bytes() == corrupted_wal
    finally:
        owner.close()
        writer.close()

    receipts = list((tmp_path / "recovery").glob("*/recovery-receipt.json"))
    assert len(receipts) == 1
    receipt = json.loads(receipts[0].read_text(encoding="utf-8"))
    assert receipt["status"] == "failed"
    assert receipt["active_database_changed"] is False
    assert original_wal != corrupted_wal


def test_startup_operational_error_recovers_valid_wal_without_starting_daemon(tmp_path, monkeypatch):
    """The production startup gate must expose a verified copy and stay blocked."""
    database, writer, wal = _wal_database(tmp_path)
    from application import paths
    from infrastructure.persistence.database import connection
    from interfaces.api.v1 import system
    from application.snapshot.services.database_backup_service import DatabaseBackupService

    monkeypatch.setattr(paths, "get_db_path", lambda: str(database))

    def fail_database(*_args, **_kwargs):
        raise sqlite3.OperationalError("disk I/O error")

    monkeypatch.setattr(connection, "get_database", fail_database)
    monkeypatch.setattr(connection, "_db_instances_by_path", {})
    monkeypatch.setattr(system, "_get_database_backup_service", lambda: DatabaseBackupService(database))
    owner = DatabaseOwnership(database).acquire()
    started = []
    lifecycle = BackendLifecycle(
        start_daemon=lambda: started.append(True),
        stop_daemon=lambda: None,
    )
    lifecycle._database_ownership = owner
    try:
        with pytest.raises(RuntimeError, match="已验证恢复副本") as error:
            lifecycle.startup(registered_route_count=0)
    finally:
        owner.close()
        writer.close()

    assert started == []
    assert "服务未启动" in str(error.value)
    receipts = list((tmp_path / "recovery").glob("*/recovery-receipt.json"))
    assert len(receipts) == 1
    receipt = json.loads(receipts[0].read_text(encoding="utf-8"))
    assert receipt["status"] == "recovered_verified"
    assert receipt["wal_validation"]["last_commit_frame"] > 0
    with sqlite3.connect(receipt["recovered_database"]) as recovered:
        assert recovered.execute("SELECT body FROM events ORDER BY id").fetchall()[-1] == (
            "committed only in WAL",
        )

