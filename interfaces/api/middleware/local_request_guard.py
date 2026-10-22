"""Exact Host/Origin checks for the local desktop API (not authentication)."""
from urllib.parse import urlsplit

from starlette.responses import JSONResponse


class LocalRequestGuard:
    def __init__(self, app, *, allowed_hosts, allowed_origins):
        self.app = app
        self.allowed_hosts = frozenset(host.lower() for host in allowed_hosts)
        self.allowed_origins = frozenset(origin.rstrip("/") for origin in allowed_origins)
        if "*" in self.allowed_hosts or "*" in self.allowed_origins:
            raise ValueError("Local API Host/Origin rules must list explicit values")

    async def __call__(self, scope, receive, send):
        if scope["type"] not in ("http", "websocket"):
            return await self.app(scope, receive, send)
        headers = {}
        for key, value in scope.get("headers", []):
            name = key.decode("latin-1").lower()
            if name in headers and name in ("host", "origin"):
                return await self._reject(scope, receive, send, "Duplicate Host/Origin")
            headers[name] = value.decode("latin-1")
        host = headers.get("host", "")
        try:
            parsed = urlsplit("//" + host)
            _port = parsed.port  # Reject malformed/out-of-range ports too.
            valid_host = (parsed.hostname or "").lower() in self.allowed_hosts
            valid_host = valid_host and not (parsed.username or parsed.password or parsed.path
                                             or parsed.query or parsed.fragment)
        except ValueError:
            valid_host = False
        if not valid_host:
            return await self._reject(scope, receive, send, "Host is not allowed")
        origin = headers.get("origin")
        if origin is not None:
            same_origin = f"{scope.get('scheme', 'http')}://{host}"
            if origin not in self.allowed_origins and origin != same_origin:
                return await self._reject(scope, receive, send, "Origin is not allowed")
        return await self.app(scope, receive, send)

    @staticmethod
    async def _reject(scope, receive, send, detail):
        if scope["type"] == "websocket":
            return await send({"type": "websocket.close", "code": 1008})
        return await JSONResponse({"detail": detail}, status_code=403)(scope, receive, send)
