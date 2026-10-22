"""Exclusive backup admission: no concurrent mutating HTTP request may run."""
from contextlib import contextmanager
import threading
from starlette.responses import JSONResponse


class DatabaseMaintenanceConflict(ValueError):
    pass


class DatabaseMaintenanceGate:
    def __init__(self):
        self._lock = threading.Lock()
        self._active = set()
        self._owner = None
        self._sealed = False

    def seal(self, scope):
        """Keep admission closed after an acknowledged update shutdown."""
        with self._lock:
            if self._owner != scope.get("database_maintenance_token"):
                raise DatabaseMaintenanceConflict("更新准备未取得独占写入权限")
            self._sealed = True

    def enter(self, scope):
        token = id(scope)
        with self._lock:
            if self._owner is not None:
                return False
            self._active.add(token)
        scope["database_maintenance_token"] = token
        return True

    def leave(self, scope):
        with self._lock:
            self._active.discard(scope.get("database_maintenance_token"))

    @contextmanager
    def exclusive(self, scope):
        token = scope.get("database_maintenance_token")
        with self._lock:
            if token is None or self._owner is not None or self._active != {token}:
                raise DatabaseMaintenanceConflict("仍有写入请求正在执行，请等待保存结束后重试备份")
            self._owner = token
        try:
            yield
        finally:
            with self._lock:
                if not self._sealed:
                    self._owner = None


class DatabaseMaintenanceMiddleware:
    def __init__(self, app, *, gate):
        self.app, self.gate = app, gate

    async def __call__(self, scope, receive, send):
        # Read-only event streams can stay open while SQLite backup runs.
        guarded = scope["type"] == "http" and scope.get("method") not in ("GET", "HEAD", "OPTIONS")
        if not guarded:
            return await self.app(scope, receive, send)
        if not self.gate.enter(scope):
            return await JSONResponse({"detail": "正在暂停写入以创建完整备份，请稍后重试"}, status_code=503)(scope, receive, send)
        try:
            await self.app(scope, receive, send)
        finally:
            self.gate.leave(scope)
