import json
from fastapi import FastAPI
from fastapi.testclient import TestClient
from infrastructure.security.audit_log import SecurityAuditLog
from interfaces.api.middleware.security_audit import SecurityAuditMiddleware
from interfaces.api.middleware.session_auth import SessionAuthority, SessionAuthMiddleware
from interfaces.api.middleware.local_request_guard import LocalRequestGuard
from interfaces.api.v1.control.security_audit import router


def test_real_http_denials_and_committed_response_metadata_never_log_secrets(tmp_path):
    app = FastAPI()
    store = SecurityAuditLog(tmp_path)
    app.state.security_audit_log = store
    app.include_router(router, prefix="/api/v1")
    authority = SessionAuthority()
    @app.post("/api/change/{novel_id}")
    def change(novel_id: str): return {"ok": True}
    app.add_middleware(SessionAuthMiddleware, authority=authority)
    app.add_middleware(LocalRequestGuard, allowed_hosts=["127.0.0.1"], allowed_origins=[])
    app.add_middleware(SecurityAuditMiddleware, store=store)
    with TestClient(app, base_url="http://127.0.0.1") as client:
        assert client.post("/api/change/private-book?key=secret-query",json={"body":"secret-prose"}).status_code == 401
        headers = {"Authorization": "Bearer " + authority.token}
        assert client.post("/api/change/private-book",headers={**headers,"Origin":"https://secret-origin"}).status_code == 403
        assert client.post("/api/change/private-book",headers=headers).status_code == 200
        assert client.get("/api/v1/settings/security-audit").status_code == 401
        report = client.get("/api/v1/settings/security-audit",headers=headers).json()
        assert {x["status"] for x in report["events"]} == {200,401,403}
        assert any(x["route"] == "/api/change/{novel_id}" for x in report["events"])
        raw = json.dumps(report)
        assert not any(x in raw for x in [authority.token,"secret-query","secret-prose","secret-origin","private-book"])
    store.close()
    assert SecurityAuditLog(tmp_path).read()["events"] == report["events"]


def test_disk_failure_is_reported_as_unavailable(tmp_path):
    path = tmp_path / "not-a-directory"
    path.write_text("occupied")
    store = SecurityAuditLog(path)
    store.record(method="POST", route="/api/*", status=200)
    report = store.read()
    assert report["status"] == "unavailable" and report["error"]
