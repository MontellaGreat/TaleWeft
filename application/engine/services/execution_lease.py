"""SQLite execution ownership shared by DAG and conventional Autopilot."""
import time
import threading

_ready = set()
_schema_lock = threading.Lock()


def _db(database=None):
    if database is not None:
        return database
    from infrastructure.persistence.database.connection import get_database
    return get_database()


def _ensure(db):
    key = (getattr(db, "db_path", None), id(db))
    with _schema_lock:
        if key in _ready:
            return
        db.execute("""CREATE TABLE IF NOT EXISTS novel_execution_leases (
            novel_id TEXT PRIMARY KEY, owner TEXT NOT NULL, job_id TEXT NOT NULL,
            expires_at REAL NOT NULL, updated_at REAL NOT NULL)""")
        db.commit()
        _ready.add(key)


def ensure_execution_schema(db=None):
    _ensure(_db(db))


def claim_execution_lease(novel_id, owner, job_id, ttl_seconds=120, db=None, *, replace_existing_owner=False):
    db = _db(db)
    _ensure(db)
    now = time.time()
    cursor = db.execute("""INSERT INTO novel_execution_leases VALUES (?, ?, ?, ?, ?)
        ON CONFLICT(novel_id) DO UPDATE SET owner=excluded.owner, job_id=excluded.job_id,
            expires_at=excluded.expires_at, updated_at=excluded.updated_at
        WHERE novel_execution_leases.expires_at <= ?
            OR (novel_execution_leases.owner=excluded.owner AND
                (novel_execution_leases.job_id=excluded.job_id OR ?))""",
        (novel_id, owner, job_id, now + max(1, ttl_seconds), now, now, int(replace_existing_owner)))
    db.commit()
    return cursor.rowcount == 1


def refresh_execution_lease(novel_id, owner, job_id, ttl_seconds=120, db=None):
    db = _db(db)
    _ensure(db)
    now = time.time()
    cursor = db.execute("""UPDATE novel_execution_leases SET expires_at=?, updated_at=?
        WHERE novel_id=? AND owner=? AND job_id=? AND expires_at>?""",
        (now + max(1, ttl_seconds), now, novel_id, owner, job_id, now))
    db.commit()
    return cursor.rowcount == 1


def release_execution_lease(novel_id, owner, job_id, db=None):
    db = _db(db)
    _ensure(db)
    cursor = db.execute("DELETE FROM novel_execution_leases WHERE novel_id=? AND owner=? AND job_id=?",
                        (novel_id, owner, job_id))
    db.commit()
    return cursor.rowcount == 1


def get_execution_lease(novel_id, db=None):
    db = _db(db)
    try:
        row = db.fetch_one("SELECT * FROM novel_execution_leases WHERE novel_id=? AND expires_at>?",
                           (novel_id, time.time()))
    except Exception as exc:
        if "no such table" in str(exc).lower():
            return None
        raise
    return dict(row) if row else None
