"""OS-held data-directory lease shared by backend and offline recovery tools.

Uses the standard Windows/POSIX file locks, released by the OS after a crash.
The JSON receipt is diagnostic; a PID alone never authorizes recovery.
"""
from contextlib import contextmanager
import json
import os
from pathlib import Path
from uuid import uuid4


class DatabaseOwnershipError(ValueError):
    pass


class DatabaseOwnership:
    def __init__(self, database):
        self.database = Path(database).resolve()
        self.path = self.database.parent / ".database-owner.lock"
        self._stream = None
        self.instance_id = uuid4().hex

    def acquire(self):
        if self._stream is not None:
            return self
        self.path.parent.mkdir(parents=True, exist_ok=True)
        stream = self.path.open("a+b")
        try:
            stream.seek(0, 2)
            if stream.tell() == 0:
                stream.write(b"\0")
                stream.flush()
            stream.seek(0)
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            stream.close()
            raise DatabaseOwnershipError("数据目录仍有活跃实例，禁止恢复或切换；请先关闭该实例") from exc
        self._stream = stream
        stream.seek(1)
        stream.truncate()
        stream.write(json.dumps({"pid": os.getpid(), "instance_id": self.instance_id,
                                 "database": str(self.database)}).encode("utf-8"))
        stream.flush()
        os.fsync(stream.fileno())
        return self

    def assert_held(self, database):
        if self._stream is None or Path(database).resolve() != self.database:
            raise DatabaseOwnershipError("恢复必须持有当前数据库的数据目录独占租约")

    def close(self):
        if self._stream is not None:
            stream, self._stream = self._stream, None
            stream.close()

    def __enter__(self):
        return self.acquire()

    def __exit__(self, *_):
        self.close()


def assert_no_legacy_owner(project_root):
    """The legacy launcher identifies its live process by executable/create time."""
    from scripts.install.utils import read_lock
    owner = read_lock(project_root)
    if owner is not None and owner[0] != os.getpid():
        raise DatabaseOwnershipError("项目启动器仍有活跃实例，禁止离线恢复")


@contextmanager
def offline_database_ownership(database, project_root=None):
    with DatabaseOwnership(database) as owner:
        if project_root is not None:
            assert_no_legacy_owner(project_root)
        yield owner
