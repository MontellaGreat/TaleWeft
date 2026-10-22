import sqlite3

import pytest

from interfaces.runtime import BackendLifecycle


def test_startup_io_error_preserves_original_database_wal_shm_and_blocks_daemon(tmp_path, monkeypatch):
    from application import paths
    from infrastructure.persistence.database import connection
    db_path = tmp_path / "story.db"
    files = [db_path, tmp_path / "story.db-wal", tmp_path / "story.db-shm"]
    for p in files:
        p.write_bytes(b"original:" + p.name.encode())
    before = {p: p.read_bytes() for p in files}
    monkeypatch.setattr(paths, "get_db_path", lambda: str(db_path))

    def fail(*_):
        raise sqlite3.OperationalError("disk I/O error")

    monkeypatch.setattr(connection, "get_database", fail)
    calls = []
    lifecycle = BackendLifecycle(start_daemon=lambda: calls.append("daemon"), stop_daemon=lambda: None)
    with pytest.raises(RuntimeError, match="DB/WAL/SHM"):
        lifecycle.stop_all_running_novels()
    assert {p: p.read_bytes() for p in files} == before
    assert calls == []
