"""SQLite SQL migration runner."""
from __future__ import annotations

import logging
import sqlite3
import time
from pathlib import Path

from infrastructure.persistence.database.sqlite_retry import (
    get_sqlite_retry_settings,
    is_sqlite_lock_error,
    migration_retry_delay,
)

logger = logging.getLogger(__name__)


def _ordered_migrations(directory: Path):
    paths = sorted(directory.glob('*.sql'))
    # Legacy filenames are not all numbered; this ALTER depends on the CREATE.
    dependency = 'add_macro_diagnosis_results.sql'
    dependent = 'add_macro_diagnosis_context_patch.sql'
    names = {path.name: path for path in paths}
    if dependency in names and dependent in names:
        paths.remove(names[dependent])
        paths.insert(paths.index(names[dependency]) + 1, names[dependent])
    return paths


def _apply_statements(conn, text):
    """Keep each file atomic and continue past individual idempotent DDL.

    executescript stops at the first existing column, which used to mark the
    entire file applied even when later statements had never run.
    """
    conn.execute('SAVEPOINT migration_file')
    try:
        statement = ''
        for character in text:
            statement += character
            if character == ';' and sqlite3.complete_statement(statement):
                try:
                    conn.execute(statement)
                except sqlite3.OperationalError as exc:
                    if 'already exists' not in str(exc) and 'duplicate column' not in str(exc):
                        raise
                statement = ''
        if statement.strip():
            conn.execute(statement)
        conn.execute('RELEASE SAVEPOINT migration_file')
    except BaseException:
        conn.execute('ROLLBACK TO SAVEPOINT migration_file')
        conn.execute('RELEASE SAVEPOINT migration_file')
        raise


def apply_migration_files(conn: sqlite3.Connection, migrations_dir: Path) -> None:
    """Apply SQL migrations idempotently using the existing tracking table."""
    retry_settings = get_sqlite_retry_settings()
    max_retries = retry_settings.migration_max_retries
    for attempt in range(max_retries):
        try:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS migrations_applied (
                    migration_file TEXT PRIMARY KEY,
                    applied_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
                """
            )
            conn.commit()
            break
        except sqlite3.OperationalError as exc:
            if is_sqlite_lock_error(exc) and attempt < max_retries - 1:
                logger.warning(
                    "Database locked, retrying... (attempt %s/%s)",
                    attempt + 1,
                    max_retries,
                )
                time.sleep(migration_retry_delay(attempt, retry_settings))
                continue
            if "already exists" in str(exc):
                break
            logger.warning(
                "Cannot create migrations_applied table: %s, using legacy mode",
                exc,
            )
            apply_migration_files_legacy(conn, migrations_dir)
            return

    applied = set()
    try:
        cursor = conn.execute("SELECT migration_file FROM migrations_applied")
        applied = {row[0] for row in cursor.fetchall()}
    except Exception:
        pass

    if not migrations_dir.is_dir():
        logger.warning(
            "未找到迁移目录（将仅依赖 schema.sql 与代码内补丁）: %s",
            migrations_dir,
        )
        return

    new_migrations = 0
    for migration_path in _ordered_migrations(migrations_dir):
        migration_file = migration_path.name
        if migration_file in applied:
            continue

        try:
            migration_sql = migration_path.read_text(encoding="utf-8")
            _apply_statements(conn, migration_sql)
            conn.execute(
                "INSERT OR IGNORE INTO migrations_applied (migration_file) VALUES (?)",
                (migration_file,),
            )
            conn.commit()
            logger.info("Applied migration: %s", migration_file)
            new_migrations += 1
        except sqlite3.OperationalError as exc:
            err = str(exc)
            raise RuntimeError(f'Migration {migration_file} failed; this migration file rolled back and startup blocked: {err}') from exc
        except Exception as exc:
            raise RuntimeError(f'Migration {migration_file} failed; this migration file rolled back and startup blocked') from exc

    if new_migrations == 0 and applied:
        logger.debug("All %d migrations already applied, skipped", len(applied))


def apply_migration_files_legacy(
    conn: sqlite3.Connection, migrations_dir: Path
) -> None:
    """Legacy migration mode without a tracking table."""
    if not migrations_dir.is_dir():
        logger.warning(
            "未找到迁移目录（将仅依赖 schema.sql 与代码内补丁）: %s",
            migrations_dir,
        )
        return

    for migration_path in _ordered_migrations(migrations_dir):
        migration_file = migration_path.name
        try:
            migration_sql = migration_path.read_text(encoding="utf-8")
            _apply_statements(conn, migration_sql)
            conn.commit()
            logger.info("Applied migration: %s", migration_file)
        except sqlite3.OperationalError as exc:
            if "already exists" in str(exc) or "duplicate column" in str(exc):
                logger.debug("Migration %s already applied: %s", migration_file, exc)
            else:
                logger.warning("Migration %s failed: %s", migration_file, exc)
        except OSError as exc:
            logger.warning("Failed to read migration %s: %s", migration_file, exc)
        except Exception as exc:
            logger.warning("Failed to apply migration %s: %s", migration_file, exc)


def _mark_applied(conn: sqlite3.Connection, migration_file: str) -> None:
    try:
        conn.execute(
            "INSERT OR IGNORE INTO migrations_applied (migration_file) VALUES (?)",
            (migration_file,),
        )
        conn.commit()
    except Exception:
        pass
