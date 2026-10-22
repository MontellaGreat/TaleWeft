import sqlite3

from infrastructure.persistence.database.migration_runner import apply_migration_files


def test_apply_migration_files_is_idempotent(tmp_path):
    migrations = tmp_path / "migrations"
    migrations.mkdir()
    (migrations / "001_create_sample.sql").write_text(
        "CREATE TABLE sample (id TEXT PRIMARY KEY);\n",
        encoding="utf-8",
    )

    conn = sqlite3.connect(":memory:")
    try:
        apply_migration_files(conn, migrations)
        apply_migration_files(conn, migrations)

        rows = conn.execute("SELECT migration_file FROM migrations_applied").fetchall()
        table = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='sample'"
        ).fetchone()
    finally:
        conn.close()

    assert rows == [("001_create_sample.sql",)]
    assert table == ("sample",)


def test_apply_migration_files_accepts_missing_directory(tmp_path):
    conn = sqlite3.connect(":memory:")
    try:
        apply_migration_files(conn, tmp_path / "missing")
    finally:
        conn.close()


def test_existing_column_does_not_skip_later_statements(tmp_path):
    directory = tmp_path / 'migrations'
    directory.mkdir()
    (directory / '001_columns.sql').write_text(
        'ALTER TABLE sample ADD COLUMN existing TEXT; ALTER TABLE sample ADD COLUMN required TEXT;', encoding='utf-8')
    conn = sqlite3.connect(':memory:')
    conn.execute('CREATE TABLE sample (id TEXT, existing TEXT)')
    apply_migration_files(conn, directory)
    assert 'required' in {row[1] for row in conn.execute('PRAGMA table_info(sample)')}
    conn.close()


def test_failed_migration_rolls_back_and_is_not_marked_applied(tmp_path):
    import pytest
    directory = tmp_path / 'migrations'
    directory.mkdir()
    (directory / '001_fail.sql').write_text(
        'CREATE TABLE partial (id TEXT); INSERT INTO absent VALUES (1);', encoding='utf-8')
    conn = sqlite3.connect(':memory:')
    with pytest.raises(RuntimeError, match='Migration 001_fail.sql failed'):
        apply_migration_files(conn, directory)
    assert not conn.execute("SELECT 1 FROM sqlite_master WHERE name='partial'").fetchone()
    assert not conn.execute('SELECT 1 FROM migrations_applied').fetchone()
    conn.close()
