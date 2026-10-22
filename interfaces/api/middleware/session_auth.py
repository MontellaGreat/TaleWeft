"""Local capability authentication; secrets are delivered by launcher or Tauri IPC."""
import os
import re
import secrets
import threading
import time
from urllib.parse import parse_qsl, urlencode, urlsplit

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel
from starlette.responses import JSONResponse

_EVENT_PATH = re.compile(r"^/api/v1/(?:dag/events|autopilot/[^/]+/(?:stream|log-stream|chapter-stream))$")


def _event_target(path: str) -> str:
    parsed = urlsplit(path)
    if parsed.scheme or parsed.netloc or parsed.fragment or not _EVENT_PATH.fullmatch(parsed.path):
        raise ValueError("Only local event stream paths can receive a ticket")
    params = parse_qsl(parsed.query, keep_blank_values=True)
    if any(key == "_session_ticket" for key, _value in params):
        raise ValueError("A ticket target cannot contain another ticket")
    query = urlencode(sorted(params))
    return parsed.path + ("?" + query if query else "")


class SessionAuthority:
    def __init__(self, token: str | None = None, *, ticket_ttl: float = 30.0):
        self.token = token or os.environ.get("PLOTPILOT_SESSION_TOKEN") or secrets.token_urlsafe(32)
        if len(self.token) < 32 or any(ch.isspace() for ch in self.token):
            raise ValueError("PLOTPILOT_SESSION_TOKEN must have at least 32 non-whitespace characters")
        self.ticket_ttl = ticket_ttl
        self._tickets = {}
        self._lock = threading.Lock()

    def accepts(self, authorization: str) -> bool:
        scheme, separator, token = authorization.partition(" ")
        return (bool(separator) and scheme.lower() == "bearer"
                and secrets.compare_digest(token.encode("utf-8"), self.token.encode("utf-8")))

    def issue_ticket(self, target: str) -> str:
        target = _event_target(target)
        now = time.monotonic()
        ticket = secrets.token_urlsafe(24)
        with self._lock:
            self._tickets = {key: value for key, value in self._tickets.items() if value[1] > now}
            if len(self._tickets) >= 1024:
                raise ValueError("Too many outstanding event stream tickets")
            self._tickets[ticket] = (target, now + self.ticket_ttl)
        return ticket

    def consume_ticket(self, ticket: str, target: str) -> bool:
        try:
            target = _event_target(target)
        except ValueError:
            return False
        with self._lock:
            value = self._tickets.pop(ticket, None)
        return value is not None and value[0] == target and value[1] > time.monotonic()


class SessionAuthMiddleware:
    def __init__(self, app, *, authority: SessionAuthority):
        self.app = app
        self.authority = authority

    async def __call__(self, scope, receive, send):
        if scope["type"] not in ("http", "websocket"):
            return await self.app(scope, receive, send)
        path = scope.get("path", "")
        method = scope.get("method", "GET")
        headers = [(key.lower(), value) for key, value in scope.get("headers", [])]
        # SPA document routes must remain loadable on a browser refresh; only
        # documents/assets are public, all data lives under /api or /internal.
        public_asset = not (path == "/api" or path.startswith("/api/")
                            or path == "/internal" or path.startswith("/internal/")
                            or path in ("/docs", "/redoc", "/openapi.json"))
        if scope["type"] == "http" and method in ("GET", "HEAD") and (public_asset or path == "/health"):
            return await self.app(scope, receive, send)
        header_map = dict(headers)
        if method == "OPTIONS" and b"origin" in header_map and b"access-control-request-method" in header_map:
            return await self.app(scope, receive, send)  # LocalRequestGuard runs first.
        authorizations = [value.decode("latin-1") for key, value in headers if key == b"authorization"]
        authorized = len(authorizations) == 1 and self.authority.accepts(authorizations[0])
        query = parse_qsl(scope.get("query_string", b"").decode("latin-1"), keep_blank_values=True)
        tickets = [value for key, value in query if key == "_session_ticket"]
        clean_query = [(key, value) for key, value in query if key != "_session_ticket"]
        if tickets:
            # Strip credentials from the scope used by downstream handlers/access logs.
            scope["query_string"] = urlencode(clean_query).encode("ascii")
            target = path + ("?" + urlencode(clean_query) if clean_query else "")
            if not authorized and method == "GET" and len(tickets) == 1:
                authorized = self.authority.consume_ticket(tickets[0], target)
        if not authorized:
            if scope["type"] == "websocket":
                return await send({"type": "websocket.close", "code": 1008})
            return await JSONResponse({"detail": "A valid local session is required"}, status_code=401,
                                      headers={"WWW-Authenticate": "Bearer", "Cache-Control": "no-store"})(scope, receive, send)
        return await self.app(scope, receive, send)


class EventTicketRequest(BaseModel):
    path: str


session_router = APIRouter(prefix="/internal", include_in_schema=False)


@session_router.post("/event-ticket")
def create_event_ticket(body: EventTicketRequest, request: Request):
    try:
        authority = request.app.state.session_authority
        ticket = authority.issue_ticket(body.path)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    return JSONResponse({"ticket": ticket, "expires_in": authority.ticket_ttl},
                        headers={"Cache-Control": "no-store"})
