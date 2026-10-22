import sqlite3

from scripts.migrate_checkpoints_to_snapshots import CheckpointMigrator


def test_backup_contains_latest_committed_wal_rows_without_checkpoint(tmp_path):
    path = tmp_path / "source.db"
    writer = sqlite3.connect(path)
    try:
        writer.execute("PRAGMA journal_mode=WAL")
        writer.execute("PRAGMA wal_autocheckpoint=0")
        writer.execute("CREATE TABLE checkpoints(id TEXT PRIMARY KEY, body TEXT)")
        writer.execute("INSERT INTO checkpoints VALUES ('latest', 'committed in WAL')")
        writer.commit()
        assert (tmp_path / "source.db-wal").stat().st_size > 0
        backup = CheckpointMigrator(str(path)).backup_checkpoints_table()
        with sqlite3.connect(backup) as restored:
            assert restored.execute("SELECT body FROM checkpoints WHERE id='latest'").fetchone()[0] == "committed in WAL"
            assert restored.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    finally:
        writer.close()
