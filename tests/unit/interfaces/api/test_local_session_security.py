"""Real HTTP admission and opaque SSE capabilities; no business database/providers."""
import asyncio

import httpx
import pytest
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse

from interfaces.api.middleware.local_request_guard import LocalRequestGuard
from interfaces.api.middleware.session_auth import SessionAuthority, SessionAuthMiddleware, session_router
from interfaces.api.settings import DEFAULT_LOCAL_HOSTS, DEFAULT_LOCAL_ORIGINS

TOKEN = "test-session-capability-0123456789abcdef"
AUTH = {"Authorization": f"Bearer {TOKEN}"}


@pytest.fixture
def app():
    app = FastAPI()
    app.state.calls = 0
    authority = SessionAuthority(TOKEN)
    app.state.session_authority = authority
    app.include_router(session_router)

    @app.get("/health")
    def health():
        return {"status": "healthy"}

    @app.get("/novels/page")
    def page():
        return "SPA document"

    @app.api_route("/api/v1/data", methods=["GET", "PUT", "DELETE"])
    @app.post("/api/v1/system/install-extensions")
    @app.post("/internal/shutdown")
    def sensitive():
        app.state.calls += 1
        return {"ok": True}

    @app.get("/api/v1/dag/events")
    @app.get("/api/v1/autopilot/novel/stream")
    def events():
        return StreamingResponse(iter(["data: verified\n\n"]), media_type="text/event-stream")

    app.add_middleware(CORSMiddleware, allow_origins=list(DEFAULT_LOCAL_ORIGINS),
                       allow_methods=["*"], allow_headers=["*"])
    app.add_middleware(SessionAuthMiddleware, authority=authority)
    app.add_middleware(LocalRequestGuard, allowed_hosts=DEFAULT_LOCAL_HOSTS,
                       allowed_origins=DEFAULT_LOCAL_ORIGINS)
    return app


def request(app, method, path, **kwargs):
    async def run():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                    base_url="http://127.0.0.1:8005") as client:
            return await client.request(method, path, **kwargs)
    return asyncio.run(run())


@pytest.mark.parametrize("method,path", [
    ("GET", "/api/v1/data"), ("PUT", "/api/v1/data"), ("DELETE", "/api/v1/data"),
    ("POST", "/api/v1/system/install-extensions"), ("POST", "/internal/shutdown"),
    ("GET", "/docs"), ("GET", "/openapi.json"), ("POST", "/internal/event-ticket"),
])
def test_sensitive_requests_require_a_session_before_handlers_run(app, method, path):
    assert request(app, method, path).status_code == 401
    assert app.state.calls == 0
    assert request(app, method, path, headers={"Authorization": "Bearer invalid"}).status_code == 401


@pytest.mark.parametrize("origin", [
    "http://localhost:3000", "http://127.0.0.1:8005", "tauri://localhost",
    "http://tauri.localhost", "https://tauri.localhost",
])
def test_trusted_origins_with_a_session_can_read_and_write(app, origin):
    assert request(app, "PUT", "/api/v1/data", headers={**AUTH, "Origin": origin}).status_code == 200
    assert app.state.calls == 1


@pytest.mark.parametrize("origin", ["https://evil.example", "null", "http://localhost:9876",
                                      "http://127.0.0.1.evil.example:8005"])
def test_wrong_origin_is_rejected_even_with_valid_token(app, origin):
    assert request(app, "PUT", "/api/v1/data", headers={**AUTH, "Origin": origin}).status_code == 403
    assert app.state.calls == 0


@pytest.mark.parametrize("host", ["evil.example", "localhost.evil.example", "localhost:99999", "user@localhost"])
def test_wrong_or_malformed_host_is_rejected(app, host):
    assert request(app, "GET", "/api/v1/data", headers={**AUTH, "Host": host}).status_code == 403


def test_cli_still_requires_secret_and_minimal_health_is_public(app):
    assert request(app, "GET", "/api/v1/data", headers=AUTH).status_code == 200
    assert request(app, "GET", "/health").json() == {"status": "healthy"}
    assert request(app, "GET", "/novels/page").status_code == 200
    assert request(app, "GET", "/internal/browser-session", headers={"Origin": "http://localhost:3000"}).status_code == 401
    assert request(app, "GET", "/api/v1/data?session_token=" + TOKEN).status_code == 401


def test_preflight_is_origin_checked_but_does_not_need_bearer(app):
    headers = {"Origin": "http://localhost:3000", "Access-Control-Request-Method": "PUT",
               "Access-Control-Request-Headers": "authorization,content-type"}
    response = request(app, "OPTIONS", "/api/v1/data", headers=headers)
    assert response.status_code == 200
    assert response.headers["access-control-allow-origin"] == "http://localhost:3000"
    assert request(app, "OPTIONS", "/api/v1/data", headers={**headers, "Origin": "https://evil.example"}).status_code == 403
    assert request(app, "OPTIONS", "/api/v1/data").status_code == 401


def test_duplicate_security_headers_are_rejected(app):
    assert request(app, "GET", "/api/v1/data", headers=[("Authorization", f"Bearer {TOKEN}"),
                  ("Authorization", f"Bearer {TOKEN}")]).status_code == 401
    assert request(app, "GET", "/api/v1/data", headers=[("Host", "localhost"),
                  ("Host", "evil.example"), ("Authorization", f"Bearer {TOKEN}")]).status_code == 403


def test_ticket_is_single_use_and_removed_from_scope(app):
    target = "/api/v1/dag/events?novel_id=one"
    issued = request(app, "POST", "/internal/event-ticket", headers=AUTH, json={"path": target})
    assert issued.status_code == 200
    ticket = issued.json()["ticket"]
    assert TOKEN not in issued.text
    response = request(app, "GET", target + "&_session_ticket=" + ticket)
    assert response.status_code == 200 and "data: verified" in response.text
    assert request(app, "GET", target + "&_session_ticket=" + ticket).status_code == 401


def test_ticket_is_bound_to_stream_path_and_query(app):
    issued = request(app, "POST", "/internal/event-ticket", headers=AUTH,
                     json={"path": "/api/v1/dag/events?novel_id=one"})
    ticket = issued.json()["ticket"]
    assert request(app, "GET", "/api/v1/dag/events?novel_id=two&_session_ticket=" + ticket).status_code == 401
    assert request(app, "GET", "/api/v1/data?_session_ticket=" + ticket).status_code == 401
    assert request(app, "POST", "/internal/event-ticket", headers=AUTH,
                   json={"path": "https://evil.example/api/v1/dag/events"}).status_code == 400
    assert request(app, "POST", "/internal/event-ticket", headers=AUTH,
                   json={"path": "/api/v1/data"}).status_code == 400


def test_ticket_expiration_and_session_restart_invalidate_capabilities(app, monkeypatch):
    now = [100.0]
    monkeypatch.setattr("interfaces.api.middleware.session_auth.time.monotonic", lambda: now[0])
    authority = app.state.session_authority
    ticket = authority.issue_ticket("/api/v1/dag/events")
    now[0] += 31
    assert not authority.consume_ticket(ticket, "/api/v1/dag/events")
    restarted = SessionAuthority("replacement-session-0123456789abcdef")
    assert not restarted.accepts(AUTH["Authorization"])
    assert not restarted.consume_ticket(ticket, "/api/v1/dag/events")
