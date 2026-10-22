import asyncio


class SecurityAuditMiddleware:
    def __init__(self, app, *, store):
        self.app, self.store = app, store

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        status = 500

        async def capture(message):
            nonlocal status
            if message["type"] == "http.response.start":
                status = message["status"]
            await send(message)

        try:
            return await self.app(scope, receive, capture)
        finally:
            method = scope.get("method", "GET")
            path = scope.get("path", "")
            if path.startswith(("/api/", "/internal/")) and (status in (401, 403) or method not in ("GET", "HEAD", "OPTIONS")):
                # A rejected request may never reach a route. Do not persist its URL.
                route = getattr(scope.get("route"), "path", None) or ("/internal/*" if path.startswith("/internal/") else "/api/*")
                await asyncio.to_thread(self.store.record, method=method, route=route, status=status)
