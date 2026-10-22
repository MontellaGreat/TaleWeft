"""Carry a chapter version through async work and enforce it at SQLite commit."""
from contextlib import contextmanager
from contextvars import ContextVar
import hashlib
import os

_active_fence = ContextVar("plotpilot_chapter_write_fence", default=None)


class ChapterWriteSuperseded(RuntimeError):
    pass


def current_chapter_write_fence():
    fence = _active_fence.get()
    return dict(fence) if fence else None


def fence_for_database(db_path):
    fence = current_chapter_write_fence()
    if fence and os.path.normcase(os.path.abspath(str(db_path))) == fence["db_path"]:
        return fence
    return None


@contextmanager
def chapter_write_fence(db_path=None, chapter_id=None, revision=None, expected_hash=None, *, fence=None, dependencies=None):
    if fence is None and db_path is not None:
        fence = {"db_path": os.path.normcase(os.path.abspath(str(db_path))),
                 "chapter_id": str(chapter_id), "revision": int(revision), "content_hash": expected_hash}
        if dependencies:
            fence['dependencies'] = dependencies
    token = _active_fence.set(fence)
    try:
        yield
    finally:
        _active_fence.reset(token)


def payload_with_chapter_fence(payload):
    fence = current_chapter_write_fence()
    return dict(payload, _chapter_fence=fence) if fence else payload


def require_chapter_write_fence(connection, fence):
    """BEGIN IMMEDIATE prevents body changes between validation and the write."""
    if not fence:
        return
    if not connection.in_transaction:
        connection.execute("BEGIN IMMEDIATE")
    row = connection.execute("SELECT revision,content FROM chapters WHERE id=?", (fence["chapter_id"],)).fetchone()
    digest = hashlib.sha256(str(row["content"] or "").encode("utf-8")).hexdigest() if row else None
    if not row or row["revision"] != fence["revision"] or digest != fence["content_hash"]:
        connection.rollback()
        raise ChapterWriteSuperseded(f"chapter_write_superseded: {fence['chapter_id']} expected revision {fence['revision']}")
    for dependency in fence.get('dependencies', []):
        row = connection.execute('SELECT revision,content FROM chapters WHERE id=?', (dependency['id'],)).fetchone()
        digest = hashlib.sha256(str(row['content'] or '').encode('utf-8')).hexdigest() if row else None
        if not row or row['revision'] != dependency['revision'] or digest != dependency['content_hash']:
            connection.rollback()
            raise ChapterWriteSuperseded(f"chapter_write_superseded: upstream chapter {dependency['id']} changed")


class FencedCursor:
    def __init__(self, connection):
        self.connection = connection
        self.result = None

    def execute(self, sql, parameters=()):
        self.result = self.connection.execute(sql, parameters)
        return self

    def executemany(self, sql, parameters):
        self.result = self.connection.executemany(sql, parameters)
        return self

    def __getattr__(self, name):
        if self.result is None:
            raise AttributeError(name)
        return getattr(self.result, name)


class FencedConnection:
    """Legacy get_connection()/cursor() writes also use the writer fence."""
    def __init__(self, database, raw):
        self.database = database
        self.raw = raw

    def execute(self, sql, parameters=()):
        words = str(sql).strip().upper().split(maxsplit=1)
        if words and words[0].rstrip(';') in ('COMMIT', 'END'):
            require_chapter_write_fence(self.raw, fence_for_database(self.database.db_path))
        return self.database.execute(sql, parameters)

    def executemany(self, sql, parameters):
        from infrastructure.persistence.database.write_dispatch import allow_direct_sqlite_writes, is_sqlite_writer_thread
        if allow_direct_sqlite_writes() or is_sqlite_writer_thread():
            require_chapter_write_fence(self.raw, fence_for_database(self.database.db_path))
            return self.raw.executemany(sql, parameters)
        return self.database.execute_many(sql, list(parameters))

    def cursor(self, *args, **kwargs):
        return FencedCursor(self)

    def commit(self):
        self.database.commit()

    def rollback(self):
        self.raw.rollback()

    def executescript(self, _script):
        raise RuntimeError("derivation fence requires individual SQL statements or a transaction")

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, traceback):
        if exc_type is None:
            self.commit()
        else:
            self.rollback()
        return False

    def __getattr__(self, name):
        return getattr(self.raw, name)
