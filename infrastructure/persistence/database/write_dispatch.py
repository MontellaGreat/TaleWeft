"""单写者内核：标记持久化队列消费线程，并对「非 writer 线程」提供入队封装。

SQLite 单机多写竞争者全部经 mp.Queue → 单一消费者线程顺序提交，从源头消除 database is locked。"""
from __future__ import annotations

import contextlib
import logging
import re
import threading
from typing import Any, Dict, List, Optional, Tuple, Union

from infrastructure.persistence.database.write_environment import (
    SQLiteWriteEnvironmentSettings,
)

logger = logging.getLogger(__name__)

_sqlite_writer_thread_ident: Optional[int] = None

# interfaces.main startup：在持久化消费者线程就绪之前允许直连 SQLite（见 startup_sqlite_writes_bypass_queue）
_startup_sqlite_bootstrap_scope = threading.local()


def register_sqlite_writer_thread() -> None:
    global _sqlite_writer_thread_ident
    _sqlite_writer_thread_ident = threading.get_ident()


def clear_sqlite_writer_thread() -> None:
    global _sqlite_writer_thread_ident
    _sqlite_writer_thread_ident = None


def is_sqlite_writer_thread() -> bool:
    return (
        _sqlite_writer_thread_ident is not None
        and threading.get_ident() == _sqlite_writer_thread_ident
    )


@contextlib.contextmanager
def sqlite_writes_bypass_queue():
    """临时允许调用方线程直连 SQLite。

    仅用于启动早期迁移、测试初始化，或必须写后立刻读的轻量交互态。
    注意：上下文内不要使用 ``await``。
    """
    depth = getattr(_startup_sqlite_bootstrap_scope, "depth", 0)
    _startup_sqlite_bootstrap_scope.depth = depth + 1
    try:
        yield
    finally:
        _startup_sqlite_bootstrap_scope.depth = depth


@contextlib.contextmanager
def startup_sqlite_writes_bypass_queue():
    """在持久化消费者线程启动前直连 SQLite 的兼容入口。"""
    with sqlite_writes_bypass_queue():
        yield


def allow_direct_sqlite_writes() -> bool:
    """脚本 / 迁移 / 启动早期 bypass / 个别单测可走直连写库；正式运行时默认走队列。"""
    if getattr(_startup_sqlite_bootstrap_scope, "depth", 0) > 0:
        return True
    return SQLiteWriteEnvironmentSettings.from_env().direct_writes


def strip_sql_comments(sql: str) -> str:
    text = re.sub(r"--[^\n]*", "", sql)
    text = re.sub(r"/\*.*?\*/", "", text, flags=re.DOTALL)
    return text


def sql_is_mutating(sql: str) -> bool:
    s = strip_sql_comments(sql).strip().upper()
    if not s:
        return False
    if s.startswith("WITH"):
        return bool(re.search(r"\b(INSERT|UPDATE|DELETE|REPLACE)\b", s))
    tok = s.split()[0]
    non_mutating = frozenset(
        {
            "SELECT",
            "EXPLAIN",
            "PRAGMA",
            "BEGIN",
            "COMMIT",
            "ROLLBACK",
            "SAVEPOINT",
            "RELEASE",
            "END",
        }
    )
    if tok in non_mutating:
        return False
    return True


Params = Union[tuple, list]


class _EnqueuedStmtCursor:
    """A transaction cursor has no affected rows until its batch commits."""

    def __init__(self):
        self.rowcount = -1
        self.lastrowid = None
        self._committed = None

    def accept_result(self, result):
        self._committed = _CommittedStmtCursor(result)
        self.rowcount = self._committed.rowcount
        self.lastrowid = self._committed.lastrowid

    def fetchone(self):
        if self._committed is None:
            raise RuntimeError("事务提交前无法读取排队 SQL 的结果")
        return self._committed.fetchone()

    def fetchall(self):
        if self._committed is None:
            raise RuntimeError("事务提交前无法读取排队 SQL 的结果")
        return self._committed.fetchall()


class _CommittedRow:
    """Match sqlite3.Row's named and positional access after IPC serialization."""

    def __init__(self, columns, values):
        self._columns = columns
        self._values = values

    def keys(self):
        return self._columns

    def __getitem__(self, key):
        if isinstance(key, str):
            key = self._columns.index(key)
        return self._values[key]

    def __iter__(self):
        return iter(self._values)


class _CommittedStmtCursor:
    """Actual writer cursor results, published only after SQLite commit."""

    def __init__(self, result):
        self.rowcount = result["rowcount"]
        self.lastrowid = result.get("lastrowid")
        self.description = [(column, None, None, None, None, None, None)
                            for column in result.get("columns", [])] or None
        self._rows = [_CommittedRow(result.get("columns", []), row)
                      for row in result.get("rows", [])]
        self._index = 0

    def fetchone(self):
        if self._index >= len(self._rows):
            return None
        row = self._rows[self._index]
        self._index += 1
        return row

    def fetchall(self):
        rows = self._rows[self._index:]
        self._index = len(self._rows)
        return rows


class TxnCollectingConnection:
    """在 API 线程上收集 SQL，退出 `transaction()` 时一次性 EXECUTE_SQL_TXN_BATCH。"""

    def __init__(self) -> None:
        self.operations: List[Tuple[str, tuple]] = []
        self.cursors: List[_EnqueuedStmtCursor] = []

    def execute(self, sql: str, params: Any = ()) -> _EnqueuedStmtCursor:
        if isinstance(params, list):
            p: tuple = tuple(params)
        elif isinstance(params, tuple):
            p = params
        else:
            p = (params,)
        self.operations.append((sql, p))
        cursor = _EnqueuedStmtCursor()
        self.cursors.append(cursor)
        return cursor


def enqueue_execute_sql(
    sql: str, params: Optional[Params] = None, *, db_path: Optional[str] = None,
) -> _CommittedStmtCursor:
    from application.engine.services.persistence_queue import (
        get_persistence_queue,
        PersistenceCommandType,
    )

    pq = get_persistence_queue()
    plist = list(params) if params is not None else []
    receipt = pq.push_and_wait(
        PersistenceCommandType.EXECUTE_SQL.value,
        {"sql": sql, "params": plist, "db_path": db_path},
    )
    return _CommittedStmtCursor(receipt.result)


def enqueue_txn_batch(
    operations: List[Tuple[str, Params]], *, db_path: Optional[str] = None,
) -> Dict:
    """多语句同一 BEGIN IMMEDIATE 事务提交（高性能、原子）。"""
    from application.engine.services.persistence_queue import (
        get_persistence_queue,
        PersistenceCommandType,
    )

    if not operations:
        return {"operations": []}
    pq = get_persistence_queue()
    serializable = [
        {"sql": op[0], "params": list(op[1]) if op[1] is not None else []}
        for op in operations
    ]
    return pq.push_and_wait(
        PersistenceCommandType.EXECUTE_SQL_TXN_BATCH.value,
        {"operations": serializable, "db_path": db_path},
    ).result


def enqueue_delete_chapter(chapter_db_id: str, *, db_path: Optional[str] = None) -> bool:
    from application.engine.services.persistence_queue import (
        get_persistence_queue,
        PersistenceCommandType,
    )

    pq = get_persistence_queue()
    pq.push_and_wait(
        PersistenceCommandType.DELETE_CHAPTER.value,
        {"chapter_db_id": chapter_db_id, "db_path": db_path},
    )
    return True
